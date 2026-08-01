from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    AiContentScopeTakeoverItem,
    AiGroupMessageMemory,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    GroupContextMessage,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
    TgGroup,
    TgGroupAccount,
)

from .group_ai_scope import (
    CONTENT_SCOPE_CONTRACT_VERSION,
    validate_group_ai_content_scope,
)
from .payloads import SendMessagePayload
from .runtime_state_hash import (
    action_state_hash,
    canonical_state_hash,
    execution_attempt_state_hash,
)


OPEN_STATUSES = frozenset({"pending", "claiming", "executing", "retryable_failed"})
SCOPE_KEYS = (
    "content_scope_contract_version",
    "content_scope_tenant_id",
    "content_scope_group_id",
    "content_scope_task_id",
)


@dataclass(frozen=True)
class TakeoverClassification:
    name: str
    input_hash: str
    reason_code: str


def preview_ai_content_scope_takeover(
    session: Session,
    *,
    cutoff_at: datetime,
    actor: str,
    dispatcher_scope: str,
    release_version: str,
    config_version: str,
    supersedes_batch_id: str | None = None,
) -> AiContentScopeTakeoverBatch:
    actions = _preview_actions(session, cutoff_at, supersedes_batch_id)
    classified = [(action, classify_takeover_action(session, action)) for action in actions]
    item_facts = [_item_fact(action, result) for action, result in classified]
    batch = AiContentScopeTakeoverBatch(
        dispatcher_scope=dispatcher_scope,
        cutoff_at=cutoff_at,
        actor=actor,
        classification_hash=canonical_state_hash(item_facts),
        classification_counts=dict(Counter(row.name for _, row in classified)),
        supersedes_batch_id=supersedes_batch_id,
        release_version=release_version,
        config_version=config_version,
    )
    session.add(batch)
    session.flush()
    session.add_all([
        AiContentScopeTakeoverItem(
            batch_id=batch.id,
            action_id=action.id,
            observed_action_state_hash=action_state_hash(action),
            classification=result.name,
            classification_input_hash=result.input_hash,
            outcome={"reason_code": result.reason_code},
        )
        for action, result in classified
    ])
    return batch


def classify_takeover_action(
    session: Session,
    action: Action,
) -> TakeoverClassification:
    attempt = _latest_attempt(session, action.id)
    facts_hash = canonical_state_hash(_classification_facts(session, action, attempt))
    if action.status == "unknown_after_send" or _gateway_started_open(action, attempt):
        return TakeoverClassification(
            "remote_reconcile_required", facts_hash, "gateway_boundary_exists",
        )
    if action.status not in OPEN_STATUSES:
        return TakeoverClassification(
            "immutable_terminal", facts_hash, "terminal_action_immutable",
        )
    payload = _parsed_payload(action)
    if payload is None:
        return TakeoverClassification(
            "quarantine", facts_hash, "legacy_payload_invalid",
        )
    binding = _binding_classification(session, action, payload)
    if binding is not None:
        name, reason = binding
        return TakeoverClassification(name, facts_hash, reason)
    scope_values = [getattr(payload, key) for key in SCOPE_KEYS]
    if all(scope_values):
        violation = validate_group_ai_content_scope(
            session, action, payload=payload, account_id=action.account_id,
        )
        if violation is None:
            return TakeoverClassification(
                "already_current", facts_hash, "scope_contract_current",
            )
        return TakeoverClassification("quarantine", facts_hash, violation.code)
    if any(scope_values):
        return TakeoverClassification(
            "quarantine", facts_hash, "partial_scope_contract",
        )
    candidate = payload.model_copy(update=_scope_snapshot(action, payload))
    violation = validate_group_ai_content_scope(
        session, action, payload=candidate, account_id=action.account_id,
    )
    if violation is None:
        return TakeoverClassification(
            "equivalent_snapshot_safe", facts_hash, "equivalent_scope_proven",
        )
    severity = _scope_violation_severity(violation.field)
    return TakeoverClassification(severity, facts_hash, violation.code)


def scope_snapshot_payload(action: Action) -> dict:
    payload = _parsed_payload(action)
    if payload is None:
        raise ValueError("legacy_payload_invalid")
    updated = dict(action.payload or {})
    updated.update(_scope_snapshot(action, payload))
    return updated


