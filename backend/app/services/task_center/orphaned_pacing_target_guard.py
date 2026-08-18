from __future__ import annotations

from typing import Any

from sqlalchemy import and_, select

from app.models import (
    Action,
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    OperationTarget,
    TaskDayLedger,
    ViewFulfillmentObligation,
)


def attach_target_guards(
    session,
    items: list[dict[str, Any]],
    *,
    lock: bool,
) -> list[dict[str, Any]]:
    action_ids = tuple(item["action_id"] for item in items)
    guards = _target_guards(session, action_ids, lock=lock)
    return [
        {**item, "target_guard": guards.get(item["action_id"], _missing_guard())}
        for item in items
    ]


def target_guard_blockers(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "action_id": item["action_id"],
            "blocker": "send_target_guard_invalid",
            "mismatches": item["target_guard"]["mismatches"],
        }
        for group in groups
        for item in group
        if item["target_guard"]["mismatches"]
    ]


def assert_target_guards_unchanged(session, items: list[dict[str, Any]]) -> None:
    observed = attach_target_guards(
        session,
        [{"action_id": item["action_id"]} for item in items],
        lock=True,
    )
    expected_by_id = {item["action_id"]: item["target_guard"] for item in items}
    if any(
        item["target_guard"] != expected_by_id[item["action_id"]]
        for item in observed
    ):
        raise RuntimeError("orphaned source pacing send target changed")


def _target_guards(session, action_ids: tuple[str, ...], *, lock: bool) -> dict[str, dict]:
    if not action_ids:
        return {}
    statement = _target_guard_statement(action_ids)
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=(
            Action,
            ViewFulfillmentObligation,
            TaskDayLedger,
            ChannelMessage,
            ChannelViewDailyMessageTarget,
            OperationTarget,
        ))
    guards: dict[str, dict] = {}
    duplicate_ids: set[str] = set()
    for row in session.execute(statement):
        action_id, guard = row.Action.id, _target_guard(*row)
        if action_id in guards:
            duplicate_ids.add(action_id)
        guards[action_id] = guard
    for action_id in duplicate_ids:
        guards[action_id] = {
            **guards[action_id],
            "mismatches": sorted({
                *guards[action_id]["mismatches"],
                "duplicate_target_row",
            }),
        }
    return guards


def _target_guard_statement(action_ids: tuple[str, ...]):
    return (
        select(
            Action,
            ViewFulfillmentObligation,
            TaskDayLedger,
            ChannelMessage,
            ChannelViewDailyMessageTarget,
            OperationTarget,
        )
        .join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == Action.payload["view_fulfillment_obligation_id"].as_string())
        .join(TaskDayLedger, TaskDayLedger.id == ViewFulfillmentObligation.task_day_ledger_id)
        .join(ChannelMessage, ChannelMessage.id == ViewFulfillmentObligation.channel_message_id)
        .join(ChannelViewDailyMessageTarget, and_(
            ChannelViewDailyMessageTarget.task_day_ledger_id == TaskDayLedger.id,
            ChannelViewDailyMessageTarget.channel_message_id == ChannelMessage.id,
            ChannelViewDailyMessageTarget.task_id == Action.task_id,
        ))
        .join(OperationTarget, OperationTarget.id == ChannelMessage.channel_target_id)
        .where(Action.id.in_(action_ids))
        .order_by(Action.id, ChannelViewDailyMessageTarget.id)
    )


def _target_guard(action, owner, ledger, message, target, operation_target) -> dict:
    payload = dict(action.payload or {})
    reference = dict(payload.get("target_reference_snapshot") or {})
    snapshot = {
        "task_id": action.task_id,
        "ledger_id": ledger.id,
        "owner_id": owner.id,
        "account_id": owner.account_id,
        "operation_target_id": operation_target.id,
        "target_peer_id": str(operation_target.tg_peer_id),
        "target_reference_revision": int(operation_target.reference_revision or 1),
        "channel_message_id": message.id,
        "remote_message_id": message.message_id,
        "target_id": target.id,
        "target_revision": target.target_revision,
        "daily_target": target.daily_target_snapshot,
        "total_target": target.total_target_snapshot,
        "effective_target": target.effective_target_snapshot,
        "due_count": target.due_count,
        "source_state": target.source_state,
    }
    expected = {
        "task_ledger": (action.task_id, ledger.task_id, target.task_id),
        "owner_ledger": (owner.task_day_ledger_id, payload.get("task_day_ledger_id")),
        "owner_id": (owner.id, str(payload.get("view_fulfillment_obligation_id") or "")),
        "owner_account": (owner.account_id, action.account_id),
        "channel_target": (operation_target.id, payload.get("channel_target_id")),
        "target_peer": (str(operation_target.tg_peer_id), str(payload.get("channel_id"))),
        "target_reference_peer": (str(operation_target.tg_peer_id), str(reference.get("tg_peer_id"))),
        "target_reference_revision": (int(operation_target.reference_revision or 1), int(payload.get("target_reference_revision") or 0)),
        "channel_message": (owner.channel_message_id, message.id, payload.get("channel_message_id")),
        "remote_message": (message.message_id, payload.get("message_id")),
        "daily_target": (target.daily_target_snapshot, payload.get("daily_view_target")),
        "total_target": (target.total_target_snapshot, payload.get("total_view_target")),
    }
    mismatches = sorted(key for key, values in expected.items() if len(set(values)) != 1)
    return {**snapshot, "mismatches": mismatches}


def _missing_guard() -> dict:
    return {"mismatches": ["target_guard_missing"]}
