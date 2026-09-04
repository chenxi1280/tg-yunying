from __future__ import annotations

from datetime import datetime, timedelta
import logging

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ContextTurn, StageWakeOutbox, Task, TgGroup
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_conversation import (
    RESPONSE_FRESHNESS_SECONDS,
    materialize_turn,
)
from .engagement_target_scope import active_unified_group_ai_owner
from .planner_wake import wake_task_planner


WAKE_OWNER_RETRY_SECONDS = 2
WAKE_LOCK_TIMEOUT_MS = 100
logger = logging.getLogger(__name__)


def drain_conversation_wake_transactions(session_factory, *, limit=100):
    current = _naive(_now())
    with session_factory() as session:
        ids = _due_wake_ids(session, current, limit)
    count = 0
    for wake_id in ids:
        try:
            with session_factory() as session:
                wake = session.get(StageWakeOutbox, wake_id)
                if wake is not None:
                    changed = _consume_isolated(session, wake, current)
                    session.commit()
                    count += changed
        except Exception:
            logger.exception("conversation_wake_transaction_failed wake_id=%s", wake_id)
    return count


def _due_wake_ids(session, current, limit):
    statement = _due_wake_statement(session, current, limit).with_only_columns(StageWakeOutbox.id)
    candidates = statement.join(ContextTurn, ContextTurn.id == StageWakeOutbox.aggregate_id)
    ids = list(session.scalars(candidates.with_for_update(of=ContextTurn, skip_locked=True)))
    remaining = max(1, int(limit)) - len(ids)
    if remaining:
        orphan = ~select(ContextTurn.id).where(ContextTurn.id == StageWakeOutbox.aggregate_id).exists()
        ids.extend(session.scalars(statement.where(orphan).limit(remaining)))
    return ids


def drain_due_conversation_wakes(
    session: Session,
    *,
    limit: int = 100,
    now_value: datetime | None = None,
) -> int:
    current = _naive(now_value or _now())
    wakes = list(session.scalars(_due_wake_statement(session, current, limit)))
    return sum(
        _consume_isolated(session, wake, current)
        for wake in wakes
        if wake.state == "pending"
    )


def _due_wake_statement(session: Session, current: datetime, limit: int):
    statement = (
        select(StageWakeOutbox)
        .where(
            StageWakeOutbox.aggregate_type == "context_turn",
            StageWakeOutbox.stage == "close_turn",
            StageWakeOutbox.state == "pending",
            StageWakeOutbox.available_at <= current,
        )
        .order_by(StageWakeOutbox.available_at, StageWakeOutbox.created_at)
        .limit(max(1, int(limit)))
    )
    return statement


def _consume_isolated(session, wake, current):
    try:
        with session.begin_nested():
            previous_timeout = None
            if session.get_bind().dialect.name == "postgresql":
                previous_timeout = session.scalar(text("SHOW lock_timeout"))
                session.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                                {"timeout": f"{WAKE_LOCK_TIMEOUT_MS}ms"})
            changed = _consume_turn_close_wake(session, wake, current)
            session.flush()
            if previous_timeout is not None:
                session.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                                {"timeout": previous_timeout})
        return changed
    except Exception:
        logger.exception("conversation_wake_failed wake_id=%s", wake.id)
        return 0


def _consume_turn_close_wake(
    session: Session,
    wake: StageWakeOutbox,
    current: datetime,
) -> int:
    turn = session.get(ContextTurn, wake.aggregate_id)
    if turn is not None:
        turn = session.scalar(select(ContextTurn).where(ContextTurn.id == turn.id)
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if turn is None:
            return 0
    wake = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.id == wake.id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if wake is None or wake.state != "pending":
        return 0
    wake.attempt_count = int(wake.attempt_count or 0) + 1
    if turn is None:
        _settle_wake(wake, "invalid")
        return 1
    if int(wake.aggregate_revision) != int(turn.version or 1):
        _settle_wake(wake, "superseded")
        return 1
    if turn.state != "assembling":
        _settle_wake(wake, "delivered")
        return 1
    if _naive(turn.closed_at) > current:
        wake.available_at = turn.closed_at
        return 0
    task = _running_turn_owner(session, turn)
    if task is None:
        return _retry_or_expire_ownerless_wake(wake, turn, current)
    group = session.get(TgGroup, turn.target_group_id) if turn.target_group_id else None
    if group is None or group.tenant_id != turn.tenant_id:
        _settle_wake(wake, "invalid")
        return 1
    materialize_turn(session, task, turn, current=current)
    wake_task_planner(
        session,
        task,
        reason_code="conversation_turn_ready",
        not_before_at=current,
    )
    return 1


def _running_turn_owner(session: Session, turn: ContextTurn) -> Task | None:
    task_id = active_unified_group_ai_owner(
        session,
        tenant_id=turn.tenant_id,
        canonical_peer_id=turn.canonical_peer_id,
    )
    task = session.get(Task, task_id) if task_id else None
    return task if task is not None and task.status == "running" else None


def _retry_or_expire_ownerless_wake(
    wake: StageWakeOutbox,
    turn: ContextTurn,
    current: datetime,
) -> int:
    deadline = _naive(turn.closed_at) + timedelta(seconds=RESPONSE_FRESHNESS_SECONDS)
    if current >= deadline:
        _settle_wake(wake, "expired")
        return 1
    wake.available_at = min(
        current + timedelta(seconds=WAKE_OWNER_RETRY_SECONDS),
        deadline,
    )
    return 0


def _settle_wake(wake: StageWakeOutbox, state: str) -> None:
    wake.state = state
    wake.delivered_at = _now()


def _naive(value: datetime) -> datetime:
    return as_beijing(value)


__all__ = ["drain_due_conversation_wakes", "drain_conversation_wake_transactions"]
