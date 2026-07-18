from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
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
    statuses = list(ACTIVE_ACTION_STATUSES if execution_date else HISTORY_ACTION_STATUSES)
    if include_skipped_codes:
        statuses.append("skipped")
    channel_key, message_key, execution_date_key = _payload_keys()
    statement = select(Action.account_id, channel_key, message_key, Action.status, Action.result).where(
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.account_id.is_not(None),
        Action.status.in_(statuses),
        _message_condition(session, messages, channel_key, message_key),
    )
    if execution_date:
        statement = statement.where(execution_date_key == execution_date)
    _add_account_history_rows(session, statement, messages, account_ids_by_message, include_skipped_codes)
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


def _message_condition(session: Session, messages: list[ChannelMessage], channel_key, message_key):
    channel_ids = _identifier_values(session, [message.id for message in messages])
    remote_ids = _identifier_values(session, [message.message_id for message in messages if message.message_id is not None])
    conditions = [channel_key.in_(channel_ids)]
    if remote_ids:
        conditions.append(message_key.in_(remote_ids))
    return or_(*conditions)


def _identifier_values(session: Session, values: list[int]) -> list[int] | list[str]:
    if session.get_bind().dialect.name == "postgresql":
        return [str(value) for value in values]
    return values


def _add_account_history_rows(
    session: Session,
    statement,
    messages: list[ChannelMessage],
    account_ids_by_message: dict[int, set[int]],
    include_skipped_codes: set[str] | None,
) -> None:
    channel_lookup = {str(message.id): message.id for message in messages}
    remote_lookup = {str(message.message_id): message.id for message in messages if message.message_id is not None}
    for account_id, channel_message_id, message_id, status, result in session.execute(statement):
        if status == "skipped" and not _skipped_code_matches(result, include_skipped_codes):
            continue
        message_ids = {
            channel_lookup.get(str(channel_message_id)),
            remote_lookup.get(str(message_id)),
        } - {None}
        for current_message_id in message_ids:
            account_ids_by_message[current_message_id].add(int(account_id))


def _skipped_code_matches(result: dict | None, include_skipped_codes: set[str] | None) -> bool:
    if not include_skipped_codes or not isinstance(result, dict):
        return False
    return str(result.get("error_code") or "") in include_skipped_codes
