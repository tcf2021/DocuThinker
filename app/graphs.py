import json
import logging
import re
import threading
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from . import db
from .config import DATA
from .indexing import activate, write_revision
from .metadata_agent import discover
from .intent import IntentAgent
from .metadata_catalog import metadata_snapshot
from .retrieval import (
    KnowledgeBaseRetriever,
    ready_documents,
    select_evidence,
    rank_routes,
)
from .parsing import Problem, mime, parse_pdf, safe_error, split_pages

log = logging.getLogger("uvicorn.error")


def prefix(doc):
    return f"agent-kb/{doc['kb']}/{doc['id']}"


class Ingest(TypedDict, total=False):
    doc: dict
    parsed: dict
    chunks: list
    manifest: dict
    proposal: dict
    revision: str


def ingestion_graph(sv):
    def archive(s):
        d = s["doc"]
        sv.store.prepare()
        sv.store.client.fput_object(
            sv.store.bucket,
            prefix(d) + "/source.pdf",
            str(DATA / "spool" / d["id"]),
            content_type="application/pdf",
        )
        return {}

    def parse(s):
        return {"parsed": parse_pdf(sv.cfg, DATA / "spool" / s["doc"]["id"])}

    def save(s):
        d, parsed = s["doc"], s["parsed"]
        folder = f"{prefix(d)}/run-{d['attempt']}/"
        files = parsed["images"] | {
            "document.md": parsed["md"].encode(),
            "content_list.json": json.dumps(
                parsed["blocks"], ensure_ascii=False, indent=2
            ).encode(),
        }
        for name, body in files.items():
            sv.store.put(folder + name, body, mime(name))
        return {"manifest": {"paths": ["source.pdf", *files], "pages": {}}}

    def chunk(s):
        return {"chunks": split_pages(s["parsed"])}

    def metadata(s):
        return {
            "proposal": discover(sv, s["doc"], s["chunks"]),
            "revision": db.uid(),
        }

    def index(s):
        write_revision(sv, s["doc"], s["chunks"], s["proposal"], s["revision"])
        return {}

    def commit(s):
        d = s["doc"]
        manifest = dict(s["manifest"])
        manifest["pages"] = {
            str(c["page"]): c["images"]
            for c in s["chunks"]
            if c["page"] and c["images"]
        }
        with db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            activate(con, d, s["proposal"], s["revision"], len(s["chunks"]))
            con.execute(
                "UPDATE docs SET status='ready',stage='ready',n_chunks=?,manifest=? WHERE id=?",
                (len(s["chunks"]), json.dumps(manifest, ensure_ascii=False), d["id"]),
            )
            db.event(con, d["id"], d["attempt"], "ready", f"{len(s['chunks'])} chunks")
        return {}

    graph = StateGraph(Ingest)
    prev = START

    def traced(name, func):
        def run(s):
            d = s["doc"]
            db.stage(d["id"], d["attempt"], name)
            return func(s)

        return run

    for name, func in [
        ("original", archive),
        ("mineru", parse),
        ("artifacts", save),
        ("split", chunk),
        ("metadata", metadata),
        ("embed_index", index),
        ("publish", commit),
    ]:
        graph.add_node(name, traced(name, func))
        graph.add_edge(prev, name)
        prev = name
    graph.add_edge(prev, END)
    return graph.compile()


class Worker:
    def __init__(self, graph, index_graph=None):
        self.graph = graph
        self.index_graph = index_graph
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.work, name="ingestion", daemon=True)

    def work(self):
        while not self.stop.is_set():
            d = db.claim()
            if not d:
                job = db.claim_index() if self.index_graph else None
                if job:
                    try:
                        self.index_graph.invoke({"job": job})
                    except Exception as exc:
                        msg = safe_error(exc)
                        with db.connection() as con:
                            con.execute(
                                "UPDATE index_jobs SET status='failed',error=? WHERE id=?",
                                (msg, job["id"]),
                            )
                        log.error("Reindex %s failed: %s", job["id"], msg)
                else:
                    self.stop.wait(0.5)
                continue
            try:
                self.graph.invoke({"doc": d})
            except Exception as exc:
                msg = safe_error(exc)
                with db.connection() as con:
                    con.execute(
                        "UPDATE docs SET status='failed',error=? WHERE id=?",
                        (msg, d["id"]),
                    )
                    db.event(con, d["id"], d["attempt"], "failed", msg)
                log.error("Ingestion %s failed: %s", d["id"], msg)
            else:
                try:
                    (DATA / "spool" / d["id"]).unlink(missing_ok=True)
                except OSError:
                    log.warning("Spool cleanup pending for %s", d["id"])


def evidence_map(rows):
    # The model selects sentence IDs. Only the server copies verbatim evidence into citations.
    refs = {}
    for number, row in enumerate(rows, 1):
        for part in re.split(r"\n+|(?<=[。！？])|(?<=[.!?])\s+", row["text"]):
            if quote_text := part.strip():
                refs[len(refs) + 1] = row | {"number": number, "quote": quote_text}
    return refs


def grounded(raw, refs):
    try:
        cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I)
        result = json.loads(cleaned)
        answer, ids = result["answer"], result["evidence_ids"]
        if isinstance(answer, dict) and set(answer) == {"answer"}:
            answer = answer["answer"]
        if not isinstance(answer, str) or not isinstance(ids, list):
            raise ValueError
        if not ids:
            return {
                "status": "insufficient",
                "answer": "现有资料不足以回答这个问题。",
                "sources": [],
            }
        if not answer.strip():
            raise ValueError
        citations = []
        used = set()
        for i in ids:
            if isinstance(i, str) and i.isascii() and i.isdigit():
                i = int(i)
            if type(i) is not int or i not in refs:
                raise ValueError
            if i not in used:
                citations.append(refs[i] | {"number": len(citations) + 1})
                used.add(i)
        return {"status": "answered", "answer": answer.strip(), "sources": citations}
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise Problem(
            "模型返回的证据编号或 JSON 无法核对。请重试，或点击“只看检索”核对片段。"
        ) from exc


