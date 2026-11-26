import json

import pytest
from pydantic import ValidationError

from app.intent import (
    Filters,
    LegacyIntentAgent as IntentAgent,
    compile_filters,
    rule_plan,
)
from app.metadata import DocumentMetadata, checked_llm_proposal, propose


def test_metadata_distinguishes_validity_and_acceptance():
    p = propose(
        "exception.pdf",
        [
            {
                "text": "例外核准单\n南站定标帧 SN-609 首次验收日期 2026-06-09，保留 75 天。",
                "page": 2,
            }
        ],
    )
    assert p["metadata"]["doc_type"] == "exception"
    assert p["metadata"]["locations"] == []
    assert p["metadata"]["effective_from"] == ""
    assert p["metadata"]["effective_to"] == ""
    assert p["metadata"]["entity_ids"] == ["SN-609"]
    assert p["provenance"]["entity_ids"][0]["page"] == 2


def test_metadata_rejects_invalid_dates_and_system_fields():
    with pytest.raises(ValidationError):
        DocumentMetadata(effective_from="2026-02-30")
    with pytest.raises(ValidationError):
        DocumentMetadata(owner="someone-else")
    with pytest.raises(ValidationError):
        DocumentMetadata(effective_from="2026-06-01", effective_to="2026-05-31")


def test_llm_proposal_requires_verbatim_evidence():
    chunks = [{"text": "版本 2.0；生效日期：2026-06-01", "page": 1}]
    base = propose("example.pdf", chunks)
    result = checked_llm_proposal(
        json.dumps(
            {
                "updates": {"organization": "编造单位"},
                "evidence": {"organization": [{"page": 1, "quote": "原文不存在"}]},
            }
        ),
        base,
        chunks,
    )
    assert not result["metadata"]["organization"]


def test_rules_comparison_and_acceptance_date():
    p = rule_plan("比较 V1.3 和 V2.0 的定标帧保留时间，SN-609 于 2026-06-09 验收", {})
    assert p["filters"]["versions"] == ["1.3", "2.0"]
    assert "effective_at" not in p["filters"]
    assert p["needs_policy_context"]


def test_no_raw_expression_or_acl_overrides():
    for data in (
        {"filter": "owner != ''"},
        {"owner": "x"},
        {"doc_types": ["policy or 1==1"]},
    ):
        with pytest.raises(ValidationError):
            Filters.model_validate(data)
    compiled = compile_filters(Filters(tags=['x" or owner != "']))
    assert '\\"' in compiled


def test_invalid_llm_and_low_confidence_fall_back():
    from types import SimpleNamespace

    sv = SimpleNamespace(
        cfg={"chat_base": "local", "chat_model": "test"},
        intent_completion=lambda *a: '{"filter":"1==1"}',
    )
    p = IntentAgent(sv).plan("SN-609 保留多久", [], {"doc_types": ["policy"]})
    assert p["planner"] == "rules"
    assert p["explicit_filters"] == {"doc_types": ["policy"]}
    assert p["warnings"]


def test_llm_cannot_add_unmentioned_filters():
    from types import SimpleNamespace

    obj = {
        "intent": "lookup",
        "confidence": 0.99,
        "filters": {"sites": ["north"]},
        "needs_policy_context": False,
    }
    sv = SimpleNamespace(
        cfg={"chat_base": "local", "chat_model": "test"},
        intent_completion=lambda *a: json.dumps(obj),
    )
    p = IntentAgent(sv).plan(
        "什么是定标", [{"metadata_json": json.dumps({"sites": ["north"]})}]
    )
    assert all("sites" not in r["inferred"] for r in p["routes"])


def test_policy_mentioning_ledgers_and_exceptions_stays_policy():
    p = propose(
        "02_policy_v20.pdf",
        [
            {
                "page": 1,
                "text": "ARCHIVE POLICY\n档案规范 V2.0\n遇到例外核准单按台账中的首次验收日期匹配。",
            }
        ],
    )
    assert p["metadata"]["doc_type"] == "policy"
    assert p["metadata"]["version"] == "2.0"
