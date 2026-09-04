from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TgAccountLoginBatch, TgAccountLoginBatchAttempt, TgAccountLoginBatchItem
from app.services._common import _now, audit
from app.timezone import as_beijing_aware

from .batches import TERMINAL_ITEM_STATUSES, skip_cancellable_items
from .notifications import finalize_batch_if_terminal


LEASE_SECONDS = 90
EXTERNAL_PHASES = {"authorization_probe", "code_baseline", "send_code", "wait_code", "online_readback"}
RATE_ADMISSION_PHASES = {"code_baseline"}


@dataclass(frozen=True)
class PhaseClaim:
    batch_id: int
    item_id: int
    attempt_id: int
    tenant_id: int
    generation: int
    phase: str
    lease_token: str


def claim_batch_phase(session: Session, batch_id: int) -> PhaseClaim | None:
    now = _now()
    batch = session.scalar(select(TgAccountLoginBatch).where(
        TgAccountLoginBatch.id == batch_id,
        TgAccountLoginBatch.status.in_(("queued", "running", "cancelling")),
    ).with_for_update(skip_locked=True))
    if not batch:
        return None
    if batch.status == "cancelling":
        skip_cancellable_items(session, batch.id)
    item = _next_claimable_item(session, batch.id, now)
    if not item:
        if not _has_nonterminal_item(session, batch.id):
            finalize_batch_if_terminal(session, batch.id)
            session.commit()
        return None
    if item.current_attempt_id is None:
        raise RuntimeError("batch login item current attempt is inconsistent")
    attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
    if not attempt or attempt.execution_generation != item.execution_generation:
        raise RuntimeError("batch login item current attempt is inconsistent")
    if _lease_is_active(attempt.lease_expires_at, now):
        return None
    if _remote_call_started(attempt):
        _mark_stale_call_unknown(session, batch, item, attempt)
        session.commit()
        return None
    token = uuid4().hex
    attempt.lease_token = token
    attempt.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    attempt.state_version += 1
    if attempt.phase in EXTERNAL_PHASES - RATE_ADMISSION_PHASES and not attempt.deadline_at:
        attempt.deadline_at = now + timedelta(seconds=get_settings().account_batch_login_item_deadline_seconds)
    item.status = "running"
    item.phase = attempt.phase
    item.started_at = item.started_at or now
    item.state_version += 1
    batch.status = "running" if batch.status == "queued" else batch.status
    batch.started_at = batch.started_at or now
    batch.last_claimed_at = now
    session.commit()
    return PhaseClaim(batch.id, item.id, attempt.id, item.tenant_id, item.execution_generation, attempt.phase, token)


def _lease_is_active(expires_at: datetime | None, now: datetime) -> bool:
    return bool(
        expires_at
        and as_beijing_aware(expires_at) > as_beijing_aware(now)
    )


def _next_claimable_item(session: Session, batch_id: int, now) -> TgAccountLoginBatchItem | None:
    return session.scalar(select(TgAccountLoginBatchItem).outerjoin(
        TgAccountLoginBatchAttempt,
        TgAccountLoginBatchAttempt.id == TgAccountLoginBatchItem.current_attempt_id,
    ).where(
        TgAccountLoginBatchItem.batch_id == batch_id,
        TgAccountLoginBatchItem.status.not_in(TERMINAL_ITEM_STATUSES),
        TgAccountLoginBatchItem.status != "reconciling",
        TgAccountLoginBatchItem.status != "post_initialization_waiting",
        or_(TgAccountLoginBatchItem.next_retry_at.is_(None), TgAccountLoginBatchItem.next_retry_at <= now),
        or_(
            TgAccountLoginBatchAttempt.id.is_(None),
            TgAccountLoginBatchAttempt.lease_expires_at.is_(None),
            TgAccountLoginBatchAttempt.lease_expires_at <= now,
        ),
    ).order_by(TgAccountLoginBatchItem.line_no).limit(1).with_for_update(of=TgAccountLoginBatchItem))


def _has_nonterminal_item(session: Session, batch_id: int) -> bool:
    return session.scalar(select(TgAccountLoginBatchItem.id).where(
        TgAccountLoginBatchItem.batch_id == batch_id,
        TgAccountLoginBatchItem.status.not_in(TERMINAL_ITEM_STATUSES),
    ).limit(1)) is not None


def _remote_call_started(attempt: TgAccountLoginBatchAttempt) -> bool:
    return any(value == "started" for value in (
        attempt.send_call_state,
        attempt.code_verify_call_state,
        attempt.twofa_verify_call_state,
    ))


