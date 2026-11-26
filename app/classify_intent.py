"""The PDF's query + authorized schema + user context -> structured intent contract."""

from datetime import datetime, timezone
import re

from .field_schema import IntentOutput, checked_model_conditions


def build_intent_payload(question, fields, history, context, current_time=None):
    if len(fields) > 128:
        raise ValueError("可过滤字段超过 128 项，请整理知识库字段目录")
    schema = IntentOutput.model_json_schema()
    if fields:
        schema["$defs"]["Predicate"]["properties"]["field"]["enum"] = [
            f["field"] for f in fields
        ]
    else:
        schema["$defs"]["DynamicFilters"]["properties"]["conditions"]["maxItems"] = 0
    return {
        "query": question,
        "chat_history": history or [],
        "user_context": context or {},
        "metadata_schema": {"filterable_fields": fields},
        "current_time": current_time
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": list(IntentOutput.model_fields["intent"].annotation.__args__),
        "output_schema": schema,
    }


def classify_intent(sv, payload, fields):
    raw = sv.agent_completion("classify_intent", payload, max_tokens=2400, timeout=45)
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I)
    return checked_model_conditions(
        IntentOutput.model_validate_json(cleaned), fields, payload
    )


def build_filter_expression(filters, fields):
    """Compile business predicates only; VectorIndex adds its independent mandatory ACL."""
    from .field_schema import compile_dynamic

    return compile_dynamic(filters, fields)
