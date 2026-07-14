from __future__ import annotations

from sqlalchemy.orm import Session

from app.services._common import _now

from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_pipeline import SlotGenerationResult
from .ai_generation_quality import fail_generation_action, store_generation_quality
from .ai_generation_state import (
    GenerationMappingError,
    apply_generated_content_metadata,
    mark_attempt_outcome,
    validate_generation_mapping,
)


def persist_generation_results(
    session: Session,
    request,
    results: list[SlotGenerationResult],
    *,
    tokens: int,
) -> None:
    batch = load_generation_batch(session, request)
    if len(results) != len(batch):
        raise GenerationMappingError("ai_generation_output_count_mismatch")
    validate_generation_mapping(
        batch,
        [result.content for result in results],
        generation_slots=list(request.config.get("generation_slots") or []),
    )
    with session.no_autoflush:
        for index, ((action, payload), result) in enumerate(zip(batch, results, strict=True)):
            if result.rejection_code:
                fail_generation_action(
                    action,
                    result.rejection_code,
                    result.rejection_detail,
                    stage="ai_generation_quality",
                )
                commit_generation_action(session, request, action)
                continue
            data = apply_generated_content_metadata(payload.model_dump(mode="json"), result.content)
            data["message_text"] = str(result.content).strip()
            data["ai_generation_status"] = "ready"
            data["ai_generation_tokens"] = int(tokens or 0) if index == 0 else 0
            data["ai_generation_result_cache"] = {}
            mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
            if not store_generation_quality(session, action, payload, data=data):
                commit_generation_action(session, request, action)
                continue
            action.payload = data
            action.result = {
                **(action.result or {}),
                "generation_stage": "generation_ready",
                "generation_outcome": "ready",
                "ai_generation_attempt_id": request.attempt_id,
                "voice_profile_anchor_rewritten": result.voice_profile_anchor_rewritten,
            }
            commit_generation_action(session, request, action)


__all__ = ["persist_generation_results"]
