"""A shared document contract: core fields, bounded attributes and source evidence."""

import json
import re
import unicodedata
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .config import INDEX_VERSION

DocType = Literal[
    "policy",
    "exception",
    "ledger",
    "incident",
    "receipt",
    "manual",
    "report",
    "contract",
    "invoice",
    "article",
    "faq",
    "record",
    "other",
]
TYPE_LABELS = {
    "policy": "制度",
    "exception": "例外审批",
    "ledger": "台账",
    "incident": "故障报告",
    "receipt": "回执",
    "manual": "操作手册",
    "report": "报告",
    "contract": "合同",
    "invoice": "发票",
    "article": "文章/论文",
    "faq": "常见问题",
    "record": "记录/纪要",
    "other": "其他",
}
TYPE_WORDS = {
    "receipt": ["回执", "交接单", "receipt"],
    "exception": ["例外", "特批", "exception"],
    "incident": ["事故", "故障", "incident"],
    "ledger": ["台账", "明细表", "ledger"],
    "policy": ["制度", "规范", "政策", "规程", "policy"],
    "manual": ["手册", "指南", "manual"],
    "report": ["报告", "report"],
    "contract": ["合同", "协议", "contract", "agreement"],
    "invoice": ["发票", "invoice"],
    "article": ["论文", "文章", "article", "paper"],
    "faq": ["常见问题", "问答集", "faq"],
    "record": ["记录", "纪要", "record", "minutes"],
}
LIST_LABELS = {
    "topics": ["主题", "主题词", "topics"],
    "locations": ["地点", "地区", "站点", "locations"],
    "entities": ["实体", "对象", "entities"],
    "entity_ids": ["编号", "实体编号", "entity_ids"],
    "tags": ["标签", "关键词", "tags", "keywords"],
}
# Shared convenience aliases, not a domain-specific schema. Any named attribute
# can also be supplied as a labelled line: 属性 supplier: 示例公司
ATTRIBUTE_LABELS = {
    "department": ["适用部门", "部门", "department"],
    "product": ["产品型号", "产品", "product"],
    "category": ["业务类别", "资料类别", "category"],
    "expense_type": ["报销类型", "费用类型", "expense_type"],
}
# These aliases are used ONLY to read old records/API inputs. New text extraction
# has no observatory vocabulary and never guesses a south/north site.
LEGACY_VALUES = {
    "sites": ("locations", {"south": "南站", "north": "北站"}),
    "data_types": (
        "topics",
        {
            "calibration": "定标",
            "raw_science": "原始科学数据",
            "preview": "预览",
            "audit": "审计",
        },
    ),
}
CODE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{1,8}-){1,3}[0-9]{1,8}[A-Z0-9]*(?![A-Z0-9])", re.I
)
VERSION = re.compile(r"(?:\b[vV]|版本\s*[:：]?\s*)(\d{1,3}(?:\.\d{1,3}){1,3})")
AttributeKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
AttributeValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
AttributeValues = Annotated[list[AttributeValue], Field(min_length=1, max_length=12)]
AttributeMap = dict[AttributeKey, AttributeValues]
RESERVED = {
    "field_definitions",
    "embedding_model",
    "owner",
    "kb",
    "doc",
    "revision",
    "vec",
    "text",
    "page",
    "metadata",
    "schema_version",
    "source_sha256",
    "user_id",
    "allowed_kb_ids",
    "permissions",
}


def normalized(text):
    return unicodedata.normalize("NFKC", text).casefold()


