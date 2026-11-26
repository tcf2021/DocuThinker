import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import db
from app.config import COLLECTION, PREVIOUS_COLLECTION, VECTOR_DIM
from app.graphs import qa_graph
from app.indexing import reindex_graph
from app.intent import (
    Filters,
    LegacyIntentAgent as IntentAgent,
    catalog,
    compile_filters,
)
from app.metadata import DocumentMetadata, checked_llm_proposal, propose


def ask(env, question="资料", filters=None):
    return qa_graph(env.sv).invoke(
        {
            "owner": env.owner,
            "kb": env.kb,
            "question": question,
            "mode": "search",
            "filters": filters or {},
            "planner": "rules",
        }
    )["result"]


@pytest.mark.parametrize(
    "filename,text,kind,attrs",
    [
        (
            "hr.pdf",
            "员工报销制度 V2.0\n主题：差旅报销\n适用部门：研发部\n报销类型：差旅费\n生效日期：2026-01-01",
            "policy",
            {"department": ["研发部"], "expense_type": ["差旅费"]},
        ),
        (
            "product.pdf",
            "产品操作手册 V1.1\n产品型号：ZX-100\n地点：深圳\n属性 firmware: 3.2",
            "manual",
            {"product": ["ZX-100"], "firmware": ["3.2"]},
        ),
        (
            "contract.pdf",
            "采购合同\n所属单位：示例公司\n编号：2026008\n属性 supplier: 示例供应商",
            "contract",
            {"supplier": ["示例供应商"]},
        ),
    ],
)
def test_one_contract_handles_unrelated_domains(filename, text, kind, attrs):
    result = propose(filename, [{"text": text, "page": 1}])
    meta = result["metadata"]
    assert meta["doc_type"] == kind
    assert meta["attributes"] == attrs
    assert set(meta) == set(DocumentMetadata.model_fields)
    assert "sites" not in meta and "data_types" not in meta
    for key in attrs:
        assert result["provenance"]["attributes." + key]


def test_rules_do_not_invent_domain_or_applicability():
    meta = propose(
        "notes.pdf", [{"text": "天文台南站定标数据，本说明不适用于北站。", "page": 1}]
    )["metadata"]
    assert meta["locations"] == [] and meta["topics"] == []
    assert meta["organization"] == "" and meta["attributes"] == {}
    assert meta["doc_type"] == "other"


def test_schema_rejects_wrong_types_and_attribute_key_injection():
    for attrs in [
        {"department": "研发部"},
        {"price": [100]},
        {"department": []},
        {"department": [""]},
        {'x"] or owner != "': ["a"]},
        {"owner": ["someone"]},
        {"price": {"gte": 100}},
    ]:
        with pytest.raises(ValidationError):
            DocumentMetadata(attributes=attrs)
        with pytest.raises(ValidationError):
            Filters(attributes=attrs)
    with pytest.raises(ValidationError):
        DocumentMetadata(department="研发部")
    assert DocumentMetadata(
        attributes={"department": ["研发部", "研发部"]}
    ).attributes == {"department": ["研发部"]}


def test_legacy_names_migrate_without_losing_custom_values():
    old = {
        "title": "人工审核标题",
        "sites": ["south", "自定义站点"],
        "locations": ["上海"],
        "data_types": ["calibration"],
        "tags": ["已审核"],
        "organization": "人工机构",
    }
    new = DocumentMetadata.model_validate(old).model_dump()
    assert new["locations"] == ["上海", "南站", "自定义站点"]
    assert new["topics"] == ["定标"]
    assert new["tags"] == old["tags"] and new["title"] == old["title"]
    assert compile_filters({"sites": ["south"]}) == compile_filters(
        {"locations": ["南站"]}
    )