def _mark_stale_call_unknown(
    session: Session,
    batch: TgAccountLoginBatch,
    item: TgAccountLoginBatchItem,
    attempt: TgAccountLoginBatchAttempt,
) -> None:
    now = _now()
    item.status = "reconciling"
    item.phase = "reconciling"
    item.failure_type = "login_remote_unknown"
    item.failure_detail = "远程调用结果未知，正在对账"
    item.next_retry_at = now
    item.state_version += 1
    attempt.phase = "reconciling"
    attempt.reconcile_status = "pending"
    attempt.reconcile_until_at = attempt.reconcile_until_at or now + timedelta(seconds=get_settings().account_batch_login_reconcile_seconds)
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.state_version += 1
    audit(session, tenant_id=item.tenant_id, actor="account-login-worker", action="批量登录远程结果待对账", target_type="tg_account_login_batch_item", target_id=str(item.id), detail=f"batch_id={batch.id}; generation={item.execution_generation}")


def load_claim(session: Session, claim: PhaseClaim) -> tuple[TgAccountLoginBatchItem, TgAccountLoginBatchAttempt]:
    item = session.get(TgAccountLoginBatchItem, claim.item_id)
    attempt = session.get(TgAccountLoginBatchAttempt, claim.attempt_id)
    if not item or not attempt or not claim_is_current(item, attempt, claim):
        raise RuntimeError("batch login phase claim is stale")
    return item, attempt


def claim_is_current(
    item: TgAccountLoginBatchItem,
    attempt: TgAccountLoginBatchAttempt,
    claim: PhaseClaim,
) -> bool:
    return (
        item.current_attempt_id == attempt.id
        and item.execution_generation == claim.generation
        and attempt.execution_generation == claim.generation
        and attempt.phase == claim.phase
        and attempt.lease_token == claim.lease_token
    )


def advance_claim(
    session: Session,
    claim: PhaseClaim,
    next_phase: str,
    *,
    status: str = "pending",
    next_retry_at=None,
) -> None:
    item, attempt = load_claim(session, claim)
    item.status = status
    item.phase = next_phase
    item.next_retry_at = next_retry_at
    item.state_version += 1
    attempt.phase = next_phase
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.state_version += 1


def fail_claim(session: Session, claim: PhaseClaim, failure_type: str, detail: str) -> None:
    item, attempt = load_claim(session, claim)
    item.status = "failed"
    item.phase = "failed"
    item.failure_type = failure_type
    item.failure_detail = detail
    item.finished_at = _now()
    item.next_retry_at = None
    item.state_version += 1
    attempt.phase = "failed"
    attempt.baseline_code_hmac = ""
    attempt.baseline_login_time_hmac = ""
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.state_version += 1
    audit(session, tenant_id=item.tenant_id, actor="account-login-worker", action="账号批量登录行失败", target_type="tg_account_login_batch_item", target_id=str(item.id), detail=f"generation={item.execution_generation}; failure_type={failure_type}; phone={item.phone_masked}")
    finalize_batch_if_terminal(session, item.batch_id)


def succeed_claim(session: Session, claim: PhaseClaim, *, warning: str = "") -> None:
    item, attempt = load_claim(session, claim)
    item.status = "succeeded_with_warning" if warning else "succeeded"
    item.phase = item.status
    item.warning_detail = warning
    item.failure_type = ""
    item.failure_detail = ""
    item.finished_at = _now()
    item.next_retry_at = None
    item.code_url_ciphertext = None
    item.state_version += 1
    attempt.phase = item.status
    attempt.baseline_code_hmac = ""
    attempt.baseline_login_time_hmac = ""
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.state_version += 1
    audit(session, tenant_id=item.tenant_id, actor="account-login-worker", action="账号批量登录行完成", target_type="tg_account_login_batch_item", target_id=str(item.id), detail=f"generation={item.execution_generation}; status={item.status}; phone={item.phone_masked}")
    finalize_batch_if_terminal(session, item.batch_id)


def mark_claim_unknown(session: Session, claim: PhaseClaim, call_field: str) -> None:
    item, attempt = load_claim(session, claim)
    setattr(attempt, call_field, "unknown")
    batch = session.get(TgAccountLoginBatch, item.batch_id)
    if not batch:
        raise RuntimeError("batch missing while marking remote unknown")
    _mark_stale_call_unknown(session, batch, item, attempt)


def commit_claim(session: Session) -> None:
    session.commit()


__all__ = [
    "PhaseClaim",
    "advance_claim",
    "claim_batch_phase",
    "commit_claim",
    "fail_claim",
    "load_claim",
    "mark_claim_unknown",
    "succeed_claim",
]