class QA(TypedDict, total=False):
    kb: str
    owner: str
    filters: dict
    planner: str
    chat_history: list
    documents: list
    metadata_catalog: dict
    answer_engine: str
    agent_trace: list
    plan: dict
    trace: list
    question: str
    mode: str
    rows: list
    result: dict


def qa_graph(sv):
    retriever = KnowledgeBaseRetriever(sv)
    graph = StateGraph(QA)
    agent = IntentAgent(sv)

    def add(name, function):
        def run(state):
            started = time.perf_counter()
            update = function(state)
            trace = state.get("agent_trace", []) + [
                {
                    "node": name,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": "completed",
                }
            ]
            update["agent_trace"] = trace
            if name == "explain":
                update["result"]["agent_trace"] = trace
            return update

        return graph.add_node(name, run)

    def load_catalog(s):
        docs = ready_documents(s["owner"], s["kb"])
        snapshot = metadata_snapshot(sv, s["owner"], s["kb"], docs)
        return {"documents": docs, "metadata_catalog": snapshot}

    def intent(s):
        docs = s["documents"]
        plan = agent.plan(
            s["question"],
            docs,
            s.get("filters"),
            s.get("planner", "auto"),
            chat_history=s.get("chat_history", []),
            user_context=db.retrieval_context(s["owner"], s["kb"]),
            fields=s["metadata_catalog"]["filterable_fields"],
        )
        plan["metadata_catalog"] = {
            k: v for k, v in s["metadata_catalog"].items() if k != "filterable_fields"
        }
        return {"plan": plan}

    def retrieve(s):
        rows, trace = retriever.search(
            s["owner"], s["kb"], s["question"], s["documents"], s["plan"]
        )
        return {"rows": rows, "trace": trace}

    add("metadata_catalog", load_catalog)
    add("intent", intent)
    add("retrieve", retrieve)
    add(
        "rerank",
        lambda s: {
            "rows": select_evidence(
                rank_routes(sv, s["question"], s["rows"]), s["plan"]
            )
        },
    )
    add(
        "evidence",
        lambda s: {
            "result": {"status": "retrieved", "answer": "", "sources": s["rows"]}
        },
    )
    add(
        "empty",
        lambda s: {
            "result": {
                "status": "empty",
                "answer": "当前条件下没有匹配证据，请核对筛选条件及索引状态。",
                "sources": [],
            }
        },
    )

    def answer(s):
        floor = float(sv.cfg.get("min_rerank_score", 0.0))
        refs = evidence_map([r for r in s["rows"] if r["rerank"] >= floor])
        if not refs:
            return {
                "result": {
                    "status": "insufficient",
                    "answer": "候选片段的相关性不足，现有资料不足以回答这个问题。",
                    "sources": [],
                }
            }
        evidence = [
            {
                "id": i,
                "text": r["quote"],
                "filename": r["filename"],
                "page": r["page"],
                "metadata": r.get("metadata", {}),
            }
            for i, r in refs.items()
        ]
        question = s["plan"].get("search_query", s["question"])
        if s.get("answer_engine") == "crewai":
            from .crew_agents import review_evidence

            try:
                checked, crew_trace = review_evidence(sv, question, evidence)
                result = grounded(checked.model_dump_json(), refs)
                result.update(
                    answer_engine="crewai",
                    review=checked.verdict,
                    review_issues=checked.issues,
                    crew_trace=crew_trace,
                )
                return {"result": result}
            except Exception as exc:
                log.warning("CrewAI verification failed: %s", safe_error(exc))
                return {
                    "result": {
                        "status": "review_failed",
                        "answer": "多 Agent 核验未完成，未发布草稿。可重试或切换单模型回答。",
                        "sources": [],
                        "answer_engine": "crewai",
                        "review": "failed",
                    }
                }
        return {
            "result": grounded(sv.chat(question, evidence), refs)
            | {"answer_engine": "single"}
        }

    add("answer", answer)
    add("crew_review", answer)
    add(
        "clarify",
        lambda s: {
            "trace": [],
            "result": {
                "status": "clarification",
                "answer": s["plan"]["clarifying_question"],
                "sources": [],
            },
        },
    )
    graph.add_edge(START, "metadata_catalog")
    graph.add_edge("metadata_catalog", "intent")
    graph.add_conditional_edges(
        "intent",
        lambda s: "clarify" if s["plan"].get("needs_clarification") else "retrieve",
        {"clarify": "clarify", "retrieve": "retrieve"},
    )
    graph.add_conditional_edges(
        "retrieve",
        lambda s: "rerank" if s["rows"] else "empty",
        {"rerank": "rerank", "empty": "empty"},
    )
    graph.add_conditional_edges(
        "rerank",
        lambda s: "evidence"
        if s["mode"] == "search"
        else ("crew_review" if s.get("answer_engine") == "crewai" else "answer"),
        {"evidence": "evidence", "answer": "answer", "crew_review": "crew_review"},
    )
    add(
        "explain",
        lambda s: {
            "result": s["result"] | {"intent": s["plan"], "retrieval_trace": s["trace"]}
        },
    )
    for name in ("empty", "evidence", "answer", "crew_review", "clarify"):
        graph.add_edge(name, "explain")
    graph.add_edge("explain", END)
    return graph.compile()
