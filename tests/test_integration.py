import json

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.config import LEGACY_COLLECTION
from app.graphs import qa_graph, ingestion_graph
from app.indexing import reindex_graph
from app.intent import literal


def ask(env, query="保留多少天？", **kw):
    return qa_graph(env.sv).invoke(
        {"owner": env.owner, "kb": env.kb, "question": query, "mode": "search", **kw}
    )["result"]


def test_real_milvus_filter_owner_version_and_json(env):
    good = env.document(
        metadata={"version": "2.0", "sites": ["south"], "tags": ["approved"]}
    )
    env.document(filename="v1.pdf", metadata={"version": "1.3", "sites": ["south"]})
    other_kb = db.uid()
    with db.connection() as con:
        con.execute("INSERT INTO kbs VALUES(?,?,?)", (other_kb, env.other, "private"))
    env.document(doc_kb=other_kb, metadata={"version": "2.0", "sites": ["south"]})
    result = ask(
        env, filters={"versions": ["2.0"], "sites": ["south"], "tags": ["approved"]}
    )
    assert {r["doc"] for r in result["sources"]} == {good["id"]}
    assert result["sources"][0]["metadata"]["schema_version"] == 3
    assert all(
        env.owner in expression
        for t in result["retrieval_trace"]
        for expression in t["expressions"]
    )


def test_strict_filters_never_relaxed_and_inference_recovers(env):
    env.document()
    result = ask(env, "SN-999 的记录", filters={"versions": ["9.9"]})
    assert not result["sources"]
    assert len(result["retrieval_trace"]) >= 2
    assert all(
        "9.9" in expression
        for t in result["retrieval_trace"]
        for expression in t["expressions"]
    )
    broad = ask(env, "SN-999 的记录")
    assert broad["sources"]
    assert any("重试" in w for w in broad["intent"]["warnings"])


def test_real_milvus_injection_value_is_literal(env):
    env.document(metadata={"tags": ["normal"]})
    assert not ask(env, filters={"tags": ['x" or owner != "']})["sources"]


def test_effective_filter_requires_known_boundaries(env):
    known = env.document(
        metadata={"effective_from": "2026-01-01", "effective_to": "2026-06-30"}
    )
    env.document(filename="unknown.pdf")
    assert {
        r["doc"] for r in ask(env, filters={"effective_at": "2026-06-09"})["sources"]
    } == {known["id"]}


def test_reindex_keeps_old_live_until_publication_and_supports_retry(env):
    d = env.document(legacy=True)
    with db.connection() as con:
        job_info = db.enqueue_index(con, d["id"])
    job = db.claim_index()
    assert ask(env)["sources"][0]["index_version"] == 1
    original = env.sv.embed.embed_documents
    env.sv.embed.embed_documents = lambda texts: (_ for _ in ()).throw(
        RuntimeError("embedding failure")
    )
    with pytest.raises(RuntimeError):
        reindex_graph(env.sv).invoke({"job": job})
    assert ask(env)["sources"][0]["index_version"] == 1
    env.sv.embed.embed_documents = original
    reindex_graph(env.sv).invoke({"job": job})
    result = ask(env)
    assert result["sources"][0]["index_version"] == 3
    assert (
        env.sv.vectors.client.query(
            LEGACY_COLLECTION,
            filter=f"doc == {literal(d['id'])}",
            output_fields=["count(*)"],
        )[0]["count(*)"]
        == 1
    )
    with db.connection() as con:
        new = dict(con.execute("SELECT * FROM docs WHERE id=?", (d["id"],)).fetchone())
        assert new["index_revision"] == job_info["id"]
        assert (
            con.execute(
                "SELECT status FROM index_jobs WHERE id=?", (job_info["id"],)
            ).fetchone()[0]
            == "ready"
        )


def test_metadata_edit_switches_snapshot_only_when_done(env):
    d = env.document(metadata={"tags": ["old"]})
    new_meta = json.loads(d["metadata_json"]) | {"tags": ["new"]}
    with db.connection() as con:
        db.enqueue_index(con, d["id"], json.dumps(new_meta))
    assert ask(env, filters={"tags": ["old"]})["sources"]
    assert not ask(env, filters={"tags": ["new"]})["sources"]
    job = db.claim_index()
    reindex_graph(env.sv).invoke({"job": job})
    assert not ask(env, filters={"tags": ["old"]})["sources"]
    assert ask(env, filters={"tags": ["new"]})["sources"]
    assert env.sv.vectors.count_revision(d["id"], d["index_revision"]) == 1


def test_cross_document_support_and_version_comparison(env):
    ledger = env.document(
        filename="ledger.pdf",
        text="批次台账\nSN-609 南站定标帧首次验收 2026-06-09。",
        metadata={"doc_type": "ledger"},
    )
    policy = env.document(metadata={"doc_type": "policy", "version": "2.0"})
    exception = env.document(
        filename="exception.pdf",
        text="例外核准\n南站定标帧保留 75 天。",
        metadata={"doc_type": "exception"},
    )
    result = ask(env, "SN-609 的保留期适用哪份制度？")
    assert {r["doc"] for r in result["sources"]} == {
        ledger["id"],
        policy["id"],
        exception["id"],
    }
    old = env.document(filename="v1.pdf", metadata={"version": "1.3"})
    comparison = ask(env, "比较 V1.3 和 V2.0 的保留期限")
    assert {old["id"], policy["id"]} <= {r["doc"] for r in comparison["sources"]}


