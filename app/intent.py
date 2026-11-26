"""Intent Agent emits typed search plans, never executable Milvus expressions."""

import json
from .classify_intent import build_intent_payload, classify_intent
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .field_schema import (
    Predicate,
    field_catalog,
    compile_dynamic,
)
from .config import INDEX_VERSION
from .metadata import (
    DocType,
    AttributeMap,
    clean_attributes,
    migrate_legacy_fields,
)


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    doc_types: list[DocType] = Field(default_factory=list, max_length=16)
    versions: list[str] = Field(default_factory=list, max_length=12)
    organizations: list[str] = Field(default_factory=list, max_length=12)
    locations: list[str] = Field(default_factory=list, max_length=12)
    topics: list[str] = Field(default_factory=list, max_length=12)
    entities: list[str] = Field(default_factory=list, max_length=12)
    entity_ids: list[str] = Field(default_factory=list, max_length=24)
    tags: list[str] = Field(default_factory=list, max_length=12)
    attributes: AttributeMap = Field(default_factory=dict, max_length=24)
    effective_at: str = ""
    conditions: list[Predicate] = Field(default_factory=list, max_length=16)
    logic: Literal["and", "or"] = "and"

    @model_validator(mode="before")
    @classmethod
    def legacy(cls, value):
        return migrate_legacy_fields(value)

    @field_validator("attributes")
    @classmethod
    def attributes_valid(cls, values):
        return clean_attributes(values)

    @field_validator(
        "versions",
        "organizations",
        "locations",
        "topics",
        "entities",
        "entity_ids",
        "tags",
    )
    @classmethod
    def bounded(cls, values, info):
        out = []
        for v in values:
            if not v or len(v) > 120 or any(ord(c) < 32 for c in v):
                raise ValueError("过滤值需为 1–120 字符，不含控制字符")
            v = v.upper() if info.field_name == "entity_ids" else v
            if v not in out:
                out.append(v)
        return out

    @field_validator("effective_at")
    @classmethod
    def valid_date(cls, value):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("日期必须为 YYYY-MM-DD")
            date.fromisoformat(value)
        return value

    def compact(self):
        out = {k: v for k, v in self.model_dump().items() if v and k != "logic"}
        if out.get("conditions"):
            out["logic"] = self.logic
        return out


def literal(value):
    # Milvus accepts JSON string literals. No user-provided keys/operators are concatenated.
    return json.dumps(value, ensure_ascii=False)


def compile_filters(filters, fields=None):
    f = filters if isinstance(filters, Filters) else Filters.model_validate(filters)
    parts = []
    if f.conditions:
        parts.append(
            compile_dynamic(
                {
                    "conditions": [c.model_dump() for c in f.conditions],
                    "logic": f.logic,
                },
                fields or [],
            )
        )
    # Compatibility adapter for explicit v1-v3 API inputs; the LLM path uses conditions.
    for key, field in [
        ("doc_types", "doc_type"),
        ("versions", "version"),
        ("organizations", "organization"),
    ]:
        if values := getattr(f, key):
            parts.append(f"{field} in {literal(values)}")
    for key in ("locations", "topics", "entities", "entity_ids", "tags"):
        if values := getattr(f, key):
            parts.append(f'json_contains_any(metadata["{key}"], {literal(values)})')
    for key, values in f.attributes.items():
        parts.append(
            f"json_contains_any(attributes[{literal(key)}], {literal(values)})"
        )
    if f.effective_at:
        number = int(f.effective_at.replace("-", ""))
        # Unknown boundaries are NOT interpreted as unlimited validity.
        parts.append(
            f"effective_from > 0 and effective_from <= {number} and effective_to >= {number}"
        )
    return " and ".join(f"({p})" for p in parts)