def test_llm_attributes_need_separate_source_evidence():
    chunks = [{"text": "员工制度\n适用部门：研发部", "page": 1}]
    base = propose("hr.pdf", chunks)
    raw = {
        "updates": {"attributes": {"department": ["研发部"], "product": ["编造产品"]}},
        "evidence": {
            "attributes.department": [{"page": 1, "quote": "适用部门：研发部"}],
            "attributes.product": [{"page": 1, "quote": "不存在"}],
        },
    }
    result = checked_llm_proposal(json.dumps(raw), base, chunks)
    assert result["metadata"]["attributes"] == {"department": ["研发部"]}
    assert result["method"] == "llm_proposal"
    assert "attributes.department" in result["provenance"]


def test_intent_uses_shared_attributes_and_respects_explicit_keys():
    documents = [
        {
            "metadata_json": json.dumps(
                {
                    "attributes": {
                        "department": ["研发部", "财务部"],
                        "product": ["ZX100"],
                    }
                }
            )
        }
    ]
    available = catalog(documents)
    assert available["attributes"]["department"] == ["研发部", "财务部"]
    sv = SimpleNamespace(cfg={})
    plan = IntentAgent(sv).plan(
        "研发部的 ZX100 资料",
        documents,
        {"attributes": {"department": ["财务部"]}},
        "rules",
    )
    assert plan["explicit_filters"] == {"attributes": {"department": ["财务部"]}}
    assert plan["routes"][0]["inferred"]["attributes"] == {"product": ["ZX100"]}


def test_llm_cannot_invent_attribute_keys_or_values():
    docs = [
        {
            "metadata_json": json.dumps(
                {"attributes": {"department": ["研发部"], "product": ["ZX100"]}}
            )
        }
    ]
    sv = SimpleNamespace(
        cfg={"chat_base": "test", "chat_model": "test"},
        intent_completion=lambda *args: json.dumps(
            {
                "confidence": 0.99,
                "filters": {
                    "attributes": {
                        "department": ["研发部"],
                        "unapproved": ["ZX100"],
                        "product": ["ZX100"],
                    }
                },
            }
        ),
    )
    plan = IntentAgent(sv).plan("研发部资料", docs)
    assert plan["routes"][0]["inferred"]["attributes"] == {"department": ["研发部"]}


def test_real_milvus_attribute_and_or_and_missing(env):
    good = env.document(
        metadata={
            "attributes": {"department": ["研发部"], "product": ["ZX100"]},
            "topics": ["售后"],
            "locations": ["上海"],
        }
    )
    env.document(
        filename="other.pdf",
        metadata={"attributes": {"department": ["财务部"], "product": ["ZX100"]}},
    )
    env.document(filename="missing.pdf")
    filters = {
        "attributes": {"department": ["研发部", "销售部"], "product": ["ZX100"]},
        "topics": ["售后"],
        "locations": ["上海"],
    }
    result = ask(env, filters=filters)
    assert {r["doc"] for r in result["sources"]} == {good["id"]}
    assert any(
        'attributes["department"]' in expr
        for t in result["retrieval_trace"]
        for expr in t["expressions"]
    )
    assert not ask(env, filters={"attributes": {"nonexistent": ["x"]}})["sources"]
    assert not ask(env, filters={"attributes": {"department": ['x" or owner != "']}})[
        "sources"
    ]
    assert not ask(
        env, "SN-999", filters={"attributes": {"department": ["不存在的部门"]}}
    )["sources"]
    inferred = ask(env, "研发部 ZX100 的售后资料")
    assert {r["doc"] for r in inferred["sources"]} == {good["id"]}


def test_real_milvus_attribute_isolation_and_snapshot(env):
    d = env.document(metadata={"attributes": {"department": ["研发部"]}})
    private = db.uid()
    with db.connection() as con:
        con.execute("INSERT INTO kbs VALUES(?,?,?)", (private, env.other, "private"))
    env.document(doc_kb=private, metadata={"attributes": {"department": ["研发部"]}})
    result = ask(env, filters={"attributes": {"department": ["研发部"]}})
    assert {r["doc"] for r in result["sources"]} == {d["id"]}
    new = json.loads(d["metadata_json"]) | {"attributes": {"department": ["财务部"]}}
    with db.connection() as con:
        db.enqueue_index(con, d["id"], json.dumps(new))
    assert ask(env, filters={"attributes": {"department": ["研发部"]}})["sources"]
    assert not ask(env, filters={"attributes": {"department": ["财务部"]}})["sources"]
    reindex_graph(env.sv).invoke({"job": db.claim_index()})
    assert not ask(env, filters={"attributes": {"department": ["研发部"]}})["sources"]
    assert ask(env, filters={"attributes": {"department": ["财务部"]}})["sources"]


