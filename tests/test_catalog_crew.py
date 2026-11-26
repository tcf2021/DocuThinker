import json

import pytest

from app import db
from app.classify_intent import build_filter_expression, build_intent_payload
from app.config import COLLECTION
from app.field_schema import IntentOutput
from app.graphs import qa_graph
from app.indexing import reindex_graph
from app.metadata_catalog import MetadataCatalog, metadata_snapshot
from app.parsing import Problem
from app.retrieval import ready_documents


def snapshot(env, **kwargs):
    return metadata_snapshot(
        env.sv, env.owner, env.kb, ready_documents(env.owner, env.kb), **kwargs
    )


def ask(env, **kwargs):
    return qa_graph(env.sv).invoke(
        {
            "owner": env.owner,
            "kb": env.kb,
            "question": "保留多少天？",
            "mode": "chat",
            "planner": "manual",
            "answer_engine": "crewai",
            **kwargs,
        }
    )["result"]


def test_catalog_reads_actual_milvus_metadata_not_sqlite_values(env):
    doc = env.document(metadata={"attributes": {"custom_key": ["indexed-value"]}})
    with db.connection() as con:
        con.execute(
            "UPDATE docs SET metadata_json=? WHERE id=?",
            (json.dumps({"attributes": {"wrong_sql_key": ["not-indexed"]}}), doc["id"]),
        )
    result = snapshot(env)
    fields = {f["field"]: f for f in result["filterable_fields"]}
    assert result["source"] == "milvus"
    assert fields["attributes.custom_key"]["examples"] == ["indexed-value"]
    assert "attributes.wrong_sql_key" not in fields
    assert "metadata.embedding_model" not in fields
    assert "metadata.section" not in fields


def test_catalog_cache_invalidates_on_publish_and_rollback(env, monkeypatch):
    doc = env.document(metadata={"attributes": {"phase": ["before"]}})
    calls = []
    original = env.sv.vectors.metadata_documents

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(env.sv.vectors, "metadata_documents", counted)
    first = snapshot(env)
    assert not first["cache_hit"]
    assert snapshot(env)["cache_hit"] and len(calls) == 1
    updated = json.loads(doc["metadata_json"]) | {"attributes": {"phase": ["after"]}}
    with db.connection() as con:
        db.enqueue_index(con, doc["id"], json.dumps(updated))
    assert snapshot(env)[
        "cache_hit"
    ]  # Unpublished revision must not affect active schema.
    reindex_graph(env.sv).invoke({"job": db.claim_index()})
    after = snapshot(env)
    assert not after["cache_hit"] and after["fingerprint"] != first["fingerprint"]
    assert next(
        f for f in after["filterable_fields"] if f["field"] == "attributes.phase"
    )["examples"] == ["after"]
    with db.connection() as con:
        con.execute(
            "UPDATE docs SET index_revision=?,metadata_json=?,metadata_provenance=? WHERE id=?",
            (
                doc["index_revision"],
                doc["metadata_json"],
                doc["metadata_provenance"],
                doc["id"],
            ),
        )
    rollback = snapshot(env)
    assert not rollback["cache_hit"]
    assert next(
        f for f in rollback["filterable_fields"] if f["field"] == "attributes.phase"
    )["examples"] == ["before"]
    assert len(calls) == 3


def test_catalog_cache_rechecks_permission_and_ttl(env):
    env.document()
    docs = ready_documents(env.owner, env.kb)
    registry = MetadataCatalog(env.sv.vectors, ttl=0)
    assert not registry.snapshot(env.owner, env.kb, docs)["cache_hit"]
    assert not registry.snapshot(env.owner, env.kb, docs)["cache_hit"]
    with pytest.raises(Problem):
        registry.snapshot(env.other, env.kb, docs)
    with db.connection() as con:
        con.execute("UPDATE kbs SET owner=? WHERE id=?", (env.other, env.kb))
    with pytest.raises(Problem):
        registry.snapshot(env.owner, env.kb, docs)


def test_catalog_refresh_and_missing_revision_fail_closed(env):
    doc = env.document()
    snapshot(env)
    assert snapshot(env)["cache_hit"]
    assert not snapshot(env, refresh=True)["cache_hit"]
    env.sv.vectors.clear_revision(doc["id"], doc["index_revision"])
    env.sv.vectors.finish()
    with pytest.raises(Problem, match="缺少"):
        snapshot(env, refresh=True)


def test_catalog_batches_every_document_without_scanning_vectors(env, monkeypatch):
    first = env.document(metadata={"attributes": {"arbitrary_key": ["value"]}})
    row = env.sv.vectors.client.get(
        COLLECTION,
        ids=[f"{first['id']}_{first['index_revision']}_1"],
        output_fields=["*"],
    )[0]
    records = []
    with db.connection() as con:
        for i in range(129):
            did = db.uid()
            con.execute(
                "INSERT INTO docs(id,kb,filename,sha,created,status,n_chunks,index_version,index_revision,metadata_json,metadata_provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    env.kb,
                    f"{i}.pdf",
                    did,
                    1,
                    "ready",
                    1,
                    3,
                    first["index_revision"],
                    first["metadata_json"],
                    first["metadata_provenance"],
                ),
            )
            records.append(
                row | {"id": f"{did}_{first['index_revision']}_1", "doc": did}
            )
    env.sv.vectors.add(records)
    env.sv.vectors.finish()
    original = env.sv.vectors.client.query
    queries = []

    def query(*args, **kwargs):
        queries.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(env.sv.vectors.client, "query", query)
    result = snapshot(env)
    assert result["indexed_documents"] == 130
    assert len(queries) == 2
    assert all(
        "vec" not in q["output_fields"] and "text" not in q["output_fields"]
        for q in queries
    )
    assert all(
        q["limit"] <= 128 and env.owner in q["filter"] and env.kb in q["filter"]
        for q in queries
    )


