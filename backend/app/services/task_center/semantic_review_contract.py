from __future__ import annotations

SEMANTIC_REVIEW_PROMPT_VERSION = "semantic_reviewer_v1"
REVIEW_SCHEMA_FIELDS = (
    "decision", "confidence", "codes", "evidence", "prompt_version",
    "primary_aspect_result", "reply_relation_result",
)


class TwoStageRealizeError(RuntimeError):
    def __init__(self, code: str, *, evidence: dict | None = None, tokens: int = 0):
        super().__init__(code)
        self.code = code
        self.evidence = dict(evidence or {})
        self.tokens = max(0, int(tokens or 0))


def _parse_semantic_review(payload: object, config: dict) -> dict:
    item = payload[0] if isinstance(payload, list) and payload else payload
    try:
        return _validate_semantic_review_item(item, config)
    except TwoStageRealizeError as exc:
        raise TwoStageRealizeError(exc.code,
            evidence={"schema_validation": _schema_shape(payload, item)},
            tokens=exc.tokens) from exc


def _schema_shape(payload: object, item: object) -> dict:
    fields = item if isinstance(item, dict) else {}
    return {
        "root_type": type(payload).__name__,
        "root_item_count": len(payload) if isinstance(payload, list) else None,
        "fields": {key: type(fields[key]).__name__ if key in fields else "missing"
            for key in REVIEW_SCHEMA_FIELDS},
        "decision_allowed": str(fields.get("decision") or "").strip() in {"pass", "fail", "uncertain"},
        "evidence_item_count": len(fields["evidence"]) if isinstance(fields.get("evidence"), list) else None,
        "codes_item_count": len(fields["codes"]) if isinstance(fields.get("codes"), list) else None,
    }


def _validate_semantic_review_item(item: object, config: dict) -> dict:
    if not isinstance(item, dict):
        raise TwoStageRealizeError("semantic_review_schema_invalid")
    decision = str(item.get("decision") or "").strip()
    confidence = item.get("confidence")
    evidence = item.get("evidence")
    prompt_version = str(item.get("prompt_version") or "").strip()
    if decision not in {"pass", "fail", "uncertain"}:
        raise TwoStageRealizeError("semantic_review_schema_invalid")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise TwoStageRealizeError("semantic_review_confidence_invalid")
    if not isinstance(evidence, list) or not evidence:
        raise TwoStageRealizeError("semantic_review_evidence_missing")
    if any(
        not isinstance(item, dict)
        or not str(item.get("criterion") or "").strip()
        or not str(item.get("observed") or "").strip()
        for item in evidence
    ):
        raise TwoStageRealizeError("semantic_review_evidence_invalid")
    if prompt_version != SEMANTIC_REVIEW_PROMPT_VERSION:
        raise TwoStageRealizeError("semantic_review_prompt_version_mismatch")
    dimensions = _grounding_review_dimensions(item, config)
    return {
        "decision": decision,
        "confidence": float(confidence),
        "codes": [str(code) for code in (item.get("codes") or []) if str(code)],
        "evidence": evidence,
        "prompt_version": prompt_version,
        "model": str(config.get("ai_semantic_reviewer_model") or "injected_test_reviewer"),
        **dimensions,
    }


def _grounding_review_dimensions(item: dict, config: dict) -> dict:
    assignment = dict(config.get("_comment_grounding_assignment") or {})
    if not assignment:
        return {}
    primary = str(item.get("primary_aspect_result") or "")
    relation = str(item.get("reply_relation_result") or "")
    if primary not in {"pass", "reject", "unknown"}:
        raise TwoStageRealizeError("semantic_primary_aspect_result_invalid")
    allowed_relation = {"pass", "reject", "unknown", "not_applicable"}
    if relation not in allowed_relation:
        raise TwoStageRealizeError("semantic_reply_relation_result_invalid")
    expected_reply = assignment.get("relation_kind") == "reply"
    if expected_reply == (relation == "not_applicable"):
        raise TwoStageRealizeError("semantic_reply_relation_applicability_invalid")
    return {
        "primary_aspect_result": primary,
        "reply_relation_result": relation,
    }
