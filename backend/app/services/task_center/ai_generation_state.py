from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.models import Action

from .ai_generator import AiGenerationUnavailable, GeneratedContent
from .payloads import SendMessagePayload


GENERATION_AUDIT_FIELDS = (
    "slot_id",
    "material_intent",
    "allow_material",
    "intent",
    "mood",
    "requested_model",
    "actual_model",
    "fallback_stage",
    "fallback_reason",
    "provider_duration_ms",
    "generation_attempts",
)


class GenerationMappingError(RuntimeError):
    pass


class GenerationAttemptStale(AiGenerationUnavailable):
    pass


def begin_generation_attempt(
    batch: list[tuple[Action, SendMessagePayload]],
    timestamp: datetime,
) -> tuple[str, str]:
    attempt_id = str(uuid4())
    request_id = str(uuid4())
    for action, payload in batch:
        data = payload.model_dump(mode="json")
        history = list(data.get("ai_generation_attempt_history") or [])
        history.append({
            "attempt_id": attempt_id,
            "request_id": request_id,
            "slot_id": payload.slot_id,
            "lease_owner": action.lease_owner or action.claim_owner,
            "started_at": timestamp.isoformat(),
            "outcome": "in_progress",
        })
        data.update({
            "ai_generation_status": "generating",
            "ai_generation_attempt_id": attempt_id,
            "ai_generation_request_id": request_id,
            "ai_generation_attempt_history": history,
        })
        action.payload = data
        result = dict(action.result or {})
        result.pop("ai_provider_call_started_at", None)
        action.result = {
            **result,
            "generation_stage": "generation_claimed",
            "generation_outcome": "in_progress",
            "ai_generation_attempt_id": attempt_id,
            "ai_generation_request_id": request_id,
        }
    return attempt_id, request_id


def validate_generation_mapping(
    batch: list[tuple[Action, SendMessagePayload]],
    contents: list[str],
    *,
    generation_slots: list[dict],
) -> None:
    slot_ids = [str(payload.slot_id or "").strip() for _action, payload in batch]
    if not all(slot_ids) or len(slot_ids) != len(set(slot_ids)):
        raise GenerationMappingError("ai_generation_slot_mapping_invalid")
    if len(contents) != len(batch):
        raise GenerationMappingError("ai_generation_output_count_mismatch")
    validate_output_sequences(
        contents,
        len(batch),
        is_reply=bool(batch and batch[0][1].reply_to_message_id),
    )
    _validate_reply_mapping(batch, contents)
    _validate_fixed_slots(batch, contents, generation_slots)
    if any(not str(content or "").strip() for content in contents):
        raise GenerationMappingError("ai_generation_output_empty")


def validate_output_sequences(
    contents: list[str],
    expected_count: int,
    *,
    is_reply: bool,
) -> None:
    if len(contents) != expected_count:
        raise GenerationMappingError("ai_generation_output_count_mismatch")
    expected = list(range(1, expected_count + 1))
    actual = _output_sequence_indexes(contents)
    if len(actual) != len(set(actual)):
        raise GenerationMappingError("ai_generation_output_sequence_duplicate")
    if actual != expected:
        raise GenerationMappingError("ai_generation_output_sequence_mismatch")
    _validate_reply_sequences(contents, is_reply=is_reply)


def _output_sequence_indexes(contents: list[str]) -> list[int]:
    return [
        int(getattr(content, "sequence_index", index) or index)
        for index, content in enumerate(contents, 1)
    ]


def _validate_reply_sequences(contents: list[str], *, is_reply: bool) -> None:
    reply_indexes = [getattr(content, "reply_to_sequence_index", None) for content in contents]
    if is_reply and any(value not in {None, index} for index, value in enumerate(reply_indexes, 1)):
        raise GenerationMappingError("ai_generation_reply_sequence_mismatch")
    if not is_reply and any(value is not None for value in reply_indexes):
        raise GenerationMappingError("ai_generation_reply_sequence_unexpected")


def _validate_reply_mapping(
    batch: list[tuple[Action, SendMessagePayload]],
    contents: list[str],
) -> None:
    for index, ((_action, payload), content) in enumerate(zip(batch, contents, strict=True), 1):
        reply_index = getattr(content, "reply_to_sequence_index", None)
        if payload.reply_to_message_id and reply_index not in {None, index}:
            raise GenerationMappingError("ai_generation_reply_sequence_mismatch")
        if not payload.reply_to_message_id and reply_index is not None:
            raise GenerationMappingError("ai_generation_reply_sequence_unexpected")


def validate_output_slot_ids(contents: list[str], generation_slots: list[dict]) -> None:
    expected = [str(slot.get("slot_id") or "").strip() for slot in generation_slots]
    actual = [str(getattr(content, "slot_id", "") or "").strip() for content in contents]
    if not all(expected) or actual != expected:
        raise GenerationMappingError("ai_generation_slot_mapping_mismatch")


def _validate_fixed_slots(
    batch: list[tuple[Action, SendMessagePayload]],
    contents: list[str],
    generation_slots: list[dict],
) -> None:
    validate_output_slot_ids(contents, generation_slots)
    if len(generation_slots) != len(batch):
        raise GenerationMappingError("ai_generation_slot_mapping_mismatch")
    for (action, payload), slot in zip(batch, generation_slots, strict=True):
        if str(slot.get("slot_id") or "") != str(payload.slot_id or ""):
            raise GenerationMappingError("ai_generation_slot_mapping_mismatch")
        if int(slot.get("account_id") or 0) != int(action.account_id or 0):
            raise GenerationMappingError("ai_generation_slot_mapping_mismatch")
        if str(slot.get("coverage_ledger_id") or "") != str(payload.coverage_ledger_id or ""):
            raise GenerationMappingError("ai_generation_slot_mapping_mismatch")


def cached_generation_result(payload: SendMessagePayload) -> tuple[str, int] | None:
    cache = payload.ai_generation_result_cache
    content = str(cache.get("content") or "").strip()
    if not content:
        return None
    metadata = {field: cache.get(field) for field in GENERATION_AUDIT_FIELDS}
    return GeneratedContent(content, **metadata), int(cache.get("tokens") or 0)


def generation_result_cache(content: str, tokens: int, attempt_id: str) -> dict:
    return apply_generated_content_metadata({
        "content": str(content or "").strip(),
        "tokens": max(0, int(tokens or 0)),
        "attempt_id": attempt_id,
    }, content)


def apply_generated_content_metadata(data: dict, content: str) -> dict:
    updated = dict(data)
    for field in GENERATION_AUDIT_FIELDS:
        value = getattr(content, field, None)
        if value is None:
            continue
        updated[field] = [dict(item) for item in value] if field == "generation_attempts" else value
    return updated


def mark_attempt_outcome(
    payload_data: dict,
    attempt_id: str,
    outcome: str,
    *,
    timestamp: datetime,
) -> None:
    history = [dict(item) for item in list(payload_data.get("ai_generation_attempt_history") or [])]
    for item in reversed(history):
        if str(item.get("attempt_id") or "") != attempt_id:
            continue
        item["outcome"] = outcome
        item["finished_at"] = timestamp.isoformat()
        break
    payload_data["ai_generation_attempt_history"] = history
