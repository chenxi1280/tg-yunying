from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)


ACTIVE_FULFILLMENT_ACTION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)
TERMINAL_REPLAN_ACTION_STATUSES = frozenset({"failed", "skipped", "cancelled"})


@dataclass(frozen=True)
class FulfillmentDailyCounts:
    total: int
    by_account: dict[int, int]


def reaction_account_ids_for_messages(
    session: Session,
    task: Task,
    messages: list[ChannelMessage],
) -> dict[int, set[int]]:
    result = _empty_account_map(messages)
    if not result:
        return result
    message_ids = list(result)
    confirmed = session.execute(
        select(
            ReactionRemoteFact.channel_message_id,
            ReactionRemoteFact.account_id,
        ).where(
            ReactionRemoteFact.tenant_id == task.tenant_id,
            ReactionRemoteFact.channel_message_id.in_(message_ids),
        )
    )
    pending = session.execute(
        select(
            ReactionFulfillmentObligation.channel_message_id,
            ReactionFulfillmentObligation.account_id,
        )
        .join(Action, Action.id == ReactionFulfillmentObligation.current_action_id)
        .where(
            ReactionFulfillmentObligation.tenant_id == task.tenant_id,
            ReactionFulfillmentObligation.channel_message_id.in_(message_ids),
            _reaction_held_or_active(
                ReactionFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ReactionFulfillmentObligation.id,
            ),
        )
    )
    _merge_account_rows(result, [*confirmed, *pending])
    return result


def view_account_ids_for_messages(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    messages: list[ChannelMessage],
) -> dict[int, set[int]]:
    result = _empty_account_map(messages)
    if not result:
        return result
    message_ids = list(result)
    confirmed = session.execute(
        select(
            ViewRemoteFact.channel_message_id,
            ViewRemoteFact.account_id,
        ).where(
            ViewRemoteFact.tenant_id == task.tenant_id,
            ViewRemoteFact.channel_message_id.in_(message_ids),
        )
    )
    pending = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            ViewFulfillmentObligation.account_id,
        )
        .join(Action, Action.id == ViewFulfillmentObligation.current_action_id)
        .where(
            ViewFulfillmentObligation.tenant_id == task.tenant_id,
            ViewFulfillmentObligation.channel_message_id.in_(message_ids),
            _held_or_active(
                ViewFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ViewFulfillmentObligation.id,
            ),
        )
    )
    _merge_account_rows(result, [*confirmed, *pending])
    return result


def view_materialized_account_ids_for_messages(
    session: Session,
    ledger: TaskDayLedger,
    messages: list[ChannelMessage],
) -> dict[int, set[int]]:
    result = _empty_account_map(messages)
    if not result:
        return result
    confirmed = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            ViewFulfillmentObligation.account_id,
        )
        .join(ViewRemoteFact, ViewRemoteFact.obligation_id == ViewFulfillmentObligation.id)
        .where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
    )
    pending = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            ViewFulfillmentObligation.account_id,
        )
        .join(Action, Action.id == ViewFulfillmentObligation.current_action_id)
        .where(
            ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
            _held_or_active(
                ViewFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ViewFulfillmentObligation.id,
            ),
        )
    )
    _merge_account_rows(result, [*confirmed, *pending])
    return result


def reaction_source_held_by_other_action(
    session: Session,
    action: Action,
    channel_message_id: int,
) -> bool:
    held_id = session.scalar(
        select(ReactionFulfillmentObligation.id)
        .join(Action, Action.id == ReactionFulfillmentObligation.current_action_id)
        .where(
            ReactionFulfillmentObligation.tenant_id == action.tenant_id,
            ReactionFulfillmentObligation.channel_message_id == channel_message_id,
            ReactionFulfillmentObligation.account_id == action.account_id,
            ReactionFulfillmentObligation.current_action_id != action.id,
            _reaction_held_or_active(
                ReactionFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ReactionFulfillmentObligation.id,
            ),
        )
        .limit(1)
    )
    return held_id is not None


