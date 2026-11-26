import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from . import db
from .config import DATA, MAX_UPLOAD, INDEX_VERSION, settings
from .graphs import Worker, ingestion_graph, prefix, qa_graph, load_catalog, plan_query
from .indexing import cached_chunks, reindex_graph
from .intent import Filters, InvalidQueryFilter, catalog
from .metadata import DocumentMetadata, AttributeKey, propose
from .metadata_agent import discover, Discovery, FieldDescription
from .field_schema import IntentOutput
from .metadata_catalog import metadata_snapshot
from .parsing import Problem, mime, safe_error
from .retrieval import ready_documents
from .services import Services
from .readiness import check_readiness

STATIC = Path(__file__).parent / "static"
COOKIE = "agent_session"


@asynccontextmanager
async def lifespan(app):
    cfg = settings()
    check_readiness(DATA / "metadata.sqlite", cfg)
    db.init_db()
    db.requeue_interrupted()
    # Only one API process owns this spool and Milvus Lite file.
    with db.connection() as con:
        retained = {
            r[0] for r in con.execute("SELECT id FROM docs WHERE status!='ready'")
        }
    for path in (DATA / "spool").iterdir():
        if path.is_file() and path.name not in retained:
            path.unlink(missing_ok=True)
    sv = Services(cfg)
    app.state.sv = sv
    app.state.qa = qa_graph(sv)
    worker = Worker(ingestion_graph(sv), reindex_graph(sv))
    worker.thread.start()
    yield
    worker.stop.set()
    worker.thread.join(timeout=2)


app = FastAPI(title="DocuThinker · 文档分析与 RAG", version="5.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    with db.connection() as con:
        con.execute("SELECT 1").fetchone()
    return {
        "status": "ok",
        "version": "5.0",
        "project": "DocuThinker",
        "metadata_schema_version": INDEX_VERSION,
        "intent_agent_version": 5,
    }


def session_token(req):
    value = req.headers.get("authorization", "")
    return value[7:] if value.startswith("Bearer ") else req.cookies.get(COOKIE, "")


def account(req: Request):
    with db.connection() as con:
        row = con.execute(
            "SELECT users.id,name FROM sessions JOIN users ON users.id=sessions.owner "
            "WHERE sessions.digest=? AND expires>?",
            (db.token_hash(session_token(req)), int(time.time())),
        ).fetchone()
    if not row:
        raise HTTPException(401, "请先登录。")
    return dict(row)


def get_kb(kb_id, who):
    with db.connection() as con:
        row = con.execute(
            "SELECT id,name FROM kbs WHERE id=? AND owner=?", (kb_id, who["id"])
        ).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在。")
    return dict(row)


def get_doc(doc_id, who):
    with db.connection() as con:
        row = con.execute(
            "SELECT docs.* FROM docs JOIN kbs ON docs.kb=kbs.id WHERE docs.id=? AND kbs.owner=?",
            (doc_id, who["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在。")
    return dict(row)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class KBName(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["search", "chat"] = "chat"
    filters: Filters = Field(default_factory=Filters)
    planner: Literal["auto", "llm", "manual"] = "auto"
    chat_history: list[HistoryMessage] = Field(default_factory=list, max_length=10)
    answer_engine: Literal["single", "crewai"] = "single"


class MetadataUpdate(BaseModel):
    metadata: DocumentMetadata
    field_definitions: dict[AttributeKey, FieldDescription] = Field(
        default_factory=dict, max_length=24
    )
    expected_revision: str = Field(max_length=32)


class ProposalRequest(BaseModel):
    method: Literal["rules", "llm"] = "llm"


class RollbackRequest(BaseModel):
    revision: str = Field(max_length=32)
    expected_revision: str = Field(max_length=32)


@app.post("/api/login")
def sign_in(body: Login, response: Response):
    token = db.login(body.username.strip(), body.password)
    if not token:
        raise HTTPException(401, "账号或密码不正确。")
    response.set_cookie(COOKIE, token, max_age=43200, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_in": 43200}


@app.post("/api/logout")
def sign_out(req: Request, response: Response):
    with db.connection() as con:
        con.execute(
            "DELETE FROM sessions WHERE digest=?", (db.token_hash(session_token(req)),)
        )
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(who=Depends(account)):
    return {"username": who["name"]}


@app.get("/api/kbs")
def list_kbs(who=Depends(account)):
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT id,name FROM kbs WHERE owner=? ORDER BY rowid", (who["id"],)
            )
        ]


@app.post("/api/kbs", status_code=201)
def create_kb(body: KBName, who=Depends(account)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "请输入知识库名称。")
    kb = db.uid()
    try:
        with db.connection() as con:
            con.execute("INSERT INTO kbs VALUES(?,?,?)", (kb, who["id"], name))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "已经有同名知识库。")
    return {"id": kb, "name": name}


DOCUMENT_SUMMARY_COLUMNS = (
    "id", "filename", "status", "stage", "attempt", "n_chunks",
    "error", "created", "index_version", "index_revision",
)


@app.get("/api/kbs/{kb_id}/documents")
def list_docs(kb_id: str, who=Depends(account)):
    get_kb(kb_id, who)
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                f"SELECT {', '.join(DOCUMENT_SUMMARY_COLUMNS)} FROM docs WHERE kb=? ORDER BY created DESC,rowid DESC",
                (kb_id,),
            )
        ]


