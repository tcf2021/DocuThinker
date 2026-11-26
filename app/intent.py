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
    CODE,
    VERSION,
    TYPE_WORDS,
    DocType,
    AttributeMap,
    ATTRIBUTE_LABELS,
    clean_attributes,
    migrate_legacy_fields,
    normalized,
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


class ModelIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal[
        "lookup", "comparison", "calculation", "policy_lookup", "general"
    ] = "general"
    confidence: float = Field(ge=0, le=1)
    filters: Filters = Field(default_factory=Filters)
    needs_policy_context: bool = False


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


def rule_plan(question, available):
    text = normalized(question)
    filters = {}
    # Entity IDs may be absent from catalog: keep the literal as an inferred route, then fall back.
    codes = list(dict.fromkeys(m.group().upper() for m in CODE.finditer(question)))[:24]
    if codes:
        filters["entity_ids"] = codes
    versions = list(dict.fromkeys(m.group(1) for m in VERSION.finditer(question)))[:12]
    if versions:
        filters["versions"] = versions
    for field in ("locations", "topics", "entities", "organizations", "tags"):
        vals = [
            value for value in available.get(field, []) if normalized(value) in text
        ]
        if vals:
            filters[field] = vals[:12]
    # A labelled ID can be numeric or non-hyphenated; it need not match CODE.
    labelled_ids = [v for v in available.get("entity_ids", []) if normalized(v) in text]
    if labelled_ids:
        filters["entity_ids"] = list(dict.fromkeys(codes + labelled_ids))[:24]
    attrs = available.get("attributes", {})
    inferred_attrs = {}
    for key, values in attrs.items():
        named = any(
            normalized(label) in text for label in [key, *ATTRIBUTE_LABELS.get(key, [])]
        )
        matches = [
            v
            for v in values
            if normalized(v) in text
            and (
                named
                or sum(
                    any(normalized(v) == normalized(x) for x in other)
                    for other in attrs.values()
                )
                == 1
            )
        ]
        if matches:
            inferred_attrs[key] = matches[:12]
    if inferred_attrs:
        filters["attributes"] = inferred_attrs
    types = [key for key, words in TYPE_WORDS.items() if any(w in text for w in words)]
    if types:
        filters["doc_types"] = types
    # Only explicitly bounded validity queries. Never treat acceptance or observation time as validity.
    effective = re.search(r"(?:生效日期为|有效期包含)\s*(\d{4}-\d{2}-\d{2})", question)
    if effective:
        try:
            date.fromisoformat(effective.group(1))
            filters["effective_at"] = effective.group(1)
        except ValueError:
            pass
    calculation = any(
        w in text
        for w in [
            "多少天后",
            "总共",
            "合计",
            "差额",
            "多占",
            "计算",
            "相差",
            "几分钟",
            "gi·",
            "gib",
            "总量",
        ]
    )
    comparison = (
        any(w in text for w in ["比较", "对比", "区别", "分别", "与", " vs "])
        or len(codes) > 1
        or len(versions) > 1
    )
    policy = any(
        w in text
        for w in [
            "保留",
            "保管",
            "适用",
            "制度",
            "政策",
            "有效",
            "删除",
            "到期",
            "retention",
        ]
    )
    kind = (
        "calculation"
        if calculation
        else "comparison"
        if comparison
        else "policy_lookup"
        if policy
        else "lookup"
        if filters
        else "general"
    )
    return {
        "intent": kind,
        "confidence": 0.9 if filters else 0.5,
        "filters": filters,
        "needs_policy_context": policy,
        "planner": "rules",
        "warnings": [],
    }


def anchored_filters(candidate, question, available, rules):
    """LLM may select canonical values only when query wording supports them."""
    accepted = {}
    text = normalized(question)
    for key, values in candidate.compact().items():
        if key == "attributes":
            kept_attrs = {
                name: [
                    v
                    for v in items
                    if v in available.get("attributes", {}).get(name, [])
                    and v in rules["filters"].get("attributes", {}).get(name, [])
                ]
                for name, items in values.items()
            }
            kept_attrs = {name: items for name, items in kept_attrs.items() if items}
            if kept_attrs:
                accepted[key] = kept_attrs
            continue
        if key == "effective_at":
            if rules["filters"].get(key) == values:
                accepted[key] = values
            continue
        kept = []
        for value in values:
            rule_values = rules["filters"].get(key, [])
            anchored = value in rule_values or normalized(value) in text
            if key == "doc_types":
                anchored = any(w in text for w in TYPE_WORDS.get(value, []))
            if value in available.get(key, []) and anchored:
                kept.append(value)
        if kept:
            accepted[key] = kept
    return accepted


