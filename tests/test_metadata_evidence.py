import copy
import json

import pytest

from app import db
from app.metadata_agent import discover, source_quote
from app.parsing import Problem


def proposed_field(quote="核准保留期限：75 天", page=8):
    return {
        "fields": [
            {
                "key": "retention_period",
                "label": "提及保留期限",
                "description": "文档提及的期限，不能代替每一对象的适用规则",
                "values": ["75 天"],
                "evidence": [
                    {"value": "75 天", "chunk_id": 0, "page": page, "quote": quote}
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    ("quote", "text", "expected"),
    [
        ("期限：75 天", "核准期限：75 天。", "期限：75 天"),
        ("期限： 75 天", "核准期限：\n75\t天。", "期限：\n75\t天"),
        ("期限： 75 天", "核准期限：\u00a075\r\n天。", "期限：\u00a075\r\n天"),
        ("期限：120 天", "期限：75 天", None),
        ("期限75天", "期限 75 天", None),
        ("notapplicable", "not applicable", None),
        ("期限： 75 天", "期限：" + " " * 900 + "75 天", None),
    ],
)
def test_source_quote_only_accepts_existing_whitespace_runs(quote, text, expected):
    assert source_quote(quote, text) == expected


def test_discovery_keeps_original_whitespace_in_provenance(env):
    doc = env.document()
    source = "核准保留期限：\n75\t天"
    calls = []

    def model(task, payload, **kwargs):
        calls.append(payload)
        return json.dumps(proposed_field("核准保留期限： 75 天"))

    env.sv.agent_completion = model
    result = discover(env.sv, doc, [{"page": 8, "text": source}], preserve=False)
    assert result["metadata"]["attributes"]["retention_period"] == ["75 天"]
    assert result["provenance"]["attributes.retention_period"][0]["quote"] == source
    assert result["model_calls"] == 1
    assert result["repair_attempts"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"page": 1}, "page_mismatch"),
        ({"chunk_id": 99}, "chunk_not_in_batch"),
        ({"value": "75天"}, "missing_value_evidence"),
        ({"quote": "核准保留期限：120 天"}, "quote_not_in_chunk"),
    ],
)
def test_discovery_repairs_reference_with_feedback_once(env, change, reason):
    doc = env.document()
    good = proposed_field()
    bad = copy.deepcopy(good)
    bad["fields"][0]["evidence"][0].update(change)
    calls = []

    def model(task, payload, **kwargs):
        assert task == "discover_metadata"
        calls.append(payload)
        if len(calls) == 1:
            assert "evidence_repair" not in payload
            return json.dumps(bad)
        repair = payload["evidence_repair"]
        assert repair["previous_output"] == bad
        assert repair["validation_errors"] == [
            {"field": "retention_period", "value": "75 天", "reasons": [reason]}
        ]
        assert payload["chunks"] == calls[0]["chunks"]
        return json.dumps(good)

    env.sv.agent_completion = model
    result = discover(
        env.sv, doc, [{"page": 8, "text": "核准保留期限：75 天"}], preserve=False
    )
    assert result["model_calls"] == 2
    assert result["repair_attempts"] == 1
    assert result["provenance"]["attributes.retention_period"][0]["page"] == 8
    assert "自动修正" in result["notice"]


def test_unverified_value_can_be_removed_by_repair_without_publishing(env):
    doc = env.document()
    first = proposed_field()
    first["fields"][0]["values"].append("120 天")
    replies = iter([first, proposed_field()])
    env.sv.agent_completion = lambda *a, **k: json.dumps(next(replies))
    result = discover(
        env.sv, doc, [{"page": 8, "text": "核准保留期限：75 天"}], preserve=False
    )
    assert result["metadata"]["attributes"]["retention_period"] == ["75 天"]
    assert result["repair_attempts"] == 1
    with db.connection() as con:
        saved = dict(con.execute("SELECT * FROM docs WHERE id=?", (doc["id"],)).fetchone())
    assert saved["metadata_json"] == doc["metadata_json"]
    assert saved["index_revision"] == doc["index_revision"]


def test_repair_budget_keeps_rejecting_fabricated_quotes_and_old_data(env):
    doc = env.document()
    bad = proposed_field("核准保留期限：120 天")
    calls = []

    def model(task, payload, **kwargs):
        calls.append(payload)
        return json.dumps(bad)

    env.sv.agent_completion = model
    with pytest.raises(Problem, match="有效原文依据：retention_period.*自动修正一次"):
        discover(env.sv, doc, [{"page": 8, "text": "核准保留期限：75 天"}], preserve=False)
    assert len(calls) == 2
    with db.connection() as con:
        saved = dict(con.execute("SELECT * FROM docs WHERE id=?", (doc["id"],)).fetchone())
    assert saved["metadata_json"] == doc["metadata_json"]
    assert saved["index_revision"] == doc["index_revision"]


def test_quote_must_not_span_two_chunks_on_same_page(env):
    doc = env.document()
    calls = []

    def model(task, payload, **kwargs):
        calls.append(payload)
        return json.dumps(proposed_field("原始科学帧 保留 75 天"))

    env.sv.agent_completion = model
    with pytest.raises(Problem, match="引用文字无法"):
        discover(
            env.sv,
            doc,
            [{"page": 8, "text": "原始科学帧"}, {"page": 8, "text": "保留 75 天"}],
        )
    assert len(calls) == 2


def test_reserved_field_is_rejected_without_repair(env):
    doc = env.document()
    bad = proposed_field()
    bad["fields"][0]["key"] = "owner"
    calls = []

    def model(task, payload, **kwargs):
        calls.append(payload)
        return json.dumps(bad)

    env.sv.agent_completion = model
    with pytest.raises(Problem, match="保留字段"):
        discover(env.sv, doc, [{"page": 8, "text": "核准保留期限：75 天"}])
    assert len(calls) == 1