@app.post("/api/kbs/{kb_id}/documents", status_code=202)
def upload(kb_id: str, file: UploadFile, response: Response, who=Depends(account)):
    get_kb(kb_id, who)
    name = Path((file.filename or "").replace("\\", "/")).name[:220]
    if not name.lower().endswith(".pdf"):
        raise HTTPException(415, "请上传 .pdf 文件。")
    spool = DATA / "spool"
    doc_id = db.uid()
    sha = hashlib.sha256()
    size = 0
    tmp = None
    installed = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=spool, prefix=".upload-", delete=False
        ) as out:
            tmp = Path(out.name)
            while chunk := file.file.read(1024 * 1024):
                if not size and b"%PDF-" not in chunk[:1024]:
                    raise HTTPException(415, "文件内容没有 PDF 文件头。")
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "每份 PDF 最多 50 MiB。")
                sha.update(chunk)
                out.write(chunk)
        if not size:
            raise HTTPException(422, "PDF 是空文件。")
        with db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id,status FROM docs WHERE kb=? AND sha=?",
                (kb_id, sha.hexdigest()),
            ).fetchone()
            if row:
                response.status_code = 200
                return dict(row) | {"duplicate": True}
            os.replace(tmp, spool / doc_id)
            installed = True
            con.execute(
                "INSERT INTO docs(id,kb,filename,sha,created) VALUES(?,?,?,?,?)",
                (doc_id, kb_id, name, sha.hexdigest(), int(time.time())),
            )
            db.event(con, doc_id, 0, "queued")
        return {"id": doc_id, "status": "queued", "duplicate": False}
    except BaseException:
        if installed:
            (spool / doc_id).unlink(missing_ok=True)
        raise
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


@app.get("/api/documents/{doc_id}")
def document(doc_id: str, who=Depends(account)):
    d = get_doc(doc_id, who)
    with db.connection() as con:
        events = [
            dict(r)
            for r in con.execute(
                "SELECT attempt,stamp,stage,message FROM events WHERE doc=? ORDER BY seq",
                (doc_id,),
            )
        ]
        jobs = [
            dict(r)
            for r in con.execute(
                "SELECT id,status,stage,created,attempt,error FROM index_jobs WHERE doc=? ORDER BY created DESC,rowid DESC LIMIT 5",
                (doc_id,),
            )
        ]
        history = [
            dict(r)
            for r in con.execute(
                "SELECT revision,index_version,n_chunks,activated FROM index_history WHERE doc=? ORDER BY activated DESC",
                (doc_id,),
            )
        ]
    return {key: d[key] for key in DOCUMENT_SUMMARY_COLUMNS} | {
        "events": events,
        "files": json.loads(d["manifest"]).get("paths", []),
        "metadata": DocumentMetadata.model_validate_json(
            d["metadata_json"]
        ).model_dump(),
        "metadata_provenance": json.loads(d["metadata_provenance"]),
        "index_jobs": jobs,
        "index_history": history,
    }


