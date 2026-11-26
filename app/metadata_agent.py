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
    attributes, evidence, used, calls = {}, {}, {}, 0
    for batch in batches(chunks):
        payload = {
            "filename": doc["filename"],
            "chunks": batch,
            "existing_fields": definitions,
            "existing_metadata": old.model_dump(),
            "output_schema": Discovery.model_json_schema(),
        }
        response = Discovery.model_validate(
            parse_json(
                sv.agent_completion(
                    "discover_metadata", payload, max_tokens=5000, timeout=90
                )
            )
        )
        calls += 1
        batch_by_id = {c["chunk_id"]: c for c in batch}
        for field in response.fields:
            if field.key in RESERVED or field.key in {
                "user_id",
                "allowed_kb_ids",
                "permissions",
            }:
                raise Problem("Metadata 模型返回了系统保留字段。")
            for value in field.values:
                valid = [
                    e.model_dump()
                    for e in field.evidence
                    if e.value == value
                    and e.chunk_id in batch_by_id
                    and batch_by_id[e.chunk_id]["page"] == e.page
                    and e.quote in batch_by_id[e.chunk_id]["text"]
                ]
                if not valid:
                    raise Problem(
                        "Metadata 模型给出的字段值没有有效原文依据：" + field.key
                    )
                evidence.setdefault("attributes." + field.key, []).extend(valid)
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
        "agent_version": 4,
        "schema_version": INDEX_VERSION,
        "field_definitions": used,
        "provenance": evidence,
        "previous_provenance": previous.get("provenance", {}) if preserve else {},
        "model_calls": calls,
        "chunks_examined": len(chunks),
        "notice": "字段由 LLM 识别，逐字依据和类型已校验；语义仍可能误判，可在 Metadata 编辑器修正。",
    }
