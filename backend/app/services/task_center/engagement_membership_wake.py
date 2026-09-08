"""Deliver account-group changes without coupling independent Task transactions."""
from datetime import timedelta
import logging

from sqlalchemy import select, text

from app.models import StageWakeOutbox
from app.services._common import _now
from app.timezone import as_beijing_aware
from app.services.account_group_revisions import MEMBERSHIP_WAKE_STAGE
from .engagement_membership_wake_delivery import (
    TASK_MEMBERSHIP_WAKE_STAGE, consume_membership_wake, consume_task_membership_wake,
    settle_membership_wake,
)

WAKE_LOCK_TIMEOUT_MS = 100
WAKE_RETRY_SECONDS = 2
logger = logging.getLogger(__name__)


def drain_membership_wake_transactions(session_factory, *, limit=100):
    current = as_beijing_aware(_now())
    for stage, consumer in ((MEMBERSHIP_WAKE_STAGE, consume_membership_wake),
            (TASK_MEMBERSHIP_WAKE_STAGE, consume_task_membership_wake)):
        with session_factory() as session:
            ids = _due_ids(session, stage, current=current, limit=limit)
        for wake_id in ids:
            _deliver_one(session_factory, wake_id, consumer=consumer, current=current)
    with session_factory() as session:
        ids = list(session.scalars(select(StageWakeOutbox.id).where(
            StageWakeOutbox.stage == MEMBERSHIP_WAKE_STAGE, StageWakeOutbox.state == "expanded")
            .order_by(StageWakeOutbox.available_at, StageWakeOutbox.id).limit(limit)))
    return sum(_deliver_one(session_factory, identity, consumer=settle_membership_wake, current=current)
        for identity in ids)


def _due_ids(session, stage, *, current, limit):
    return list(session.scalars(select(StageWakeOutbox.id).where(
        StageWakeOutbox.stage == stage, StageWakeOutbox.state == "pending",
        StageWakeOutbox.available_at <= current)
        .order_by(StageWakeOutbox.available_at, StageWakeOutbox.created_at, StageWakeOutbox.id)
        .limit(limit).with_for_update(skip_locked=True)))


def _deliver_one(session_factory, wake_id, *, consumer, current):
    try:
        with session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": f"{WAKE_LOCK_TIMEOUT_MS}ms"})
            changed = consumer(session, wake_id, current)
            session.commit()
            return changed
    except Exception as error:
        logger.exception("membership_wake_transaction_failed wake_id=%s", wake_id)
        retry = getattr(getattr(error, "orig", None), "sqlstate", None) == "55P03"
        _record_delivery_error(session_factory, wake_id, retry=retry, current=current)
        return 0


def _record_delivery_error(session_factory, wake_id, *, retry, current):
    with session_factory() as session:
        wake = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.id == wake_id)
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if wake is None or wake.state not in {"pending", "expanded"}:
            return
        wake.attempt_count += 1
        if retry:
            wake.available_at = current + timedelta(seconds=WAKE_RETRY_SECONDS)
        else:
            wake.state, wake.delivered_at = "failed", current
        session.commit()