@app.get("/api/metadata/schema")
def metadata_schema(who=Depends(account)):
    return {
        "schema_version": INDEX_VERSION,
        "metadata": DocumentMetadata.model_json_schema(),
        "filters": Filters.model_json_schema(),
        "intent_output": IntentOutput.model_json_schema(),
        "metadata_discovery": Discovery.model_json_schema(),
        "conventions": {
            "lists": "同字段多个值为 OR，不同字段为 AND。",
            "mentions": "主题、地点、实体和编号表示文档提及，不代表适用关系；attributes 需有明确原文或人工依据。",
            "attributes": "所有领域统一为小写英文属性名→非空字符串数组，例如 department: [研发部]。只支持精确匹配，不做数值范围比较。",
            "unknown": "未知字符串用空字符串、列表用 []；effective_at 仅匹配已确认起止日期。",
            "access": "owner、kb、doc、revision 由服务端确定，客户端不能覆盖。",
        },
    }


@app.get("/api/agents")
def agent_architecture(request: Request, who=Depends(account)):
    return {
        "orchestrator": "LangGraph",
        "answer_engines": ["single", "crewai"],
        "crewai_roles": ["Evidence Analyst", "Evidence Reviewer"],
        "graph": request.app.state.qa.get_graph().draw_mermaid(),
        "scope": "Authenticated owner + selected knowledge base + active document revision",
        "memory": "Request-local; no shared CrewAI memory",
    }


@app.get("/api/kbs/{kb_id}/metadata")
def kb_metadata(
    kb_id: str, request: Request, refresh: bool = False, who=Depends(account)
):
    get_kb(kb_id, who)
    documents = ready_documents(who["id"], kb_id)
    try:
        snapshot = metadata_snapshot(
            request.app.state.sv, who["id"], kb_id, documents, refresh
        )
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from exc
    return {
        "fields": catalog(documents),
        **snapshot,
        "ready_documents": len(documents),
        "current_documents": sum(
            d["index_version"] == INDEX_VERSION for d in documents
        ),
        "metadata_schema_version": INDEX_VERSION,
        "agent_documents": sum(
            json.loads(d["metadata_provenance"]).get("agent_version") == 4
            for d in documents
        ),
    }


@app.post("/api/kbs/{kb_id}/intent")
def preview_intent(kb_id: str, body: Question, request: Request, who=Depends(account)):
    get_kb(kb_id, who)
    if not body.question.strip():
        raise HTTPException(422, "请输入问题。")
    state = body.model_dump() | {"owner": who["id"], "kb": kb_id, "question": body.question.strip()}
    sv = request.app.state.sv
    try:
        return plan_query(sv, state | load_catalog(sv, state))["plan"]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Problem as exc:
        raise HTTPException(502, safe_error(exc)) from exc


@app.post("/api/documents/{doc_id}/metadata/propose")
def propose_metadata(
    doc_id: str, body: ProposalRequest, request: Request, who=Depends(account)
):
    d = get_doc(doc_id, who)
    if d["status"] != "ready":
        raise HTTPException(409, "文档就绪后才能拟定 Metadata。")
    try:
        chunks = cached_chunks(request.app.state.sv, d)
        if body.method == "llm":
            return discover(request.app.state.sv, d, chunks, preserve=False)
        return propose(d["filename"], chunks)
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from exc