def recompute_takeover_hashes(
    session: Session,
    action: Action,
) -> tuple[str, TakeoverClassification]:
    return action_state_hash(action), classify_takeover_action(session, action)


def _preview_actions(
    session: Session,
    cutoff_at: datetime,
    supersedes_batch_id: str | None,
) -> list[Action]:
    if supersedes_batch_id:
        ids = _superseded_action_ids(session, supersedes_batch_id)
        if not ids:
            return []
        statement = select(Action).where(Action.id.in_(ids))
    else:
        statement = select(Action).where(
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.created_at <= cutoff_at,
        )
    return list(session.scalars(statement.order_by(Action.id.asc())))


def _superseded_action_ids(session: Session, batch_id: str) -> list[str]:
    batch = session.get(AiContentScopeTakeoverBatch, batch_id)
    if batch is None or batch.status not in {"blocked", "applying"}:
        raise ValueError("takeover_superseded_batch_invalid")
    return list(session.scalars(
        select(AiContentScopeTakeoverItem.action_id).where(
            AiContentScopeTakeoverItem.batch_id == batch_id,
            AiContentScopeTakeoverItem.status.in_(
                ("pending", "conflict", "quarantined"),
            ),
        ).order_by(AiContentScopeTakeoverItem.action_id.asc())
    ))


def _item_fact(action: Action, result: TakeoverClassification) -> dict:
    return {
        "action_id": action.id,
        "action_state_hash": action_state_hash(action),
        "classification": result.name,
        "classification_input_hash": result.input_hash,
    }


def _parsed_payload(action: Action) -> SendMessagePayload | None:
    try:
        return SendMessagePayload.model_validate(action.payload or {})
    except (ValidationError, ValueError, TypeError):
        return None


def _binding_classification(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> tuple[str, str] | None:
    ids = (
        str(action.content_mix_cycle_slot_id or ""),
        str(action.primary_quantity_slot_id or ""),
        str(payload.content_mix_cycle_slot_id or ""),
        str(payload.primary_quantity_slot_id or ""),
    )
    if not all(ids):
        return "replan_required", "content_mix_binding_missing"
    if len(set((ids[0], ids[2]))) != 1 or len(set((ids[1], ids[3]))) != 1:
        return "quarantine", "content_mix_binding_conflict"
    rows = _binding_rows(session, action)
    if rows is None:
        return "replan_required", "content_mix_binding_fact_missing"
    cycle_slot, quantity, cycle = rows
    if _binding_ownership_conflicts(action, cycle_slot, quantity, cycle):
        return "quarantine", "content_mix_binding_ownership_conflict"
    return _coverage_binding_classification(session, action, payload, quantity)


def _binding_rows(
    session: Session,
    action: Action,
) -> tuple[ContentMixCycleSlot, TaskGroupDailyMessageSlot, ContentMixCycle] | None:
    cycle_slot_id = str(action.content_mix_cycle_slot_id or "")
    quantity_id = str(action.primary_quantity_slot_id or "")
    if not cycle_slot_id or not quantity_id:
        return None
    cycle_slot = session.get(ContentMixCycleSlot, cycle_slot_id)
    quantity = session.get(TaskGroupDailyMessageSlot, quantity_id)
    cycle = session.get(ContentMixCycle, cycle_slot.cycle_id) if cycle_slot else None
    if cycle_slot is None or quantity is None or cycle is None:
        return None
    return cycle_slot, quantity, cycle


def _binding_ownership_conflicts(action, cycle_slot, quantity, cycle) -> bool:
    return bool(
        cycle_slot.primary_quantity_slot_id != quantity.id
        or cycle_slot.current_action_id != action.id
        or quantity.task_id != action.task_id
        or quantity.tenant_id != action.tenant_id
        or cycle.task_id != action.task_id
        or cycle.tenant_id != action.tenant_id
        or cycle.task_day_ledger_id != quantity.task_day_ledger_id
    )


def _coverage_binding_classification(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    quantity: TaskGroupDailyMessageSlot,
) -> tuple[str, str] | None:
    expected_id = str(quantity.task_account_daily_coverage_id or "")
    if str(payload.coverage_ledger_id or "") != expected_id:
        return "quarantine", "coverage_binding_conflict"
    if not expected_id:
        return None
    coverage = session.get(TaskAccountDailyCoverage, expected_id)
    if coverage is None:
        return "replan_required", "coverage_fact_missing"
    identity = (
        coverage.account_id == action.account_id
        and coverage.task_id == action.task_id
        and coverage.tenant_id == action.tenant_id
        and coverage.task_day_ledger_id == quantity.task_day_ledger_id
    )
    return None if identity else ("quarantine", "coverage_identity_conflict")


def _scope_snapshot(action: Action, payload: SendMessagePayload) -> dict:
    return {
        "content_scope_contract_version": CONTENT_SCOPE_CONTRACT_VERSION,
        "content_scope_tenant_id": action.tenant_id,
        "content_scope_group_id": payload.group_id,
        "content_scope_task_id": str(action.task_id or ""),
    }


def _scope_violation_severity(field: str) -> str:
    if field in {"context_message_ids", "reply_to_message_id", "ai_message_memory_id", "chat_mode"}:
        return "replan_required"
    return "quarantine"


def _gateway_started_open(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> bool:
    return bool(
        action.status in OPEN_STATUSES
        and attempt is not None
        and attempt.gateway_call_started_at is not None
    )


def _latest_attempt(session: Session, action_id: str) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action_id,
        ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1)
    )