def view_source_held_by_other_action(
    session: Session,
    action: Action,
    channel_message_id: int,
) -> bool:
    held_id = session.scalar(
        select(ViewFulfillmentObligation.id)
        .join(Action, Action.id == ViewFulfillmentObligation.current_action_id)
        .where(
            ViewFulfillmentObligation.tenant_id == action.tenant_id,
            ViewFulfillmentObligation.channel_message_id == channel_message_id,
            ViewFulfillmentObligation.account_id == action.account_id,
            ViewFulfillmentObligation.current_action_id != action.id,
            _held_or_active(
                ViewFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ViewFulfillmentObligation.id,
            ),
        )
        .limit(1)
    )
    return held_id is not None


def view_confirmed_counts(
    session: Session,
    task: Task,
    messages: list[ChannelMessage],
) -> dict[int, int]:
    message_ids = [message.id for message in messages]
    if not message_ids:
        return {}
    rows = session.execute(
        select(
            ViewFulfillmentObligation.channel_message_id,
            func.count(ViewRemoteFact.id),
        )
        .join(ViewRemoteFact, ViewRemoteFact.obligation_id == ViewFulfillmentObligation.id)
        .join(
            TaskDayLedger,
            TaskDayLedger.id == ViewFulfillmentObligation.task_day_ledger_id,
        )
        .where(
            TaskDayLedger.task_id == task.id,
            ViewFulfillmentObligation.channel_message_id.in_(message_ids),
        )
        .group_by(ViewFulfillmentObligation.channel_message_id)
    )
    return {int(message_id): int(count) for message_id, count in rows}


def view_daily_counts(
    session: Session,
    ledger: TaskDayLedger,
) -> FulfillmentDailyCounts:
    confirmed = session.execute(
        select(ViewFulfillmentObligation.id, ViewFulfillmentObligation.account_id)
        .join(ViewRemoteFact, ViewRemoteFact.obligation_id == ViewFulfillmentObligation.id)
        .where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
    )
    pending = session.execute(
        select(ViewFulfillmentObligation.id, ViewFulfillmentObligation.account_id)
        .join(Action, Action.id == ViewFulfillmentObligation.current_action_id)
        .where(
            ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
            _held_or_active(
                ViewFulfillmentObligation.status,
                Action.status,
                Action.payload,
                ViewFulfillmentObligation.id,
            ),
        )
    )
    rows = {
        str(obligation_id): int(account_id)
        for obligation_id, account_id in [*confirmed, *pending]
    }
    by_account: dict[int, int] = {}
    for account_id in rows.values():
        by_account[account_id] = by_account.get(account_id, 0) + 1
    return FulfillmentDailyCounts(total=len(rows), by_account=by_account)


def _held_or_active(
    obligation_status,
    action_status,
    action_payload,
    obligation_id,
):
    return or_(
        obligation_status == "unknown",
        and_(
            obligation_status == "pending",
            action_status.notin_(TERMINAL_REPLAN_ACTION_STATUSES),
            or_(
                action_status.in_(ACTIVE_FULFILLMENT_ACTION_STATUSES),
                action_payload["view_fulfillment_obligation_id"].as_string()
                == obligation_id,
            ),
        ),
    )


def _reaction_held_or_active(
    obligation_status,
    action_status,
    action_payload,
    obligation_id,
):
    return or_(
        obligation_status.in_(("unknown", "unavailable")),
        and_(
            obligation_status == "pending",
            action_status.notin_(TERMINAL_REPLAN_ACTION_STATUSES),
            or_(
                action_status.in_(ACTIVE_FULFILLMENT_ACTION_STATUSES),
                action_payload["reaction_fulfillment_obligation_id"].as_string()
                == obligation_id,
            ),
        ),
    )


def _empty_account_map(
    messages: list[ChannelMessage],
) -> dict[int, set[int]]:
    return {message.id: set() for message in messages}


def _merge_account_rows(
    result: dict[int, set[int]],
    rows,
) -> None:
    for message_id, account_id in rows:
        if int(message_id) in result:
            result[int(message_id)].add(int(account_id))
