"""Discover filterable JSON keys from authorized records; compile typed conditions."""

import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .metadata import DocumentMetadata, RESERVED, migrate_legacy_fields


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Literal["query", "chat_history", "user_context"]
    quote: str = Field(min_length=1, max_length=500)


class Predicate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(pattern=r"^(metadata|attributes)\.[a-z][a-z0-9_]{0,39}$")
    operator: Literal["eq", "in", "contains_any", "contains_all", "gte", "lt"]
    values: list[str] = Field(min_length=1, max_length=24)
    evidence: Evidence | None = None

    @field_validator("values")
    @classmethod
    def values_valid(cls, values):
        if any(not v or len(v) > 240 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("筛选值需为 1–240 字符，不含控制字符")
        return list(dict.fromkeys(values))


class DynamicFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logic: Literal["and", "or"] = "and"
    conditions: list[Predicate] = Field(default_factory=list, max_length=16)


class IntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["lookup", "comparison", "calculation", "policy_lookup", "general"]
    search_query: str = Field(
        min_length=1,
        max_length=2000,
        validation_alias=AliasChoices("semantic_query", "search_query"),
        serialization_alias="semantic_query",
    )
    filters: DynamicFilters = Field(default_factory=DynamicFilters)
    confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False
    clarifying_question: str = Field(default="", max_length=500)


def utc_stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def field_catalog(documents):
    """Call with ready_documents(owner, selected_kb), never with unscoped data."""
    registry = {}
    for doc in documents:
        meta = migrate_legacy_fields(json.loads(doc.get("metadata_json", "{}")))
        provenance = json.loads(doc.get("metadata_provenance", "{}"))
        definitions = provenance.get("field_definitions", {})
        sources = [("metadata", meta), ("attributes", meta.get("attributes", {}))]
        if provenance.get("agent_version") == 4:
            sources.append(
                (
                    "metadata",
                    {
                        "source_file": meta.get("source_file", doc["filename"]),
                        "ingested_at": meta.get(
                            "ingested_at", utc_stamp(doc["created"])
                        ),
                    },
                )
            )
        for root, data in sources:
            for key, value in data.items():
                if (
                    not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key)
                    or key in RESERVED
                    or key in {"user_id", "allowed_kb_ids"}
                ):
                    continue
                declared = (
                    DocumentMetadata.model_fields.get(key)
                    if root == "metadata"
                    else None
                )
                if declared and value == declared.default:
                    continue
                if isinstance(value, str) and value:
                    kind, values = "string", [value]
                elif (
                    isinstance(value, list)
                    and value
                    and all(isinstance(v, str) for v in value)
                ):
                    kind, values = "string_array", value
                else:
                    continue
                if root == "metadata" and key == "ingested_at":
                    kind = "datetime"
                field = root + "." + key
                operators = (
                    ["contains_any", "contains_all"]
                    if kind == "string_array"
                    else ["eq", "in"]
                )
                if kind == "datetime":
                    operators += ["gte", "lt"]
                definition = definitions.get(key, {}) if root == "attributes" else {}
                model_field = DocumentMetadata.model_fields.get(key)
                description = (
                    definition.get("description")
                    or (model_field.description if model_field else None)
                    or key
                )
                if root == "metadata" and key == "ingested_at":
                    description = (
                        "文档入库时间，带时区的 UTC ISO 8601 字符串；不是文档生效时间"
                    )
                if root == "metadata" and key == "source_file":
                    description = "原始上传文件名"
                label = definition.get("label") or key
                entry = registry.setdefault(
                    field,
                    {
                        "field": field,
                        "type": kind,
                        "operators": operators,
                        "label": label,
                        "description": description,
                        "examples": [],
                        "document_count": 0,
                    },
                )
                if entry["type"] != kind:
                    raise ValueError(
                        "同一 Metadata 字段的存储类型不一致，请先修正：" + field
                    )
                entry["document_count"] += 1
                entry["examples"] = list(dict.fromkeys(entry["examples"] + values))[:30]
    return list(registry.values())


def compile_dynamic(filters, fields):
    spec = (
        filters
        if isinstance(filters, DynamicFilters)
        else DynamicFilters.model_validate(filters)
    )
    registry = {f["field"]: f for f in fields}
    parts = []
    for item in spec.conditions:
        entry = registry.get(item.field)
        if not entry or item.operator not in entry["operators"]:
            raise ValueError(
                "筛选字段或运算符不在当前用户的可用 Schema 中：" + item.field
            )
        root, key = item.field.split(".", 1)
        path = root + "[" + json.dumps(key, ensure_ascii=False) + "]"
        values = item.values
        if entry["type"] == "datetime":
            values = []
            for value in item.values:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError("时间条件必须包含时区")
                values.append(dt.astimezone(timezone.utc).isoformat(timespec="seconds"))
        literal = json.dumps(values, ensure_ascii=False)
        if item.operator in ("contains_any", "contains_all"):
            parts.append(f"json_{item.operator}({path}, {literal})")
        elif item.operator == "in":
            parts.append(f"{path} in {literal}")
        else:
            if len(values) != 1:
                raise ValueError("eq/gte/lt 必须只有一个值")
            op = {"eq": "==", "gte": ">=", "lt": "<"}[item.operator]
            parts.append(f"{path} {op} {json.dumps(values[0], ensure_ascii=False)}")
    return (" " + spec.logic + " ").join("(" + p + ")" for p in parts)


def checked_model_conditions(output, fields, payload):
    # Schema validates paths/types; evidence grounds semantic mappings without
    # requiring the canonical value to occur literally in the original query.
    compile_dynamic(output.filters, fields)
    sources = {
        "query": [payload["query"]],
        "chat_history": [
            m["content"] for m in payload["chat_history"] if m["role"] == "user"
        ],
        "user_context": [payload["user_context"].get("department", "")],
    }
    for condition in output.filters.conditions:
        evidence = condition.evidence
        if not evidence or not any(
            evidence.quote in text for text in sources[evidence.source]
        ):
            raise ValueError("模型筛选条件缺少可核对的输入依据")
    if output.needs_clarification and not output.clarifying_question.strip():
        raise ValueError("需要澄清时必须提供问题")
    return output