def make_v2_document(env):
    from pymilvus import DataType

    d = env.document()
    old = {
        "title": "人工审核标题",
        "doc_type": "policy",
        "organization": "人工机构",
        "version": "9.8",
        "sites": ["south"],
        "data_types": ["calibration"],
        "tags": ["人工标签"],
    }
    client = env.sv.vectors.client
    if not client.has_collection(PREVIOUS_COLLECTION):
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        for name, size in [
            ("id", 120),
            ("kb", 32),
            ("owner", 32),
            ("doc", 32),
            ("revision", 32),
            ("text", 8192),
            ("doc_type", 32),
            ("version", 160),
            ("organization", 480),
        ]:
            schema.add_field(
                name, DataType.VARCHAR, is_primary=name == "id", max_length=size
            )
        for name in ("page", "effective_from", "effective_to"):
            schema.add_field(name, DataType.INT64)
        schema.add_field("metadata", DataType.JSON)
        schema.add_field("vec", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="vec", index_type="FLAT", metric_type="COSINE")
        client.create_collection(
            PREVIOUS_COLLECTION, schema=schema, index_params=indexes
        )
    row = client.query(COLLECTION, filter=f'doc == "{d["id"]}"', output_fields=["*"])[0]
    row.pop("attributes")
    row.update(metadata=old, organization=old["organization"], version=old["version"])
    client.upsert(PREVIOUS_COLLECTION, [row])
    client.flush(PREVIOUS_COLLECTION)
    with db.connection() as con:
        con.execute(
            "UPDATE docs SET index_version=2,metadata_json=?,metadata_provenance=? WHERE id=?",
            (
                json.dumps(old),
                json.dumps({"method": "manual", "metadata": old}),
                d["id"],
            ),
        )
    return d


def test_v2_remains_readable_migration_preserves_manual_values_and_rollback(env):
    d = make_v2_document(env)
    assert ask(env)["sources"][0]["index_version"] == 2
    assert not ask(env, filters={"locations": ["南站"]})["sources"]
    with db.connection() as con:
        db.enqueue_index(con, d["id"])
    reindex_graph(env.sv).invoke({"job": db.claim_index()})
    result = ask(
        env, filters={"locations": ["南站"], "topics": ["定标"], "tags": ["人工标签"]}
    )
    assert result["sources"][0]["index_version"] == 3
    meta = result["sources"][0]["metadata"]
    assert meta["title"] == "人工审核标题" and meta["version"] == "9.8"
    assert meta["organization"] == "人工机构"
    with db.connection() as con:
        current = dict(
            con.execute("SELECT * FROM docs WHERE id=?", (d["id"],)).fetchone()
        )
        old = dict(
            con.execute(
                "SELECT * FROM index_history WHERE doc=? AND index_version=2",
                (d["id"],),
            ).fetchone()
        )
        con.execute(
            "UPDATE docs SET index_version=2,index_revision=?,metadata_json=?,metadata_provenance=? WHERE id=?",
            (
                old["revision"],
                old["metadata_json"],
                old["metadata_provenance"],
                d["id"],
            ),
        )
    assert current["index_version"] == 3
    assert ask(env)["sources"][0]["index_version"] == 2
    assert (
        env.sv.vectors.client.query(
            PREVIOUS_COLLECTION,
            filter=f'doc == "{d["id"]}"',
            output_fields=["count(*)"],
        )[0]["count(*)"]
        == 1
    )