@app.put("/api/documents/{doc_id}/metadata", status_code=202)
def update_metadata(doc_id: str, body: MetadataUpdate, who=Depends(account)):
    get_doc(doc_id, who)
    with db.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT index_revision FROM docs WHERE id=?", (doc_id,)
        ).fetchone()[0]
        if current != body.expected_revision:
            raise HTTPException(409, "文档版本已更新，请刷新详情后再保存。")
        try:
            definitions = {
                k: v.model_dump()
                for k, v in body.field_definitions.items()
                if k in body.metadata.attributes
            }
            return db.enqueue_index(
                con,
                doc_id,
                json.dumps(
                    {
                        "metadata": body.metadata.model_dump(),
                        "field_definitions": definitions,
                    },
                    ensure_ascii=False,
                ),
            )
        except Problem as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/documents/{doc_id}/reindex", status_code=202)
def reindex_document(doc_id: str, who=Depends(account)):
    get_doc(doc_id, who)
    with db.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            return db.enqueue_index(con, doc_id)
        except Problem as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/kbs/{kb_id}/reindex", status_code=202)
def reindex_kb(kb_id: str, who=Depends(account)):
    get_kb(kb_id, who)
    with db.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        docs = con.execute(
            "SELECT id FROM docs WHERE kb=? AND status='ready'", (kb_id,)
        ).fetchall()
        return {"jobs": [db.enqueue_index(con, d["id"]) for d in docs]}


@app.get("/api/kbs/{kb_id}/index-jobs")
def index_jobs(kb_id: str, who=Depends(account)):
    get_kb(kb_id, who)
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT j.id,j.doc,d.filename,j.status,j.stage,j.created,j.error FROM index_jobs j "
                "JOIN docs d ON j.doc=d.id WHERE d.kb=? ORDER BY j.created DESC,j.rowid DESC LIMIT 200",
                (kb_id,),
            )
        ]


