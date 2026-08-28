from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    OperationTarget,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)

from .pacing import (
    cumulative_pacing_due,
    fixed_interval_is_immediate,
    task_pacing_anchor,
)


def ensure_channel_view_targets(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    ledger: TaskDayLedger,
    messages: list[ChannelMessage],
    config: dict,
    now: datetime,
) -> dict[int, ChannelViewDailyMessageTarget]:
    existing = _targets_by_message(session, ledger.id)
    materialized_message_ids = set(session.scalars(
        select(ViewFulfillmentObligation.channel_message_id).where(
            ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
        )
    ))
    source_messages = _source_messages(session, messages, materialized_message_ids)
    candidates = _target_candidates(
        source_messages,
        existing,
        materialized_message_ids,
        config=config,
        now=now,
    )
    baselines = _fact_baselines_at_attach(
        session,
        ledger,
        candidates,
        target_peer_id=channel.tg_peer_id,
        tenant_id=task.tenant_id,
    )
    for message in candidates:
        target = _new_target(
            task,
            channel,
            message,
            ledger=ledger,
            config=config,
            baseline=baselines.get(message.id, FactAttachBaseline()),
            now=now,
        )
        existing[message.id] = _insert_or_read(session, target)
    _refresh_targets(existing, ledger, task.pacing_config or {}, now=now)
    return existing


def _refresh_targets(
    targets: dict[int, ChannelViewDailyMessageTarget],
    ledger: TaskDayLedger,
    pacing_config: dict,
    *,
    now: datetime,
) -> None:
    for target in targets.values():
        due = channel_view_target_due(target, ledger, pacing_config, now=now)
        target.due_count = max(int(target.due_count or 0), due)
        target.source_state = "expired" if _at_or_after(now, target.active_until) else "active"


def _source_messages(
    session: Session,
    messages: list[ChannelMessage],
    materialized_message_ids: set[int],
) -> dict[int, ChannelMessage]:
    source_messages = {message.id: message for message in messages}
    if not materialized_message_ids:
        return source_messages
    for message in session.scalars(select(ChannelMessage).where(
        ChannelMessage.id.in_(materialized_message_ids),
    )):
        source_messages[message.id] = message
    return source_messages


def _target_candidates(
    source_messages: dict[int, ChannelMessage],
    existing: dict[int, ChannelViewDailyMessageTarget],
    materialized_message_ids: set[int],
    *,
    config: dict,
    now: datetime,
) -> list[ChannelMessage]:
    return [
        message for message in source_messages.values()
        if message.id not in existing
        and (message.id in materialized_message_ids or _eligible_at(message, config, now))
    ]


def target_messages(
    session: Session,
    targets: dict[int, ChannelViewDailyMessageTarget],
) -> list[ChannelMessage]:
    if not targets:
        return []
    rows = session.scalars(
        select(ChannelMessage)
        .where(ChannelMessage.id.in_(targets))
        .order_by(ChannelMessage.published_at.desc(), ChannelMessage.id.desc())
    )
    return list(rows)


def channel_view_target_due(
    target: ChannelViewDailyMessageTarget,
    ledger: TaskDayLedger,
    pacing_config: dict,
    *,
    now: datetime,
) -> int:
    quantity = int(target.effective_target_snapshot or 0)
    if quantity <= 0:
        return 0
    immediate = fixed_interval_is_immediate(pacing_config)
    if (pacing_config.get("mode") or "template") == "fixed" and immediate:
        return quantity
    deadline = _same_timezone(target.active_until, now)
    as_of = min(now, deadline) if deadline is not None else now
    period_end = _same_timezone(ledger.deadline_at, now)
    return cumulative_pacing_due(
        quantity,
        pacing_config,
        anchor_at=_same_timezone(target.accrual_anchor_at, now) or now,
        period_start_at=_same_timezone(ledger.period_start_at, now) or now,
        period_end_at=period_end,
        now=as_of,
    )


def _targets_by_message(
    session: Session,
    ledger_id: str,
) -> dict[int, ChannelViewDailyMessageTarget]:
    rows = session.scalars(
        select(ChannelViewDailyMessageTarget)
        .where(ChannelViewDailyMessageTarget.task_day_ledger_id == ledger_id)
        .with_for_update()
    )
    return {row.channel_message_id: row for row in rows}


