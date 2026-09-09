"""Bind configuration-only topic evidence to the same generation and candidate."""
from app.models import GenerationJob, Task

from .ai_group_emergency_contract import digest


TOPIC_CONTEXT_KEY = "topic_only_context_v1"


def freeze_topic_only_context(task, action, *, job, payload) -> None:
    if payload.ai_generation_context_mode != "topic_only":
        return
    _require_empty_human_context(payload)
    _require_job_scope(task, action, job)
    snapshot = _snapshot(action, payload)
    frozen = dict(job.evaluator_evidence or {}).get(TOPIC_CONTEXT_KEY)
    if frozen is not None and frozen != snapshot:
        raise ValueError("topic_only_context_binding_changed")
    job.evaluator_evidence = {**dict(job.evaluator_evidence or {}), TOPIC_CONTEXT_KEY: snapshot}


def bind_topic_only_candidate(session, action, payload) -> None:
    if payload.ai_generation_context_mode != "topic_only":
        return
    job = session.get(GenerationJob, payload.generation_job_id)
    task = session.get(Task, action.task_id)
    _require_job_scope(task, action, job)
    if dict(job.evaluator_evidence or {}).get(TOPIC_CONTEXT_KEY) != _snapshot(action, payload):
        raise ValueError("topic_only_context_binding_missing")
    job.candidate_hash = str(action.candidate_hash or "")
    job.evaluator_evidence = {**dict(job.evaluator_evidence or {}),
                             "topic_only_candidate_hash": job.candidate_hash}


def validate_topic_only_candidate(session, action, payload) -> None:
    from .payloads import SendMessagePayload

    _require_empty_human_context(payload)
    job = session.get(GenerationJob, payload.generation_job_id)
    task = session.get(Task, action.task_id)
    _require_job_scope(task, action, job)
    evidence = dict(job.evaluator_evidence or {})
    if (evidence.get(TOPIC_CONTEXT_KEY) != _snapshot(action, payload)
            or evidence.get("topic_only_candidate_hash") != digest(payload.message_text)
            or job.candidate_hash != action.candidate_hash or job.candidate_hash != digest(payload.message_text)
            or job.state not in {"generating", "ready"}):
        raise ValueError("topic_only_candidate_binding_invalid")
    current = SendMessagePayload.model_validate(action.payload or {})
    _require_empty_human_context(current)
    if _snapshot(action, current) != _snapshot(action, payload) or current.message_text != payload.message_text:
        raise ValueError("topic_only_publication_stale")


def _require_empty_human_context(payload) -> None:
    if (payload.ai_generation_context_mode != "topic_only" or not payload.ai_generation_topic_direction
            or payload.reply_to_message_id or payload.conversation_turn_claim_id or payload.interaction_opportunity_id
            or payload.ai_generation_history or payload.context_message_ids or payload.anchor_message_ids
            or payload.context_snapshot_message_id):
        raise ValueError("topic_only_human_context_invalid")


def _require_job_scope(task, action, job) -> None:
    if task is None or job is None or task.type != "group_ai_chat":
        raise ValueError("topic_only_generation_scope_invalid")
    if ((task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1"
            or (job.tenant_id, job.task_id, job.task_lifecycle_epoch, job.obligation_type, job.obligation_id)
            != (action.tenant_id, action.task_id, action.task_lifecycle_epoch, action.obligation_type, action.obligation_id)
            or task.task_lifecycle_epoch != action.task_lifecycle_epoch):
        raise ValueError("topic_only_generation_scope_invalid")


def _snapshot(action, payload) -> dict:
    return {"tenant_id": action.tenant_id, "task_id": action.task_id, "action_id": action.id,
            "account_id": action.account_id, "task_lifecycle_epoch": action.task_lifecycle_epoch,
            "generation_job_id": payload.generation_job_id, "primary_quantity_slot_id": action.primary_quantity_slot_id,
            "group_id": payload.group_id, "config_revision": payload.content_intent_config_revision,
            "topic": dict(payload.ai_generation_topic_direction),
            "topic_hash": digest(payload.ai_generation_topic_direction), "evidence_source": "configured_topic"}


def freeze_topic_only_batch(task, batch, *, jobs):
    for job, (action, payload) in zip(jobs, batch):
        freeze_topic_only_context(task, action, job=job, payload=payload)
