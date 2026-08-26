from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.models import (
    Action,
    AiGroupMessageMemory,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    GroupContextMessage,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now

from .group_ai_scope_types import GroupAiScopeFacts


BULK_QUERY_ID_CHUNK_SIZE = 5_000


@dataclass(frozen=True)
class TakeoverClassificationContext:
    latest_attempts: dict[str, ExecutionAttempt]
    scope_facts: GroupAiScopeFacts
    context_messages: dict[int, GroupContextMessage]
    quantities: dict[str, TaskGroupDailyMessageSlot]
    cycle_slots: dict[str, ContentMixCycleSlot]
    cycles: dict[str, ContentMixCycle]
    coverages: dict[str, TaskAccountDailyCoverage]


def build_takeover_classification_context(
    session: Session,
    actions: list[Action],
) -> TakeoverClassificationContext:
    payloads = {action.id: _payload(action) for action in actions}
    latest_attempts = _latest_attempts(session, actions)
    tasks = _rows_by_id(session, Task, {action.task_id for action in actions if action.task_id})
    groups = _rows_by_id(session, TgGroup, _payload_ints(payloads, "group_id"))
    targets = _operation_targets(session, tasks)
    links = _account_links(session, actions, payloads)
    context_rows = _context_rows(session, payloads)
    memories = _rows_by_id(
        session,
        AiGroupMessageMemory,
        _payload_strings(payloads, "ai_message_memory_id"),
    )
    quantities, cycle_slots, cycles, coverages = _binding_rows(session, actions)
    scope_facts = GroupAiScopeFacts(
        tasks=tasks,
        groups=groups,
        operation_targets=targets,
        context_keys=_context_keys(context_rows),
        reply_target_keys=_reply_target_keys(session, actions, payloads, context_rows),
        memories=memories,
        account_link_ids=links,
    )
    return TakeoverClassificationContext(
        latest_attempts=latest_attempts,
        scope_facts=scope_facts,
        context_messages={row.id: row for row in context_rows},
        quantities=quantities,
        cycle_slots=cycle_slots,
        cycles=cycles,
        coverages=coverages,
    )


def _payload(action: Action) -> dict:
    return action.payload if isinstance(action.payload, dict) else {}


def _rows_by_id(session: Session, model, ids: set) -> dict:
    if not ids:
        return {}
    rows = {}
    ordered_ids = sorted(ids)
    for start in range(0, len(ordered_ids), BULK_QUERY_ID_CHUNK_SIZE):
        chunk = ordered_ids[start:start + BULK_QUERY_ID_CHUNK_SIZE]
        for row in session.scalars(select(model).where(model.id.in_(chunk))):
            rows[row.id] = row
    return rows


def _payload_ints(payloads: dict[str, dict], key: str) -> set[int]:
    return {
        parsed
        for payload in payloads.values()
        if (parsed := _safe_int(payload.get(key))) is not None
    }


def _payload_strings(payloads: dict[str, dict], key: str) -> set[str]:
    return {str(payload.get(key)) for payload in payloads.values() if payload.get(key)}


def _latest_attempts(
    session: Session,
    actions: list[Action],
) -> dict[str, ExecutionAttempt]:
    action_ids = [action.id for action in actions]
    if not action_ids:
        return {}
    rows = session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
    ).order_by(
        ExecutionAttempt.action_id.asc(),
        ExecutionAttempt.attempt_no.desc(),
    ))
    latest: dict[str, ExecutionAttempt] = {}
    for row in rows:
        latest.setdefault(row.action_id, row)
    return latest


def _operation_targets(
    session: Session,
    tasks: dict[str, Task],
) -> dict[int, OperationTarget]:
    target_ids = {
        int((task.type_config or {}).get("target_operation_target_id") or 0)
        for task in tasks.values()
    }
    target_ids.discard(0)
    return _rows_by_id(session, OperationTarget, target_ids)


def _account_links(
    session: Session,
    actions: list[Action],
    payloads: dict[str, dict],
) -> dict[tuple[int, int, int], int]:
    tenants = {action.tenant_id for action in actions}
    groups = _payload_ints(payloads, "group_id")
    accounts = {int(action.account_id) for action in actions if action.account_id}
    if not tenants or not groups or not accounts:
        return {}
    rows = session.scalars(select(TgGroupAccount).where(
        TgGroupAccount.tenant_id.in_(tenants),
        TgGroupAccount.group_id.in_(groups),
        TgGroupAccount.account_id.in_(accounts),
    ))
    return {
        (row.tenant_id, row.group_id, row.account_id): row.id
        for row in rows
    }


def _context_rows(
    session: Session,
    payloads: dict[str, dict],
) -> list[GroupContextMessage]:
    context_ids = _all_context_ids(payloads)
    reply_ids = _payload_strings(payloads, "reply_to_message_id")
    rows: dict[int, GroupContextMessage] = {}
    _load_context_chunks(session, rows, "id", context_ids)
    _load_context_chunks(session, rows, "remote_message_id", reply_ids)
    return [rows[row_id] for row_id in sorted(rows)]