def catalog(documents):
    out = {
        k: set()
        for k in (
            "doc_types",
            "versions",
            "organizations",
            "locations",
            "topics",
            "entities",
            "entity_ids",
            "tags",
        )
    }
    attributes = {}
    for doc in documents:
        meta = migrate_legacy_fields(json.loads(doc.get("metadata_json", "{}")))
        for key, field in [
            ("doc_types", "doc_type"),
            ("versions", "version"),
            ("organizations", "organization"),
        ]:
            if value := meta.get(field):
                out[key].add(value)
        for key in ("locations", "topics", "entities", "entity_ids", "tags"):
            out[key].update(meta.get(key, []))
        for key, values in meta.get("attributes", {}).items():
            attributes.setdefault(key, set()).update(values)
    return {key: sorted(values)[:200] for key, values in out.items()} | {
        "attributes": {
            key: sorted(attributes[key])[:40] for key in sorted(attributes)[:24]
        }
    }


class InvalidQueryFilter(ValueError):
    """The caller supplied a filter unsupported by the current field catalog."""


class IntentAgent:
    """Main flow: scoped schema + context -> internal LLM -> validated predicates."""

    def __init__(self, sv):
        self.sv = sv

    def plan(
        self,
        question,
        documents,
        requested=None,
        planner="auto",
        chat_history=None,
        user_context=None,
        current_time=None,
        fields=None,
    ):
        mode = self.sv.cfg.get("intent_mode", "auto") if planner == "auto" else planner
        if mode not in {"auto", "llm", "manual"}:
            raise ValueError("请选择 auto、llm 或 manual 意图模式")
        explicit = Filters.model_validate(requested or {}).compact()
        fields = field_catalog(documents) if fields is None else fields
        if explicit.get("conditions"):
            try:
                compile_filters(explicit, fields)
            except ValueError as exc:
                raise InvalidQueryFilter(str(exc)) from exc
        plan = {
            "intent": "general",
            "confidence": 0.0,
            "planner": "llm",
            "explicit_filters": explicit,
            "routes": [],
            "warnings": [],
            "metadata_schema_version": INDEX_VERSION,
            "allow_inference_fallback": False,
            "search_query": question,
            "semantic_query": question,
            "needs_clarification": False,
            "clarifying_question": "",
            "field_schema": fields,
        }
        if mode == "manual":
            plan["planner"] = "manual"
            plan["routes"] = [
                {"name": "手动条件检索", "inferred": {}, "query": question}
            ]
            return plan
        try:
            payload = build_intent_payload(
                question, fields, chat_history, user_context, current_time
            )
            result = classify_intent(self.sv, payload, fields)
            if result.needs_clarification or result.confidence < 0.75:
                plan.update(
                    needs_clarification=True,
                    clarifying_question=result.clarifying_question
                    or "请补充要查询的对象或范围，以便准确筛选资料。",
                    confidence=result.confidence,
                )
                return plan
            conditions = [c.model_dump() for c in result.filters.conditions]
            explicit_fields = {c["field"] for c in explicit.get("conditions", [])}
            # If the caller mixes old-style explicit filters with dynamic LLM conditions,
            # both are ANDed by the server. Never silently replace either constraint.
            if explicit_fields and result.filters.logic == "and":
                conditions = [
                    c for c in conditions if c["field"] not in explicit_fields
                ]
            elif explicit_fields and any(
                c["field"] in explicit_fields for c in conditions
            ):
                conditions = []
                plan["warnings"].append(
                    "模型 OR 条件与手动条件重叠，已仅使用手动条件。"
                )
            inferred = (
                {"conditions": conditions, "logic": result.filters.logic}
                if conditions
                else {}
            )
            plan.update(
                intent=result.intent,
                confidence=result.confidence,
                search_query=result.search_query,
                semantic_query=result.search_query,
            )
            plan["routes"] = [
                {
                    "name": "模型识别字段与条件",
                    "inferred": inferred,
                    "query": result.search_query,
                }
            ]
        except Exception:
            plan.update(
                planner="semantic-fallback",
                routes=[
                    {
                        "name": "模型不可用，保留手动条件进行语义检索",
                        "inferred": {},
                        "query": question,
                    }
                ],
            )
            plan["warnings"].append(
                "内部 LLM 调用或输出校验失败；本次未自动生成 Metadata 条件，请查看模型配置或手动筛选。"
            )
        return plan