def migrate_legacy_fields(value):
    """Translate v2 names without discarding manual labels or custom values."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for old, (new, aliases) in LEGACY_VALUES.items():
        if old in out:
            values = out.pop(old)
            if not isinstance(values, list) or not all(
                isinstance(v, str) for v in values
            ):
                raise ValueError(f"{old} 必须是字符串数组")
            existing = out.get(new, [])
            if not isinstance(existing, list) or not all(
                isinstance(v, str) for v in existing
            ):
                raise ValueError(f"{new} 必须是字符串数组")
            out[new] = list(
                dict.fromkeys(existing + [aliases.get(v, v) for v in values])
            )
    return out


def clean_attributes(values):
    for key, items in values.items():
        if key in RESERVED:
            raise ValueError("attributes 不能使用系统保留字段名")
        if any(any(ord(c) < 32 for c in item) for item in items):
            raise ValueError("属性值不能含控制字符")
    return {key: list(dict.fromkeys(items)) for key, items in values.items()}


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="", max_length=240)
    doc_type: DocType = "other"
    organization: str = Field(default="", max_length=120)
    version: str = Field(default="", max_length=40)
    language: Literal["zh", "en", "mixed", "unknown"] = "unknown"
    effective_from: str = ""
    effective_to: str = ""
    topics: list[str] = Field(default_factory=list, max_length=40)
    locations: list[str] = Field(default_factory=list, max_length=40)
    entities: list[str] = Field(default_factory=list, max_length=40)
    entity_ids: list[str] = Field(default_factory=list, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=40)
    attributes: AttributeMap = Field(default_factory=dict, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def legacy(cls, value):
        return migrate_legacy_fields(value)

    @field_validator("effective_from", "effective_to")
    @classmethod
    def valid_date(cls, value):
        if value and (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            or date.fromisoformat(value).isoformat() != value
        ):
            raise ValueError("日期必须是 YYYY-MM-DD 或空字符串")
        return value

    @field_validator("topics", "locations", "entities", "entity_ids", "tags")
    @classmethod
    def clean_list(cls, values, info):
        out = []
        for value in values:
            value = unicodedata.normalize("NFKC", value.strip())
            if not value or len(value) > 100 or any(ord(c) < 32 for c in value):
                raise ValueError("标签需为 1–100 字符，不含控制字符")
            value = value.upper() if info.field_name == "entity_ids" else value
            if value not in out:
                out.append(value)
        return out

    @field_validator("attributes")
    @classmethod
    def attributes_valid(cls, values):
        return clean_attributes(values)

    @model_validator(mode="after")
    def bounds(self):
        if (
            self.effective_from
            and self.effective_to
            and self.effective_from > self.effective_to
        ):
            raise ValueError("生效起始日期不能晚于终止日期")
        if len(self.model_dump_json().encode("utf-8")) > 48000:
            raise ValueError("Metadata 总大小不能超过 48,000 UTF-8 字节")
        return self


def labelled_values(text, labels):
    names = "|".join(re.escape(v) for v in labels)
    return [
        m.group(1).strip()
        for m in re.finditer(
            r"^\s*(?:[-*]\s+)?(?:" + names + r")\s*[:：]\s*([^\n]+)", text, re.I | re.M
        )
    ]


def split_values(lines, limit=40):
    values = [v.strip() for line in lines for v in re.split(r"[,，、;；]", line)]
    return list(dict.fromkeys(v for v in values if v and len(v) <= 100))[:limit]


def mentions(text):
    data = {
        field: split_values(labelled_values(text, labels))
        for field, labels in LIST_LABELS.items()
    }
    data["entity_ids"] = list(
        dict.fromkeys(
            [m.group().upper() for m in CODE.finditer(text)]
            + [v.upper() for v in data["entity_ids"]]
        )
    )[:200]
    return data


def extract_attributes(text):
    attrs = {}
    for key, labels in ATTRIBUTE_LABELS.items():
        if values := split_values(labelled_values(text, labels), 12):
            attrs[key] = values
    for match in re.finditer(
        r"^\s*(?:属性\s+|attribute\.)([a-z][a-z0-9_]{0,39})\s*[:：]\s*([^\n]+)",
        text,
        re.M,
    ):
        key = match.group(1)
        if key in RESERVED:
            continue
        values = split_values([match.group(2)], 12)
        if values and (key in attrs or len(attrs) < 24):
            attrs[key] = list(dict.fromkeys(attrs.get(key, []) + values))[:12]
    return attrs


def propose(filename, chunks):
    """Rules fill labelled values; no free-form NER or language model call."""
    first = chunks[0] if chunks else {"text": "", "page": 0}
    title = first.get("section") or next(
        (s.strip("# ") for s in first["text"].splitlines() if s.strip()), filename
    )
    text = "\n".join(c["text"] for c in chunks)
    doc_type = "other"
    for source in (title, filename, first["text"][:300]):
        candidates = [
            key
            for key, words in TYPE_WORDS.items()
            if any(w in normalized(source) for w in words)
        ]
        if len(candidates) == 1:
            doc_type = candidates[0]
            break
    version = (
        VERSION.search(title)
        or VERSION.search(filename)
        or VERSION.search(first["text"][:300])
    )
    org = labelled_values(text, ["所属单位", "机构", "组织", "organization"])
    zh = bool(re.search(r"[\u4e00-\u9fff]", text))
    en = bool(re.search(r"\b[A-Za-z]{3,}\b", text))
    data = dict(
        title=title[:240],
        doc_type=doc_type,
        version=version.group(1) if version else "",
        organization=org[0][:120] if org else "",
        language="mixed" if zh and en else "zh" if zh else "en" if en else "unknown",
        attributes=extract_attributes(text),
        **mentions(text),
    )
    for field, labels in [
        ("effective_from", ["生效日期", "生效自", "有效期开始", "effective_from"]),
        ("effective_to", ["失效日期", "有效至", "有效期结束", "effective_to"]),
    ]:
        for value in labelled_values(text, labels):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                try:
                    date.fromisoformat(value)
                    data[field] = value
                    break
                except ValueError:
                    pass
    if (
        data.get("effective_from")
        and data.get("effective_to")
        and data["effective_from"] > data["effective_to"]
    ):
        data["effective_from"] = data["effective_to"] = ""
    data = DocumentMetadata.model_validate(data).model_dump()
    provenance = {}
    for field, value in data.items():
        if not value or value in ("other", "unknown"):
            continue
        if field in ("title", "doc_type", "version", "language"):
            provenance[field] = [
                {
                    "source": "rules",
                    "page": first["page"],
                    "quote": first["text"][:600],
                    "filename": filename,
                }
            ]
            continue
        fields = (
            {"attributes." + k: v for k, v in value.items()}
            if field == "attributes"
            else {field: value if isinstance(value, list) else [value]}
        )
        for name, terms in fields.items():
            matches = []
            for c in chunks:
                for line in c["text"].splitlines():
                    if any(normalized(v) in normalized(line) for v in terms):
                        matches.append(
                            {"source": "rules", "page": c["page"], "quote": line[:800]}
                        )
            provenance[name] = matches[:8]
    return {
        "metadata": data,
        "provenance": provenance,
        "method": "rules",
        "schema_version": INDEX_VERSION,
    }


def migrate_proposal(metadata, provenance=None):
    """Keep reviewed v2 values; never regenerate them from domain rules."""
    old = provenance or {}
    return {
        "metadata": DocumentMetadata.model_validate(metadata).model_dump(),
        "provenance": {
            "migration": [
                {
                    "source": "v2_to_v3",
                    "mapping": "sites→locations; data_types→topics",
                    "previous": old,
                }
            ]
        },
        "method": "migration",
        "schema_version": INDEX_VERSION,
    }


def chunk_metadata(document_meta, chunk, doc, revision):
    return {
        **document_meta,
        "chunk_mentions": mentions(chunk["text"]),
        "schema_version": INDEX_VERSION,
        "source_sha256": doc["sha"],
        "section": chunk.get("section", ""),
        "page": chunk["page"],
        "revision": revision,
    }


def embedding_text(text, meta):
    parts = [
        meta["title"][:80],
        TYPE_LABELS[meta["doc_type"]],
        meta["version"],
        meta.get("section", "")[:60],
    ]
    return "文档：" + " / ".join(p for p in parts if p)[:96] + "\n正文：" + text


def date_number(value):
    return int(value.replace("-", "")) if value else 0


def checked_llm_proposal(raw, base, chunks):
    """Check quotes per core field or attribute; proposals still require review."""
    obj = json.loads(
        re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I)
    )
    if not isinstance(obj, dict):
        raise ValueError("invalid metadata proposal")
    updates, evidence = obj.get("updates", {}), obj.get("evidence", {})
    if not isinstance(updates, dict) or not isinstance(evidence, dict):
        raise ValueError("invalid metadata proposal")
    accepted, sources = {}, dict(base["provenance"])

    def supported(name):
        entries = evidence.get(name, [])
        valid = (
            isinstance(entries, list)
            and 0 < len(entries) <= 8
            and all(
                isinstance(e, dict)
                and isinstance(e.get("quote"), str)
                and e["quote"].strip()
                and len(e["quote"]) <= 800
                and any(
                    c["page"] == e.get("page") and e["quote"] in c["text"]
                    for c in chunks
                )
                for e in entries
            )
        )
        if valid:
            sources[name] = [
                {"source": "llm_proposal", "page": e["page"], "quote": e["quote"]}
                for e in entries
            ]
        return valid

    for field, value in updates.items():
        if field not in DocumentMetadata.model_fields:
            continue
        if field == "attributes" and isinstance(value, dict):
            attrs = dict(base["metadata"].get("attributes", {}))
            for key, values in value.items():
                if supported("attributes." + key):
                    attrs[key] = values
            accepted[field] = attrs
        elif field != "attributes" and supported(field):
            accepted[field] = value
    meta = DocumentMetadata.model_validate(base["metadata"] | accepted).model_dump()
    return {
        "metadata": meta,
        "provenance": sources,
        "method": "llm_proposal",
        "schema_version": INDEX_VERSION,
        "notice": "逐字依据已核对，字段含义仍需检查；点击保存并重新索引才会生效。",
    }
