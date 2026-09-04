"""Observe late local runner termination without inventing a Telegram outcome."""
from dataclasses import dataclass
import logging
from threading import Event, Lock

from sqlalchemy import select

from app.models import Action, ExecutionAttempt
from app.services._common import _now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminationReceipt:
    tenant_id: int
    action_id: str
    attempt_id: str
    terminated: Event


class PendingTerminations:
    def __init__(self):
        self._receipts = {}
        self._lock = Lock()

    def register(self, receipt):
        with self._lock:
            previous = self._receipts.get(receipt.attempt_id)
            if previous is not None and previous != receipt:
                raise ValueError("telegram_termination_identity_conflict")
            self._receipts[receipt.attempt_id] = receipt

    def completed(self):
        with self._lock:
            return tuple(row for row in self._receipts.values() if row.terminated.is_set())

    def acknowledge(self, receipt):
        with self._lock:
            if self._receipts.get(receipt.attempt_id) == receipt:
                del self._receipts[receipt.attempt_id]


pending_terminations = PendingTerminations()


def register_termination(attempt, terminated, *, registry=pending_terminations):
    registry.register(TerminationReceipt(attempt.tenant_id, attempt.action_id, attempt.id, terminated))


def drain_telegram_terminations(session_factory, *, registry=pending_terminations):
    count = 0
    for receipt in registry.completed():
        try:
            with session_factory() as session:
                if not _persist_termination(session, receipt):
                    continue
                session.commit()
            registry.acknowledge(receipt)
            count += 1
        except Exception:
            logger.exception("telegram_termination_persist_failed attempt_id=%s", receipt.attempt_id)
    return count


def _persist_termination(session, receipt):
    action = session.scalar(select(Action).where(Action.id == receipt.action_id,
        Action.tenant_id == receipt.tenant_id).with_for_update(skip_locked=True))
    if action is None:
        return False
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.id == receipt.attempt_id,
        ExecutionAttempt.action_id == receipt.action_id, ExecutionAttempt.tenant_id == receipt.tenant_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if attempt is None or attempt.status in {"before_call", "before_gateway", "gateway_call_started"}:
        return False
    if attempt.gateway_call_started_at is None:
        raise ValueError("telegram_termination_requires_issued_attempt")
    attempt.result_snapshot = {**dict(attempt.result_snapshot or {}),
        "transport_termination_state": "acknowledged", "transport_termination_observed_at": _now().isoformat()}
    return True
