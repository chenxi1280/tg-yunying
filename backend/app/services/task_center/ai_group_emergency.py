"""Select one deterministic emergency body without replaying a Telegram request."""
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select

from app.models import (
    Action, AiGroupEmergencySelection, AiGroupMessageMemory, GenerationJob,
    Task, TaskAccountDailyCoverage, TaskDayLedger, TaskGroupDailyMessageSlot,
)
from app.services._common import _now
from app.timezone import as_beijing

from .ai_group_emergency_contract import (
    DIRECT_SOURCE, EMERGENCY_SOURCES, PENDING_STATUS, POLICY_VERSION, REPLY_SOURCE,
    UNICODE_POOL_VERSION, UNKNOWN_STATUS, digest, emergency_enabled, is_response_payload, selected_content,
    selection_identity, telegram_unattempted, unknown_content_evidence,
)
from .ai_message_memory_text import message_identity
from .generation_deadlines import latest_safe_send_at
from .payloads import SendMessagePayload
from .ai_group_emergency_projection import (
    advance_emergency_projection, lock_emergency_projection, validate_emergency_projection,
)


MEMORY_RETENTION = timedelta(days=30)
MEMORY_SENDABLE_STATES = frozenset({"reserved", "claiming", "executing", "unknown_after_send", "success"})


def select_emergency_content(session, task, action) -> bool:
    """Caller owns Task then Action row locks; generation CAS loses publication rights."""
    data = dict(action.payload or {})
    if data.get("emergency_selection_id"):
        validate_emergency_selection(session, action, SendMessagePayload.model_validate(data))
        return False
    if not _action_eligible(session, task, action):
        return False
    projection = lock_emergency_projection(session, action)
    if projection is None:
        return False
    job = session.scalar(select(GenerationJob).where(
        GenerationJob.id == str(data.get("generation_job_id") or ""),
    ).with_for_update())
    evidence = _generation_evidence(session, action, job)
    if evidence is None:
        return False
    reply = is_response_payload(data)
    content = selected_content(action.id, reply=reply)
    selection = _new_selection(action, data, content=content, evidence=evidence)
    session.add(selection)
    memory = _new_memory(action, selection, content=content)
    session.add(memory)
    session.flush()
    advance_emergency_projection(action, projection, selection=selection)
    _publish_selection(action, selection, memory=memory, content=content)
    session.flush()
    return True


def _action_eligible(session, task, action) -> bool:
    data = dict(action.payload or {})
    if not emergency_enabled(task) or task.status != "running" or task.deleted_at is not None:
        return False
    if (action.status != "pending" or action.claim_owner or action.lease_owner
            or action.task_lifecycle_epoch != task.task_lifecycle_epoch
            or data.get("ai_generation_status") not in {PENDING_STATUS, UNKNOWN_STATUS}
            or data.get("message_text") or not telegram_unattempted(session, action)):
        return False
    slot = session.get(TaskGroupDailyMessageSlot, action.primary_quantity_slot_id)
    ledger = session.get(TaskDayLedger, slot.task_day_ledger_id) if slot else None
    if not slot or not ledger or ledger.lifecycle_status != "open" or slot.state == "terminal":
        return False
    if ((slot.tenant_id, slot.task_id) != (action.tenant_id, action.task_id)
            or data.get("primary_quantity_slot_id") != slot.id
            or ledger.obligation_local_date != as_beijing(_now()).astimezone(ZoneInfo(ledger.timezone_snapshot)).date()):
        return False
    deadline = latest_safe_send_at(session, action)
    if deadline is None or as_beijing(_now()) >= as_beijing(deadline):
        return False
    coverage_id = str(data.get("coverage_ledger_id") or "")
    if not coverage_id:
        return True
    coverage = session.get(TaskAccountDailyCoverage, coverage_id)
    return bool(coverage and coverage.state == "reserved" and coverage.reserved_action_id == action.id
                and (coverage.tenant_id, coverage.task_id, coverage.account_id, coverage.group_id)
                == (action.tenant_id, action.task_id, action.account_id, data.get("group_id")))


def _generation_evidence(session, action, job) -> dict | None:
    if job is None or job.generation_owner_id or job.lease_expires_at:
        return None
    expected = (action.tenant_id, action.task_id, action.task_lifecycle_epoch,
                action.obligation_type, action.obligation_id)
    if (job.tenant_id, job.task_id, job.task_lifecycle_epoch, job.obligation_type, job.obligation_id) != expected:
        return None
    if job.state == "unknown" and (action.payload or {}).get("ai_generation_status") == UNKNOWN_STATUS:
        return unknown_content_evidence(session, action, job)
    if job.state == "failed" and (action.payload or {}).get("ai_generation_status") == PENDING_STATUS:
        return {"normal_generation_outcome": "failed", "generation_stage": job.generation_stage,
                "quality_evidence": dict((action.result or {}).get("emergency_generation_evidence") or {})}
    return None


def _new_selection(action, data, *, content, evidence):
    return AiGroupEmergencySelection(
        id=str(uuid4()), tenant_id=action.tenant_id, task_id=action.task_id, action_id=action.id,
        primary_quantity_slot_id=action.primary_quantity_slot_id,
        generation_job_id=str(data.get("generation_job_id") or "") or None,
        policy_version=POLICY_VERSION, previous_action_version=int(action.action_version or 1),
        materialization_version=int(action.materialization_version or 1) + 1,
        previous_payload_hash=digest(data), content_hash=digest(content),
        source=REPLY_SOURCE if is_response_payload(data) else DIRECT_SOURCE,
        reason=str((action.result or {}).get("error_code") or data["ai_generation_status"]),
        identity=selection_identity(action, data),
        evidence={**evidence, "unicode_pool_version": UNICODE_POOL_VERSION},
    )


