"""Validate frozen normal or independent content immediately before Gateway binding."""
from app.models import Action, GenerationJob, Task
from sqlalchemy.orm import Session

from .ai_content_runtime import AiContentRuntimeConflict, bind_candidate_to_gateway
from .ai_generation_guards import is_normal_frozen_candidate
from .ai_generation_quality import fail_generation_action
from .ai_generator import AiGenerationUnavailable
from .payloads import SendMessagePayload


def bind_ready_candidate_to_gateway(
    session: Session,
    task: Task,
    action: Action,
    payload: SendMessagePayload,
) -> None:
    if payload.emergency_selection_id:
        from .ai_group_emergency import validate_emergency_selection

        validate_emergency_selection(session, action, payload)
        return
    if payload.ai_generation_context_mode == "topic_only":
        from .ai_group_topic_binding import validate_topic_only_candidate

        validate_topic_only_candidate(session, action, payload)
    job_id = str(payload.generation_job_id or "")
    job = session.get(GenerationJob, job_id) if job_id else None
    if job is None or not job.window_slot_id:
        return
    try:
        bind_candidate_to_gateway(
            session,
            job,
            candidate_hash=str(action.candidate_hash or ""),
            allow_context_drift=is_normal_frozen_candidate(payload),
            task_config_revision=int(
                payload.content_intent_config_revision or task.config_revision or 1
            ),
        )
    except AiContentRuntimeConflict as exc:
        code = str(exc)
        fail_generation_action(action, code, code, stage=code)
        raise AiGenerationUnavailable(code) from exc
