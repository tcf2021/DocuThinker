"""LLM-driven field discovery during ingestion; no industry keyword extraction."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from . import db
from .config import INDEX_VERSION
from .field_schema import field_catalog
from .metadata import AttributeKey, AttributeValues, DocumentMetadata, RESERVED
from .parsing import Problem


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: str = Field(min_length=1, max_length=100)
    chunk_id: int = Field(ge=0)
    page: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=800)


class DiscoveredField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: AttributeKey
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    values: AttributeValues
    evidence: list[FieldEvidence] = Field(min_length=1, max_length=24)


class Discovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[DiscoveredField] = Field(default_factory=list, max_length=24)


class FieldDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)


def parse_json(raw):
    return json.loads(
        re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I)
    )


def batches(chunks, max_chars=12000, max_chunks=12):
    group, chars = [], 0
    for i, chunk in enumerate(chunks):
        record = {"chunk_id": i, "page": chunk["page"], "text": chunk["text"]}
        if group and (
            chars + len(record["text"]) > max_chars or len(group) >= max_chunks
        ):
            yield group
            group, chars = [], 0
        group.append(record)
        chars += len(record["text"])
    if group:
        yield group


def source_quote(quote, text):
    """Return the actual source span; only existing whitespace runs may differ."""
    if quote in text:
        return quote
    # Never drop words, punctuation or digits, join tokens, or cross chunk borders.
    pattern = r"\s+".join(re.escape(part) for part in re.split(r"\s+", quote))
    match = re.search(pattern, text)
    if match and len(match[0]) <= 800:
        return match[0]
    return None


def validate_evidence(response, batch):
    """Validate each value before merging any of this batch's proposed metadata."""
    by_id = {chunk["chunk_id"]: chunk for chunk in batch}
    verified, errors = [], []
    for field in response.fields:
        if field.key in RESERVED:
            raise Problem("Metadata 模型返回了系统保留字段。")
        field_evidence = []
        for value in field.values:
            candidates = [e for e in field.evidence if e.value == value]
            valid, reasons = [], set()
            if not candidates:
                reasons.add("missing_value_evidence")
            for item in candidates:
                chunk = by_id.get(item.chunk_id)
                if chunk is None:
                    reasons.add("chunk_not_in_batch")
                elif item.page != chunk["page"]:
                    reasons.add("page_mismatch")
                elif (quote := source_quote(item.quote, chunk["text"])) is None:
                    reasons.add("quote_not_in_chunk")
                else:
                    valid.append(item.model_dump() | {"quote": quote})
            if not valid:
                errors.append(
                    {"field": field.key, "value": value, "reasons": sorted(reasons)}
                )
            field_evidence.extend(valid)
        verified.append((field, field_evidence))
    return verified, errors


def discover(sv, doc, chunks, preserve=True):
    with db.connection() as con:
        records = [
            dict(r)
            for r in con.execute(
                "SELECT docs.* FROM docs JOIN kbs ON docs.kb=kbs.id WHERE docs.kb=? AND docs.status='ready'",
                (doc["kb"],),
            )
        ]
    definitions = {}
    for field in field_catalog(records):
        if field["field"].startswith("attributes."):
            definitions[field["field"].split(".", 1)[1]] = {
                "label": field["label"],
                "description": field["description"],
                "examples": field["examples"],
            }
    if len(definitions) > 128:
        raise Problem("知识库字段目录超过 128 项，请整理重复字段后再识别。")
    old = DocumentMetadata.model_validate_json(doc.get("metadata_json", "{}"))
    previous = json.loads(doc.get("metadata_provenance", "{}"))
    attributes, evidence, used, calls, repairs = {}, {}, {}, 0, 0
    for batch in batches(chunks):
        payload = {
            "filename": doc["filename"],
            "chunks": batch,
            "existing_fields": definitions,
            "existing_metadata": old.model_dump(),
            "output_schema": Discovery.model_json_schema(),
        }
        for attempt in range(2):
            calls += 1
            response = Discovery.model_validate(
                parse_json(
                    sv.agent_completion(
                        "discover_metadata", payload, max_tokens=5000, timeout=90
                    )
                )
            )
            verified, errors = validate_evidence(response, batch)
            if not errors:
                break
            if attempt:
                reasons = {
                    "missing_value_evidence": "缺少与字段值一致的 evidence.value",
                    "chunk_not_in_batch": "引用的切片不在当前批次",
                    "page_mismatch": "引用页码与切片页码不一致",
                    "quote_not_in_chunk": "引用文字无法在对应原文片段中找到",
                }
                detail = "、".join(reasons[r] for r in errors[0]["reasons"])
                raise Problem(
                    "Metadata 模型给出的字段值没有有效原文依据："
                    + errors[0]["field"]
                    + "（已自动修正一次仍未通过；"
                    + detail
                    + "）。本次提案未应用。"
                )
            repairs += 1
            payload = payload | {
                "evidence_repair": {
                    "previous_output": response.model_dump(),
                    "validation_errors": errors,
                }
            }
        for field, field_evidence in verified:
            evidence.setdefault("attributes." + field.key, []).extend(field_evidence)
            attributes[field.key] = list(
                dict.fromkeys(attributes.get(field.key, []) + field.values)
            )
            if len(attributes[field.key]) > 12:
                raise Problem(
                    "单个字段超过 12 个值，请细分文档或审核 Metadata：" + field.key
                )
            definitions.setdefault(
                field.key, {"label": field.label, "description": field.description}
            )
            definition = definitions[field.key]
            if definition["description"] == field.key:
                definition.update(label=field.label, description=field.description)
            definition["examples"] = list(
                dict.fromkeys(definition.get("examples", []) + field.values)
            )[:30]
            used[field.key] = {k: definition[k] for k in ("label", "description")}
        if len(attributes) > 24:
            raise Problem("单个文档识别出超过 24 个字段，请细分文档后重新入库。")
    if not calls:
        raise Problem("没有可供 Metadata 模型分析的文档片段。")
    # Previously published metadata is retained; new fields fill gaps. An explicit
    # re-proposal can replace old inferred values after the user reviews it.
    base = old.model_dump() if preserve else DocumentMetadata().model_dump()
    if preserve:
        attributes = attributes | old.attributes
        used = used | previous.get("field_definitions", {})
    base["title"] = base["title"] or doc["filename"]
    base["attributes"] = attributes
    metadata = DocumentMetadata.model_validate(base).model_dump()
    return {
        "metadata": metadata,
        "method": "llm_auto",
        "agent_version": 5,
        "schema_version": INDEX_VERSION,
        "field_definitions": used,
        "provenance": evidence,
        "previous_provenance": previous.get("provenance", {}) if preserve else {},
        "model_calls": calls,
        "repair_attempts": repairs,
        "chunks_examined": len(chunks),
        "notice": "字段由 LLM 识别，切片、页码、原文依据和类型已校验（仅兼容空白格式差异）；语义仍可能误判，可在 Metadata 编辑器修正。"
        + (
            f"其中 {repairs} 批次经过一次自动修正，无法证实的字段或值可能已移除，请核对提案。"
            if repairs
            else ""
        ),
    }