def _new_memory(action, selection, *, content):
    normalized, fingerprint, cluster, shell = message_identity(content)
    return AiGroupMessageMemory(
        id=str(uuid4()), tenant_id=action.tenant_id, task_id=action.task_id, action_id=action.id,
        account_id=action.account_id, group_id=selection.identity["group_id"], raw_text=content,
        normalized_text=normalized, text_fingerprint=fingerprint, semantic_cluster=cluster,
        template_shell_key=shell, reservation_key=f"emergency:{selection.id}",
        status="reserved", planned_at=_now(), expires_at=_now() + MEMORY_RETENTION,
        duplicate_window="emergency_quantity_slot", quality_decision=selection.source,
        content_source=selection.source, result={"emergency_selection_id": selection.id,
            "primary_quantity_slot_id": action.primary_quantity_slot_id,
            "content_hash": selection.content_hash, "quality_degraded": True},
    )


def _publish_selection(action, selection, *, memory, content):
    data = dict(action.payload or {})
    action.payload = {**data, "message_text": content, "emergency_selection_id": selection.id,
        "ai_generation_status": "ready", "ai_generation_claim_owner": "", "ai_generation_claim_token": "",
        "ai_generation_result_cache": {}, "ai_message_memory_id": memory.id,
        "content_source": selection.source, "generation_source": POLICY_VERSION,
        "human_quality_decision": "emergency_degraded", "quality_fallback": "emergency",
        "fallback_reason": selection.reason, "media_segments": [], "material_intent": "",
        "planned_material_kind": "none", "planned_normal_text_emoji": "no"}
    action.candidate_hash = selection.content_hash
    action.materialization_version = selection.materialization_version
    action.action_version = selection.previous_action_version + 1
    action.result = {**dict(action.result or {}), "generation_outcome": "ready",
                     "emergency_selection_id": selection.id, "quality_degraded": True}


def validate_emergency_selection(session, action, payload) -> None:
    row = validate_emergency_content_binding(session, action, payload)
    if row.materialization_version != action.materialization_version:
        raise ValueError("emergency_selection_binding_invalid")
    validate_emergency_projection(session, action, selection=row)


def validate_emergency_content_binding(session, action, payload):
    row = session.get(AiGroupEmergencySelection, payload.emergency_selection_id)
    task = session.get(Task, action.task_id)
    data = payload.model_dump(mode="json")
    if (row is None or task is None or not emergency_enabled(task)
            or task.task_lifecycle_epoch != action.task_lifecycle_epoch
            or row.policy_version != POLICY_VERSION or row.identity != selection_identity(action, data)
            or row.content_hash != digest(payload.message_text) or row.content_hash != action.candidate_hash
            or payload.content_source != row.source or payload.generation_source != POLICY_VERSION
            or payload.media_segments or payload.material_intent):
        raise ValueError("emergency_selection_binding_invalid")
    current = dict(action.payload or {})
    if (current.get("emergency_selection_id") != row.id
            or current.get("message_text") != payload.message_text
            or selection_identity(action, current) != row.identity
            or current.get("media_segments") or current.get("material_intent")
            or current.get("ai_message_memory_id") != payload.ai_message_memory_id):
        raise ValueError("emergency_selection_publication_stale")
    memory = session.get(AiGroupMessageMemory, payload.ai_message_memory_id)
    if not emergency_memory_matches(session, action, memory):
        raise ValueError("emergency_message_memory_invalid")
    return row


def emergency_memory_matches(session, action, memory) -> bool:
    data = dict(action.payload or {})
    selection_id = str(data.get("emergency_selection_id") or "")
    selection = session.get(AiGroupEmergencySelection, selection_id) if selection_id else None
    if not selection or not memory:
        return False
    if not _memory_selection_identity_matches(action, selection, data=data):
        return False
    evidence = dict(memory.result or {})
    return bool(
        memory.id == data.get("ai_message_memory_id")
        and (memory.tenant_id, memory.task_id, memory.action_id, memory.account_id, memory.group_id)
        == (action.tenant_id, action.task_id, action.id, action.account_id, data.get("group_id"))
        and memory.reservation_key == f"emergency:{selection.id}"
        and memory.content_source == selection.source == data.get("content_source")
        and memory.raw_text == data.get("message_text")
        and digest(memory.raw_text) == selection.content_hash == action.candidate_hash
        and memory.status in MEMORY_SENDABLE_STATES
        and evidence.get("emergency_selection_id", selection.id) == selection.id
        and evidence.get("content_hash", selection.content_hash) == selection.content_hash
    )


def _memory_selection_identity_matches(action, selection, *, data) -> bool:
    owner = (action.tenant_id, action.task_id, action.id, action.primary_quantity_slot_id)
    if (selection.tenant_id, selection.task_id, selection.action_id, selection.primary_quantity_slot_id) != owner:
        return False
    if (selection.policy_version != POLICY_VERSION or data.get("generation_source") != POLICY_VERSION
            or selection.source not in EMERGENCY_SOURCES
            or selection.materialization_version != action.materialization_version
            or selection.generation_job_id != data.get("generation_job_id")):
        return False
    try:
        return selection.identity == selection_identity(action, data)
    except ValidationError:
        return False