def _classification_facts(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt | None,
) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    task = session.get(Task, action.task_id) if action.task_id else None
    group = session.get(TgGroup, payload.get("group_id")) if payload.get("group_id") else None
    account_link = _account_link(session, action, payload)
    context = _context_facts(session, action, payload)
    memory = session.get(AiGroupMessageMemory, payload.get("ai_message_memory_id")) if payload.get("ai_message_memory_id") else None
    rows = _binding_rows(session, action)
    return {
        "task": _task_fact(task),
        "group": _group_fact(group),
        "account_link_id": account_link,
        "binding": _binding_facts(rows),
        "context": context,
        "memory": _memory_fact(memory),
        "attempt_hash": execution_attempt_state_hash(attempt) if attempt else "",
    }


def _account_link(session: Session, action: Action, payload: dict) -> int | None:
    group_id = int(payload.get("group_id") or 0)
    account_id = int(action.account_id or 0)
    if not group_id or not account_id:
        return None
    return session.scalar(select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == action.tenant_id,
        TgGroupAccount.group_id == group_id,
        TgGroupAccount.account_id == account_id,
    ))


def _context_facts(session: Session, action: Action, payload: dict) -> list[dict]:
    ids = set(payload.get("context_message_ids") or [])
    ids.update(payload.get("anchor_message_ids") or [])
    if payload.get("context_snapshot_message_id"):
        ids.add(payload["context_snapshot_message_id"])
    if not ids:
        return []
    rows = session.scalars(select(GroupContextMessage).where(
        GroupContextMessage.id.in_(ids),
    ).order_by(GroupContextMessage.id.asc()))
    return [
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "group_id": row.group_id,
            "remote_message_id": row.remote_message_id,
        }
        for row in rows
    ]


def _task_fact(task: Task | None) -> dict:
    if task is None:
        return {}
    config = task.type_config if isinstance(task.type_config, dict) else {}
    return {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "type": task.type,
        "target_group_id": config.get("target_group_id"),
        "target_operation_target_id": config.get("target_operation_target_id"),
    }


def _group_fact(group: TgGroup | None) -> dict:
    if group is None:
        return {}
    return {"id": group.id, "tenant_id": group.tenant_id, "peer_id": group.tg_peer_id}


def _binding_facts(rows) -> dict:
    if rows is None:
        return {}
    slot, quantity, cycle = rows
    return {
        "slot": [slot.id, slot.primary_quantity_slot_id, slot.current_action_id, slot.slot_state],
        "quantity": [quantity.id, quantity.task_id, quantity.task_day_ledger_id, quantity.task_account_daily_coverage_id, quantity.state],
        "cycle": [cycle.id, cycle.task_id, cycle.task_day_ledger_id],
    }


def _memory_fact(memory: AiGroupMessageMemory | None) -> dict:
    if memory is None:
        return {}
    return {
        "id": memory.id,
        "tenant_id": memory.tenant_id,
        "task_id": memory.task_id,
        "group_id": memory.group_id,
        "action_id": memory.action_id,
    }


__all__ = [
    "TakeoverClassification",
    "classify_takeover_action",
    "preview_ai_content_scope_takeover",
    "recompute_takeover_hashes",
    "scope_snapshot_payload",
]