def test_schema_contract_accepts_pdf_semantic_query_and_compiles(env):
    env.document(metadata={"attributes": {"retention_class": ["长期"]}})
    fields = snapshot(env)["filterable_fields"]
    payload = build_intent_payload(
        "长期资料", fields, [], db.retrieval_context(env.owner, env.kb)
    )
    assert "semantic_query" in payload["output_schema"]["properties"]
    parsed = IntentOutput.model_validate(
        {
            "intent": "lookup",
            "semantic_query": "长期归档资料",
            "confidence": 0.9,
            "filters": {
                "conditions": [
                    {
                        "field": "attributes.retention_class",
                        "operator": "contains_any",
                        "values": ["长期"],
                    }
                ]
            },
        }
    )
    assert parsed.search_query == "长期归档资料"
    assert (
        'json_contains_any(attributes["retention_class"], ["长期"])'
        in build_filter_expression(parsed.filters, fields)
    )


def test_inferred_filter_does_not_leak_unfiltered_legacy_results(env):
    env.document(metadata={"attributes": {"team": ["研发"]}})
    env.document(filename="legacy.pdf", legacy=True)
    env.sv.agent_completion = lambda *a, **k: json.dumps(
        {
            "intent": "lookup",
            "semantic_query": "财务资料",
            "confidence": 0.95,
            "filters": {
                "conditions": [
                    {
                        "field": "attributes.team",
                        "operator": "contains_any",
                        "values": ["财务"],
                        "evidence": {"source": "query", "quote": "财务"},
                    }
                ]
            },
        }
    )
    result = ask(env, question="财务资料", planner="llm", mode="search")
    assert result["sources"] == []
    assert all(t["index_version"] == 3 for t in result["retrieval_trace"])


def crew_model(env, verdict="supported", bad_id=False):
    calls = []

    def call(task, payload, **kwargs):
        calls.append((task, payload))
        assert task in ("evidence_analyst", "evidence_reviewer")
        eid = payload["evidence"][0]["id"]
        answer = {
            "answer": "保留90天。DRAFT_MARKER",
            "evidence_ids": [9999 if bad_id else eid],
        }
        if task == "evidence_reviewer":
            assert "DRAFT_MARKER" in json.dumps(
                payload["crew_messages"], ensure_ascii=False
            )
            answer.update(answer="保留90天。", verdict=verdict, issues=[])
        return json.dumps(answer)

    env.sv.agent_completion = call
    return calls


def test_real_crewai_runs_analysis_then_review_inside_langgraph(env):
    good = env.document(text="原始科学帧保留90天。")
    other_kb = db.uid()
    with db.connection() as con:
        con.execute("INSERT INTO kbs VALUES(?,?,?)", (other_kb, env.other, "private"))
    env.document(doc_kb=other_kb, text="SECRET_NEVER_SEND")
    calls = crew_model(env)
    result = ask(env)
    assert result["status"] == "answered", result
    assert result["answer_engine"] == "crewai" and result["review"] == "supported"
    assert {s["doc"] for s in result["sources"]} == {good["id"]}
    assert [t for t, p in calls] == ["evidence_analyst", "evidence_reviewer"]
    assert "SECRET_NEVER_SEND" not in json.dumps(calls)
    assert [t["node"] for t in result["agent_trace"]] == [
        "metadata_catalog",
        "intent",
        "retrieve",
        "rerank",
        "crew_review",
        "explain",
    ]
    assert len(result["crew_trace"]) == 2


def test_crewai_insufficient_review_does_not_publish_draft(env):
    env.document()
    crew_model(env, verdict="insufficient")
    result = ask(env)
    assert result["status"] == "insufficient"
    assert result["sources"] == [] and "DRAFT_MARKER" not in result["answer"]


def test_crewai_forged_citation_fails_without_unreviewed_fallback(env):
    env.document()
    calls = crew_model(env, bad_id=True)
    result = ask(env)
    assert result["status"] == "review_failed"
    assert result["sources"] == []
    assert all(task == "evidence_analyst" for task, payload in calls)


def test_search_mode_never_spends_crewai_calls(env):
    env.document()
    calls = crew_model(env)
    result = ask(env, mode="search")
    assert result["status"] == "retrieved" and calls == []
    assert "crew_review" not in [t["node"] for t in result["agent_trace"]]


def test_crewai_call_budget_is_bounded(env):
    from app.crew_agents import InternalLLM

    env.sv.agent_completion = lambda *a, **k: '{"answer":"test","evidence_ids":[1]}'
    llm = InternalLLM(env.sv, "evidence_analyst", "q", [{"id": 1}], [])
    for _ in range(3):
        llm.call("test")
    with pytest.raises(Problem, match="预算"):
        llm.call("test")