@app.post("/api/index-jobs/{job_id}/retry", status_code=202)
def retry_index(job_id: str, who=Depends(account)):
    with db.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT j.* FROM index_jobs j JOIN docs d ON j.doc=d.id JOIN kbs k ON d.kb=k.id WHERE j.id=? AND k.owner=?",
            (job_id, who["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "索引任务不存在。")
        if (
            row["status"] != "failed"
            or con.execute(
                "SELECT 1 FROM index_jobs WHERE doc=? AND status IN ('queued','running')",
                (row["doc"],),
            ).fetchone()
        ):
            raise HTTPException(
                409, "只能重试失败任务，且文档不能有其他排队中的索引任务。"
            )
        con.execute(
            "UPDATE index_jobs SET status='queued',stage='queued',error='' WHERE id=?",
            (job_id,),
        )
    return {"id": job_id, "status": "queued"}


@app.post("/api/documents/{doc_id}/index/rollback")
def rollback_index(doc_id: str, body: RollbackRequest, who=Depends(account)):
    get_doc(doc_id, who)
    with db.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        doc = dict(con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone())
        old = con.execute(
            "SELECT * FROM index_history WHERE doc=? AND revision=?",
            (doc_id, body.revision),
        ).fetchone()
        pending = con.execute(
            "SELECT 1 FROM index_jobs WHERE doc=? AND status IN ('queued','running')",
            (doc_id,),
        ).fetchone()
        if not old:
            raise HTTPException(404, "未找到保留的索引版本。")
        if old["index_version"] != INDEX_VERSION:
            raise HTTPException(409, "此历史索引格式已停用。")
        if pending or doc["index_revision"] != body.expected_revision:
            raise HTTPException(409, "索引任务未完成或当前版本已变化，请刷新后重试。")
        db.remember_index(con, doc)
        con.execute(
            "UPDATE docs SET index_revision=?,index_version=?,metadata_json=?,metadata_provenance=?,n_chunks=? WHERE id=?",
            (
                old["revision"],
                old["index_version"],
                old["metadata_json"],
                old["metadata_provenance"],
                old["n_chunks"],
                doc_id,
            ),
        )
        db.event(con, doc_id, doc["attempt"], "rollback", "已恢复保留的索引版本")
    return {"index_revision": old["revision"], "index_version": old["index_version"]}


@app.post("/api/documents/{doc_id}/retry", status_code=202)
def retry(doc_id: str, who=Depends(account)):
    d = get_doc(doc_id, who)
    with db.connection() as con:
        row = con.execute(
            "UPDATE docs SET status='queued',stage='queued',error='' WHERE id=? AND status='failed'",
            (doc_id,),
        )
        if not row.rowcount:
            raise HTTPException(409, "只有失败的任务可以重试。")
        db.event(con, doc_id, d["attempt"], "queued", "手动重试")
    return {"status": "queued"}


@app.get("/api/documents/{doc_id}/files/{name:path}")
def artifact(doc_id: str, name: str, request: Request, who=Depends(account)):
    d = get_doc(doc_id, who)
    if d["status"] != "ready" or name not in json.loads(d["manifest"]).get("paths", []):
        raise HTTPException(404, "未找到已就绪的文件。")
    folder = prefix(d) + ("/" if name == "source.pdf" else f"/run-{d['attempt']}/")
    try:
        result = request.app.state.sv.store.read_stream(folder + name)
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from exc

    def body():
        try:
            yield from result.stream(1024 * 128)
        finally:
            result.close()
            result.release_conn()

    return StreamingResponse(
        body(),
        media_type=mime(name),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/kbs/{kb_id}/ask")
def ask(kb_id: str, body: Question, request: Request, who=Depends(account)):
    get_kb(kb_id, who)
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "请输入问题。")
    try:
        return request.app.state.qa.invoke(
            {
                "kb": kb_id,
                "owner": who["id"],
                "question": question,
                "mode": body.mode,
                "filters": body.filters.model_dump(),
                "planner": body.planner,
                "chat_history": [m.model_dump() for m in body.chat_history],
                "answer_engine": body.answer_engine,
            }
        )["result"]
    except InvalidQueryFilter as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from exc


@app.post("/api/checks")
def checks(request: Request, models: bool = False, who=Depends(account)):
    sv = request.app.state.sv
    out = {}

    def probe(name, callback, success):
        try:
            callback()
            out[name] = {"ok": True, "detail": success}
        except Exception as exc:
            out[name] = {"ok": False, "detail": safe_error(exc)}

    def mineru():
        with httpx.Client(trust_env=False, timeout=7) as client:
            res = client.get(sv.cfg["mineru_url"].rstrip("/") + "/health")
            res.raise_for_status()
            spec = client.get(sv.cfg["mineru_url"].rstrip("/") + "/openapi.json")
            spec.raise_for_status()
            if "post" not in spec.json()["paths"].get("/file_parse", {}):
                raise Problem(
                    "已连通服务，但未找到 POST /file_parse，请核对 8000 的 API。"
                )

    probe("MinerU", mineru, "/health 与 POST /file_parse 已确认")
    probe("MinIO", sv.store.check, "已在目标桶写入、读回并删除自检对象")
    probe("Milvus", lambda: sv.vectors.client.list_collections(), "已连接向量集合")
    configured = bool(sv.cfg.get("chat_model") and sv.cfg.get("chat_base"))
    out["LLM"] = {
        "ok": None,
        "detail": (
            "已配置 " + sv.cfg["chat_model"] + "，用“生成答案”验证调用"
            if configured
            else "尚未配置；可使用“只看检索”"
        ),
    }
    separate = bool(sv.cfg.get("agent_base"))
    agent_model = sv.cfg.get("agent_model") if separate else sv.cfg.get("chat_model")
    agent_base = sv.cfg.get("agent_base") if separate else sv.cfg.get("chat_base")
    out["Agent LLM"] = {
        "ok": None,
        "detail": (
            "已配置 "
            + agent_model
            + ("（独立内部服务）" if separate else "（复用聊天服务）")
            + "；用“查看检索计划”验证调用"
        )
        if agent_model and agent_base
        else "未配置；自动字段识别需要先配置内部模型或聊天服务。",
    }
    if models:
        probe(
            "Embedding",
            lambda: sv.embed.embed_query("文档测试"),
            "本地模型已完成一次向量计算",
        )
        probe(
            "Reranker",
            lambda: sv.rank("文档测试", [{"text": "文档测试"}]),
            "本地模型已完成一次打分",
        )
    return out