def _fact_baselines_at_attach(
    session: Session,
    ledger: TaskDayLedger,
    messages: list[ChannelMessage],
    *,
    target_peer_id: str,
    tenant_id: int,
) -> dict[int, "FactAttachBaseline"]:
    message_ids = [message.id for message in messages]
    if not message_ids:
        return {}
    cumulative_rows = session.execute(
        select(ViewRemoteFact.channel_message_id, func.count(ViewRemoteFact.id))
        .where(
            ViewRemoteFact.tenant_id == tenant_id,
            ViewRemoteFact.target_peer_id == target_peer_id,
            ViewRemoteFact.channel_message_id.in_(message_ids),
        )
        .group_by(ViewRemoteFact.channel_message_id)
    )
    cumulative = {int(message_id): int(count) for message_id, count in cumulative_rows}
    ledger_rows = session.execute(
        select(ViewFulfillmentObligation.channel_message_id, func.count(ViewRemoteFact.id))
        .join(ViewRemoteFact, ViewRemoteFact.obligation_id == ViewFulfillmentObligation.id)
        .where(ViewFulfillmentObligation.task_day_ledger_id == ledger.id)
        .group_by(ViewFulfillmentObligation.channel_message_id)
    )
    ledger_counts = {int(message_id): int(count) for message_id, count in ledger_rows}
    return {
        message_id: FactAttachBaseline(
            lifetime_confirmed=int(cumulative.get(message_id, 0)),
            ledger_confirmed=int(ledger_counts.get(message_id, 0)),
        )
        for message_id in message_ids
    }


def _new_target(
    task: Task,
    channel: OperationTarget,
    message: ChannelMessage,
    *,
    ledger: TaskDayLedger,
    config: dict,
    baseline: "FactAttachBaseline",
    now: datetime,
) -> ChannelViewDailyMessageTarget:
    daily_target = int(
        config.get("per_message_daily_view_target")
        or config.get("target_views_per_message")
        or 1
    )
    raw_total = config.get("per_message_total_view_target")
    if raw_total is None or str(raw_total).strip() == "" or int(raw_total) <= 0:
        total_target = 0
        effective_target = daily_target
    else:
        total_target = max(daily_target, int(raw_total))
        effective_target = (
            daily_target
            if baseline.lifetime_confirmed < total_target
            else 0
        )
    anchor = task_pacing_anchor(task)
    anchor_at = max(anchor, message.created_at) if anchor else message.created_at
    return ChannelViewDailyMessageTarget(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_peer_id=channel.tg_peer_id,
        channel_message_id=message.id,
        target_revision=int(task.config_revision or 1),
        daily_target_snapshot=daily_target,
        total_target_snapshot=total_target,
        lifetime_confirmed_at_attach=baseline.lifetime_confirmed,
        ledger_confirmed_at_attach=baseline.ledger_confirmed,
        effective_target_snapshot=effective_target,
        accrual_anchor_at=anchor_at,
        active_until=_active_until(message, config, ledger),
        created_at=now,
        updated_at=now,
    )


@dataclass(frozen=True)
class FactAttachBaseline:
    lifetime_confirmed: int = 0
    ledger_confirmed: int = 0


def _insert_or_read(
    session: Session,
    target: ChannelViewDailyMessageTarget,
) -> ChannelViewDailyMessageTarget:
    try:
        with session.begin_nested():
            session.add(target)
            session.flush()
        return target
    except IntegrityError:
        existing = session.scalar(
            select(ChannelViewDailyMessageTarget)
            .where(
                ChannelViewDailyMessageTarget.task_day_ledger_id
                == target.task_day_ledger_id,
                ChannelViewDailyMessageTarget.target_peer_id == target.target_peer_id,
                ChannelViewDailyMessageTarget.channel_message_id
                == target.channel_message_id,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("channel_view_target_conflict_without_row")
        return existing


def _eligible_at(message: ChannelMessage, config: dict, now: datetime) -> bool:
    active_days = int(config.get("message_active_days") or 0)
    if active_days <= 0 or message.published_at is None:
        return True
    return not _at_or_after(now, message.published_at + timedelta(days=active_days))


def _active_until(
    message: ChannelMessage,
    config: dict,
    ledger: TaskDayLedger,
) -> datetime:
    active_days = int(config.get("message_active_days") or 0)
    if active_days <= 0 or message.published_at is None:
        return ledger.deadline_at
    expires = _same_timezone(
        message.published_at + timedelta(days=active_days),
        ledger.deadline_at,
    )
    return min(expires, ledger.deadline_at) if expires is not None else ledger.deadline_at


def _at_or_after(left: datetime, right: datetime) -> bool:
    matched = _same_timezone(right, left)
    return matched is not None and left >= matched


def _same_timezone(value: datetime | None, reference: datetime) -> datetime | None:
    if value is None or value.tzinfo is reference.tzinfo:
        return value
    if reference.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value.astimezone(reference.tzinfo)


__all__ = [
    "channel_view_target_due",
    "ensure_channel_view_targets",
    "target_messages",
]