def test_new_ingestion_stores_metadata_vectors_and_original_evidence(env, monkeypatch):
    did = db.uid()
    (env.root / "spool" / did).write_bytes(b"%PDF-test")
    with db.connection() as con:
        con.execute(
            "INSERT INTO docs(id,kb,filename,sha,created) VALUES(?,?,?,?,?)",
            (did, env.kb, "receipt.pdf", did, 1),
        )
    monkeypatch.setattr(
        "app.graphs.parse_pdf",
        lambda *args: {
            "md": "交接回执 SN-609",
            "blocks": [
                {
                    "type": "text",
                    "text": "交接回执 SN-609\n南站封条号 LX-7316",
                    "page_idx": 0,
                }
            ],
            "images": {},
        },
    )
    ingestion_graph(env.sv).invoke({"doc": db.claim()})
    result = ask(
        env,
        "SN-609 的封条号",
        filters={
            "conditions": [
                {
                    "field": "attributes.doc_type",
                    "operator": "contains_any",
                    "values": ["receipt"],
                }
            ]
        },
    )
    assert result["sources"][0]["metadata"]["attributes"]["doc_type"] == ["receipt"]
    assert "LX-7316" in result["sources"][0]["text"]
    assert result["sources"][0]["page"] == 1
    assert env.sv.store.files[f"agent-kb/{env.kb}/{did}/source.pdf"] == b"%PDF-test"


def test_http_auth_edit_jobs_history_rollback_and_legacy_filter(env, monkeypatch):
    d = env.document(legacy=True)
    monkeypatch.setattr(main.app.state, "sv", env.sv, raising=False)
    monkeypatch.setattr(main.app.state, "qa", qa_graph(env.sv), raising=False)
    client = TestClient(main.app)
    assert client.get(f"/api/documents/{d['id']}").status_code == 401
    main.app.dependency_overrides[main.account] = lambda: {
        "id": env.other,
        "name": "two",
    }
    try:
        for suffix in ("", "/files/source.pdf"):
            assert client.get(f"/api/documents/{d['id']}" + suffix).status_code == 404
        for suffix in ("/reindex", "/metadata/propose"):
            assert (
                client.post(f"/api/documents/{d['id']}" + suffix, json={}).status_code
                == 404
            )
        assert (
            client.post(f"/api/kbs/{env.kb}/ask", json={"question": "test"}).status_code
            == 404
        )
        main.app.dependency_overrides[main.account] = lambda: {
            "id": env.owner,
            "name": "one",
        }
        r = client.post(
            f"/api/kbs/{env.kb}/ask",
            json={
                "question": "test",
                "mode": "search",
                "filters": {"sites": ["south"]},
            },
        ).json()
        assert not r["sources"]
        assert any("旧文档" in w for w in r["intent"]["warnings"])
        r = client.post(f"/api/kbs/{env.kb}/reindex")
        assert r.status_code == 202
        job = db.claim_index()
        reindex_graph(env.sv).invoke({"job": job})
        detail = client.get(f"/api/documents/{d['id']}").json()
        assert detail["index_version"] == 3
        assert detail["metadata"]["attributes"]["version"] == ["2.0"]
        stale = client.put(
            f"/api/documents/{d['id']}/metadata",
            json={"metadata": detail["metadata"], "expected_revision": ""},
        )
        assert stale.status_code == 409
        back = client.post(
            f"/api/documents/{d['id']}/index/rollback",
            json={"revision": "", "expected_revision": detail["index_revision"]},
        )
        assert back.status_code == 200
        assert client.get(f"/api/documents/{d['id']}").json()["index_version"] == 1
    finally:
        main.app.dependency_overrides.clear()
        client.close()


def test_queue_idempotence_and_restart_recovery(env):
    d = env.document()
    with db.connection() as con:
        a = db.enqueue_index(con, d["id"])
        b = db.enqueue_index(con, d["id"])
    assert a["id"] == b["id"] and b["duplicate"]
    job = db.claim_index()
    db.requeue_interrupted()
    recovered = db.claim_index()
    assert recovered["id"] == job["id"]
    reindex_graph(env.sv).invoke({"job": recovered})
    assert ask(env)["sources"]
    db.init_db()  # Idempotent schema upgrade must preserve data.
    assert ask(env)["sources"]


def test_worker_marks_failure_but_does_not_unpublish(env):
    import time
    from app.graphs import Worker

    d = env.document()
    with db.connection() as con:
        db.enqueue_index(con, d["id"])

    class BrokenGraph:
        def invoke(self, state):
            raise RuntimeError("private upstream text must not leak")

    worker = Worker(None, BrokenGraph())
    worker.thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with db.connection() as con:
                row = con.execute(
                    "SELECT status,error FROM index_jobs WHERE doc=?", (d["id"],)
                ).fetchone()
            if row["status"] == "failed":
                break
            time.sleep(0.05)
        assert row["status"] == "failed"
        assert "private upstream" not in row["error"]
        assert ask(env)["sources"]
    finally:
        worker.stop.set()
        worker.thread.join(timeout=3)


def test_answer_citations_refer_to_exact_filtered_evidence(env):
    env.document(metadata={"version": "2.0"})
    env.sv.chat = lambda question, evidence: json.dumps(
        {"answer": "保留 90 天。", "evidence_ids": [evidence[-1]["id"]]}
    )
    result = qa_graph(env.sv).invoke(
        {
            "owner": env.owner,
            "kb": env.kb,
            "question": "保留多久",
            "mode": "chat",
            "filters": {"versions": ["2.0"]},
        }
    )["result"]
    assert result["status"] == "answered"
    assert result["sources"][0]["quote"] in result["sources"][0]["text"]
    assert result["sources"][0]["metadata"]["version"] == "2.0"
    assert result["intent"]["explicit_filters"] == {"versions": ["2.0"]}
