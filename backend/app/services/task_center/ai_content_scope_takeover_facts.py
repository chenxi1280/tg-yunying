from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    GroupContextMessage,
    Task,
    TgGroup,
    TgGroupAccount,
)

from .ai_content_scope_takeover_context import TakeoverClassificationContext
from .runtime_state_hash import execution_attempt_state_hash


def classification_facts(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt | None,
    *,
    task: Task | None,
    binding_rows,
    quantity,
    coverage,
    context: TakeoverClassificationContext | None,
) -> dict:
    payload = action.payload if isinstance(action.payload, dict) else {}
    group = _group(session, payload, context)
    account_link = _account_link(session, action, payload, context)
    context_rows = _context_facts(session, payload, context)
    memory = _memory(session, payload, context)
    return {
        "task": _task_fact(task),
        "group": _group_fact(group),
        "account_link_id": account_link,
        "binding": _binding_facts(binding_rows, quantity, coverage),
        "context": context_rows,
        "memory": _memory_fact(memory),
        "attempt_hash": execution_attempt_state_hash(attempt) if attempt else "",
    }


def _group(
    session: Session,
    payload: dict,
    context: TakeoverClassificationContext | None,
) -> TgGroup | None:
    group_id = _safe_int(payload.get("group_id")) or 0
    if not group_id:
        return None
    if context:
        return context.scope_facts.groups.get(group_id)
    return session.get(TgGroup, group_id)


def _account_link(
    session: Session,
    action: Action,
    payload: dict,
    context: TakeoverClassificationContext | None,
) -> int | None:
    group_id = _safe_int(payload.get("group_id")) or 0
    account_id = int(action.account_id or 0)
    if not group_id or not account_id:
        return None
    if context:
        return context.scope_facts.account_link_ids.get(
            (action.tenant_id, group_id, account_id),
        )
    return session.scalar(select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == action.tenant_id,
        TgGroupAccount.group_id == group_id,
        TgGroupAccount.account_id == account_id,
    ))


def _context_facts(
    session: Session,
    payload: dict,
    context: TakeoverClassificationContext | None,
) -> list[dict]:
    ids = _context_ids(payload)
    if not ids:
        return []
    if context:
        rows = [
            context.context_messages[row_id]
            for row_id in sorted(ids)
            if row_id in context.context_messages
        ]
    else:
        rows = session.scalars(select(GroupContextMessage).where(
            GroupContextMessage.id.in_(ids),
        ).order_by(GroupContextMessage.id.asc()))
    return [_context_fact(row) for row in rows]


def _context_ids(payload: dict) -> set[int]:
    ids = _safe_ints(payload.get("context_message_ids"))
    ids.update(_safe_ints(payload.get("anchor_message_ids")))
    snapshot_id = _safe_int(payload.get("context_snapshot_message_id"))
    if snapshot_id is not None:
        ids.add(snapshot_id)
    return ids


def _safe_ints(value: object) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {parsed for item in value if (parsed := _safe_int(item)) is not None}


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _context_fact(row: GroupContextMessage) -> dict:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "group_id": row.group_id,
        "remote_message_id": row.remote_message_id,
    }


def _memory(
    session: Session,
    payload: dict,
    context: TakeoverClassificationContext | None,
) -> AiGroupMessageMemory | None:
    memory_id = str(payload.get("ai_message_memory_id") or "")
    if not memory_id:
        return None
    if context:
        return context.scope_facts.memories.get(memory_id)
    return session.get(AiGroupMessageMemory, memory_id)


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
        "fulfillment_contract_version": task.fulfillment_contract_version,
    }


def _group_fact(group: TgGroup | None) -> dict:
    if group is None:
        return {}
    return {"id": group.id, "tenant_id": group.tenant_id, "peer_id": group.tg_peer_id}


def _binding_facts(rows, quantity, coverage) -> dict:
    slot = rows[0] if rows else None
    cycle = rows[2] if rows else None
    return {
        "slot": _slot_fact(slot),
        "quantity": _quantity_fact(quantity),
        "cycle": _cycle_fact(cycle),
        "coverage": _coverage_fact(coverage),
    }


def _slot_fact(slot) -> list:
    if slot is None:
        return []
    return [slot.id, slot.primary_quantity_slot_id, slot.current_action_id, slot.slot_state]


def _quantity_fact(quantity) -> list:
    if quantity is None:
        return []
    return [
        quantity.id,
        quantity.tenant_id,
        quantity.task_id,
        quantity.task_day_ledger_id,
        quantity.task_account_daily_coverage_id,
        quantity.state,
    ]


def _cycle_fact(cycle) -> list:
    if cycle is None:
        return []
    return [cycle.id, cycle.tenant_id, cycle.task_id, cycle.task_day_ledger_id]


def _coverage_fact(coverage) -> list:
    if coverage is None:
        return []
    return [
        coverage.id,
        coverage.tenant_id,
        coverage.task_id,
        coverage.account_id,
        coverage.task_day_ledger_id,
    ]


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


__all__ = ["classification_facts"]