def _load_context_chunks(
    session: Session,
    rows: dict[int, GroupContextMessage],
    field_name: str,
    values: set,
) -> None:
    ordered = sorted(values)
    field = getattr(GroupContextMessage, field_name)
    for start in range(0, len(ordered), BULK_QUERY_ID_CHUNK_SIZE):
        chunk = ordered[start:start + BULK_QUERY_ID_CHUNK_SIZE]
        for row in session.scalars(select(GroupContextMessage).where(field.in_(chunk))):
            rows[row.id] = row


def _all_context_ids(payloads: dict[str, dict]) -> set[int]:
    ids: set[int] = set()
    for payload in payloads.values():
        ids.update(_safe_ints(payload.get("context_message_ids")))
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


def _binding_rows(session: Session, actions: list[Action]) -> tuple[dict, dict, dict, dict]:
    quantity_ids = {str(row.primary_quantity_slot_id) for row in actions if row.primary_quantity_slot_id}
    cycle_slot_ids = {str(row.content_mix_cycle_slot_id) for row in actions if row.content_mix_cycle_slot_id}
    quantities = _rows_by_id(session, TaskGroupDailyMessageSlot, quantity_ids)
    cycle_slots = _rows_by_id(session, ContentMixCycleSlot, cycle_slot_ids)
    cycle_ids = {row.cycle_id for row in cycle_slots.values()}
    cycles = _rows_by_id(session, ContentMixCycle, cycle_ids)
    coverage_ids = {
        row.task_account_daily_coverage_id
        for row in quantities.values()
        if row.task_account_daily_coverage_id
    }
    coverages = _rows_by_id(session, TaskAccountDailyCoverage, coverage_ids)
    return quantities, cycle_slots, cycles, coverages


def _context_keys(rows: list[GroupContextMessage]) -> frozenset[tuple[int, int, int]]:
    return frozenset((row.id, row.tenant_id, row.group_id) for row in rows)


def _reply_target_keys(
    session: Session,
    actions: list[Action],
    payloads: dict[str, dict],
    context_rows: list[GroupContextMessage],
) -> frozenset[tuple[int, str, int, str]]:
    requested = _requested_reply_keys(actions, payloads)
    if not requested:
        return frozenset()
    found = _human_reply_keys(requested, context_rows)
    found.update(_own_history_reply_keys(session, requested))
    return frozenset(found)


def _requested_reply_keys(
    actions: list[Action],
    payloads: dict[str, dict],
) -> set[tuple[int, str, int, str]]:
    return {
        (
            action.tenant_id,
            str(action.task_id or ""),
            _safe_int(payloads[action.id].get("group_id")) or 0,
            str(payloads[action.id].get("reply_to_message_id") or ""),
        )
        for action in actions
        if payloads[action.id].get("reply_to_message_id")
    }


def _human_reply_keys(
    requested: set[tuple[int, str, int, str]],
    rows: list[GroupContextMessage],
) -> set[tuple[int, str, int, str]]:
    human = {
        (row.tenant_id, row.group_id, str(row.remote_message_id))
        for row in rows
        if not row.is_bot and row.content != ""
    }
    return {
        key for key in requested if (key[0], key[2], key[3]) in human
    }


def _own_history_reply_keys(
    session: Session,
    requested: set[tuple[int, str, int, str]],
) -> set[tuple[int, str, int, str]]:
    candidate = aliased(ExecutionAttempt)
    newer = aliased(ExecutionAttempt)
    remote_ids = {key[3] for key in requested}
    tenant_ids = {key[0] for key in requested}
    task_ids = {key[1] for key in requested}
    latest_success = ~exists(select(newer.id).where(
        newer.action_id == candidate.action_id,
        newer.status == "success",
        newer.remote_message_id != "",
        newer.attempt_no > candidate.attempt_no,
    ))
    rows = session.execute(select(Action, candidate.remote_message_id).join(
        candidate, candidate.action_id == Action.id,
    ).where(
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.tenant_id.in_(tenant_ids),
        Action.task_id.in_(task_ids),
        Action.status == "success",
        Action.executed_at.is_not(None),
        Action.executed_at >= _reply_history_start(),
        candidate.status == "success",
        candidate.remote_message_id.in_(remote_ids),
        latest_success,
    ))
    return _matching_own_reply_keys(requested, rows)


def _reply_history_start():
    days = max(1, int(get_settings().ai_reply_target_history_window_days))
    return _now() - timedelta(days=days)


def _matching_own_reply_keys(requested, rows) -> set[tuple[int, str, int, str]]:
    found = set()
    for action, remote_id in rows:
        payload = _payload(action)
        if not str(payload.get("message_text") or "").strip():
            continue
        key = (
            action.tenant_id,
            str(action.task_id or ""),
            _safe_int(payload.get("group_id")) or 0,
            str(remote_id),
        )
        if key in requested:
            found.add(key)
    return found


__all__ = [
    "TakeoverClassificationContext",
    "build_takeover_classification_context",
]
