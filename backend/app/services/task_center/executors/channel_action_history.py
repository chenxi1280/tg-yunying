from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, Task


ACTIVE_ACTION_STATUSES = ("pending", "executing", "success")
HISTORY_ACTION_STATUSES = (*ACTIVE_ACTION_STATUSES, "failed")


@dataclass(frozen=True)
class ChannelViewDailyCounts:
    total: int
    by_account: dict[int, int]


def channel_message_account_ids_for_messages(
    session: Session,
    task: Task,
    action_type: str,
    messages: list[ChannelMessage],
    *,
    execution_date: str | None = None,
    include_skipped_codes: set[str] | None = None,
) -> dict[int, set[int]]:
    account_ids_by_message = {message.id: set() for message in messages}
    if not account_ids_by_message:
        return account_ids_by_message
    statement = union_all(
        *_account_history_queries(
            session,
            task,
            action_type=action_type,
            messages=messages,
            execution_date=execution_date,
            include_skipped_codes=include_skipped_codes,
        )
    )
    lookups = _message_lookups(messages)
    for account_id, message_key, identifier_kind in session.execute(statement):
        message_id = lookups[str(identifier_kind)].get(str(message_key))
        if message_id is not None:
            account_ids_by_message[message_id].add(int(account_id))
    return account_ids_by_message


def channel_message_success_counts(
    session: Session,
    task: Task,
    action_type: str,
    messages: list[ChannelMessage],
) -> dict[int, int]:
    channel_key, _message_key, _execution_date_key = _payload_keys()
    message_keys = _identifier_values(session, [message.id for message in messages])
    rows = session.execute(
        select(channel_key, func.count(Action.id))
        .where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status == "success",
            channel_key.in_(message_keys),
        )
        .group_by(channel_key)
    )
    message_lookup = {str(message.id): message.id for message in messages}
    return {
        message_lookup[str(message_key)]: int(count)
        for message_key, count in rows
        if message_key is not None and str(message_key) in message_lookup
    }


def channel_view_daily_action_counts(session: Session, task: Task, execution_date: str) -> ChannelViewDailyCounts:
    _channel_key, _message_key, execution_date_key = _payload_keys()
    rows = session.execute(
        select(Action.account_id, func.count(Action.id))
        .where(
            Action.task_id == task.id,
            Action.action_type == "view_message",
            Action.status.in_(ACTIVE_ACTION_STATUSES),
            execution_date_key == execution_date,
        )
        .group_by(Action.account_id)
    )
    by_account: dict[int, int] = {}
    total = 0
    for account_id, count in rows:
        total += int(count)
        if account_id is not None:
            by_account[int(account_id)] = int(count)
    return ChannelViewDailyCounts(total=total, by_account=by_account)


def _payload_keys():
    return (
        Action.payload["channel_message_id"].as_string(),
        Action.payload["message_id"].as_string(),
        Action.payload["execution_date"].as_string(),
    )


def _identifier_values(session: Session, values: list[int]) -> list[int] | list[str]:
    if session.get_bind().dialect.name == "postgresql":
        return [str(value) for value in values]
    return values


def _account_history_queries(
    session: Session,
    task: Task,
    *,
    action_type: str,
    messages: list[ChannelMessage],
    execution_date: str | None,
    include_skipped_codes: set[str] | None,
) -> list:
    channel_key, message_key, execution_date_key = _payload_keys()
    identities = (
        ("channel", channel_key, _identifier_values(session, [message.id for message in messages])),
        ("legacy", message_key, _identifier_values(session, [message.message_id for message in messages if message.message_id is not None])),
    )
    statuses = ACTIVE_ACTION_STATUSES if execution_date else HISTORY_ACTION_STATUSES
    status_groups = [(statuses, None)]
    if include_skipped_codes:
        status_groups.append((("skipped",), include_skipped_codes))
    queries = []
    for query_statuses, skipped_codes in status_groups:
        for identifier_kind, identifier, identifier_values in identities:
            if not identifier_values:
                continue
            statement = select(
                Action.account_id,
                identifier.label("message_key"),
                literal(identifier_kind).label("identifier_kind"),
            ).where(
                Action.task_id == task.id,
                Action.action_type == action_type,
                Action.account_id.is_not(None),
                Action.status.in_(query_statuses),
                identifier.in_(identifier_values),
            )
            if execution_date:
                statement = statement.where(execution_date_key == execution_date)
            if skipped_codes:
                statement = statement.where(Action.result["error_code"].as_string().in_(sorted(skipped_codes)))
            queries.append(statement)
    return queries


def _message_lookups(messages: list[ChannelMessage]) -> dict[str, dict[str, int]]:
    return {
        "channel": {str(message.id): message.id for message in messages},
        "legacy": {str(message.message_id): message.id for message in messages if message.message_id is not None},
    }