class LegacyIntentAgent:
    def __init__(self, sv):
        self.sv = sv

    def plan(self, question, documents, requested=None, planner="auto"):
        explicit = Filters.model_validate(requested or {}).compact()
        available = catalog(documents)
        result = rule_plan(question, available)
        mode = self.sv.cfg.get("intent_mode", "auto") if planner == "auto" else planner
        if mode not in ("rules", "auto", "llm"):
            mode = "rules"
        configured = bool(
            self.sv.cfg.get("chat_base") and self.sv.cfg.get("chat_model")
        )
        if mode in ("auto", "llm") and configured:
            try:
                raw = self.sv.intent_completion(
                    question, available, ModelIntent.model_json_schema()
                )
                cleaned = re.sub(
                    r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I
                )
                model = ModelIntent.model_validate_json(cleaned)
                if model.confidence >= 0.8:
                    inferred = anchored_filters(
                        model.filters, question, available, result
                    )
                    # Explicit rules preserve all IDs / versions in comparison questions.
                    result["filters"] = inferred | result["filters"]
                    result["needs_policy_context"] |= model.needs_policy_context
                    if result["intent"] in ("general", "lookup"):
                        result["intent"] = model.intent
                    result.update(planner="llm+rules", confidence=model.confidence)
                else:
                    result["warnings"].append(
                        "模型意图置信度不足，使用可核对的规则计划。"
                    )
            except Exception:
                result["warnings"].append(
                    "意图模型调用或结构校验失败，已回退到规则计划。"
                )
        elif mode == "llm" and not configured:
            result["warnings"].append("未配置意图模型，使用规则计划。")
        # Explicit filters always win and are never relaxed, including on fallback/context routes.
        found = result.pop("filters")
        inferred = {key: value for key, value in found.items() if key not in explicit}
        if "attributes" in explicit:
            remaining = {
                k: v
                for k, v in found.get("attributes", {}).items()
                if k not in explicit["attributes"]
            }
            if remaining:
                inferred["attributes"] = remaining
        context_versions = inferred.get("versions", [])
        if result["needs_policy_context"] and inferred.get("entity_ids"):
            # A batch fact lives in a ledger/receipt even when the question says 'which policy'.
            inferred.pop("doc_types", None)
            inferred.pop("versions", None)
        routes = [{"name": "匹配", "inferred": inferred}]
        if result["needs_policy_context"] and (
            inferred.get("entity_ids")
            or inferred.get("locations")
            or inferred.get("topics")
            or inferred.get("attributes")
        ):
            support = {"doc_types": ["policy"]}
            if context_versions:
                support["versions"] = context_versions
            routes.extend(
                [
                    {
                        "name": "制度依据",
                        "inferred": {
                            k: v for k, v in support.items() if k not in explicit
                        },
                    },
                    {
                        "name": "例外审批依据",
                        "inferred": {"doc_types": ["exception"]}
                        if "doc_types" not in explicit
                        else {},
                    },
                ]
            )
        # Separate branches protect both sides of a comparison. Bound the cost to five routes.
        for field in ("versions", "entity_ids"):
            if len(inferred.get(field, [])) > 1:
                values = inferred[field]
                slots = 5 - len(routes[1:])
                branches = [[v] for v in values[: slots - 1]] + [values[slots - 1 :]]
                routes = [
                    {
                        "name": f"{field}={','.join(values)}",
                        "inferred": inferred | {field: values},
                    }
                    for values in branches
                    if values
                ] + routes[1:]
                break
        if "最新" in question or "当前" in question:
            result["warnings"].append(
                "“最新/当前”需要核对生效范围与历史批次，未自动丢弃旧版本。"
            )
        context_question = VERSION.sub("", CODE.sub("", question)).strip()
        for route in routes:
            route["query"] = question
            if route["name"] == "制度依据":
                route["query"] = context_question + " 制度 规定 适用条件"
            elif route["name"] == "例外审批依据":
                route["query"] = context_question + " 例外 审批 适用条件"
        return result | {
            "explicit_filters": explicit,
            "routes": routes[:5],
            "metadata_schema_version": INDEX_VERSION,
        }


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
        if mode == "rules":
            # Explicit backwards-compatible choice; never an automatic LLM failure fallback.
            return LegacyIntentAgent(self.sv).plan(
                question, documents, requested, "rules"
            )
        explicit = Filters.model_validate(requested or {}).compact()
        fields = field_catalog(documents) if fields is None else fields
        if explicit.get("conditions"):
            compile_filters(explicit, fields)
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
