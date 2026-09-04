from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.models import (
    AccountPool,
    AccountStatus,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
    TgLoginFlow,
)
from app.security import decrypt_secret, encrypt_session
from app.services._common import _now, gateway
from app.services.account_profile_auto_init import queue_login_profile_initialization
from app.services.account_usage_policy import sync_account_usage
from app.services.developer_apps import credentials_for_account
from app.timezone import as_beijing_aware

from .notifications import finalize_batch_if_terminal, record_batch_correction
from .rate_limit import RateLease, acquire_rate_lease, release_rate_lease


RECONCILE_RETRY_SECONDS = 15
RECONCILE_LEASE_SECONDS = 90


@dataclass(frozen=True)
class ReconcileProbe:
    attempt_id: int
    session_ciphertext: str = ""
    credentials: object | None = None
    developer_app_id: int = 0


def drain_account_login_reconciliation(session_factory, limit: int) -> int:
    processed = 0
    for _ in range(max(1, limit)):
        probe = _claim_reconcile_probe(session_factory)
        if not probe:
            break
        processed += 1
        if not probe.session_ciphertext or not probe.credentials:
            continue
        lease = _acquire_reconcile_lease(session_factory, probe)
        if not lease:
            continue
        try:
            health = gateway.check_account_health_isolated(probe.session_ciphertext, probe.credentials)
        except Exception:
            health = None
        finally:
            _release_reconcile_lease(session_factory, lease)
        _apply_reconcile_probe(session_factory, probe.attempt_id, health)
    return processed


def _claim_reconcile_probe(session_factory) -> ReconcileProbe | None:
    with session_factory() as session:
        now = _now()
        attempt = session.scalar(select(TgAccountLoginBatchAttempt).join(
            TgAccountLoginBatchItem,
            TgAccountLoginBatchItem.id == TgAccountLoginBatchAttempt.item_id,
        ).where(
            TgAccountLoginBatchAttempt.reconcile_status.in_(("pending", "probing")),
            TgAccountLoginBatchItem.status.in_(("reconciling", "unresolved")),
            (TgAccountLoginBatchItem.next_retry_at.is_(None) | (TgAccountLoginBatchItem.next_retry_at <= now)),
        ).order_by(TgAccountLoginBatchAttempt.last_reconciled_at.asc().nullsfirst()).with_for_update(skip_locked=True))
        if not attempt:
            return None
        item = session.get(TgAccountLoginBatchItem, attempt.item_id)
        if not item or item.current_attempt_id != attempt.id:
            attempt.reconcile_status = "superseded"
            session.commit()
            return ReconcileProbe(attempt.id)
        if _reconcile_expired(session, item, attempt, now):
            session.commit()
            return ReconcileProbe(attempt.id)
        flow = session.get(TgLoginFlow, attempt.flow_id) if attempt.flow_id else None
        account = session.get(TgAccount, item.account_id) if item.account_id else None
        if not flow or not account or not flow.temporary_session_ciphertext:
            _schedule_reconcile(item, attempt, now)
            session.commit()
            return ReconcileProbe(attempt.id)
        raw_session = decrypt_secret(flow.temporary_session_ciphertext)
        if not raw_session:
            _schedule_reconcile(item, attempt, now)
            session.commit()
            return ReconcileProbe(attempt.id)
        try:
            credentials = credentials_for_account(session, account)
        except ValueError:
            _schedule_reconcile(item, attempt, now)
            session.commit()
            return ReconcileProbe(attempt.id)
        attempt.reconcile_status = "probing"
        attempt.last_reconciled_at = now
        item.next_retry_at = now + timedelta(seconds=RECONCILE_LEASE_SECONDS)
        session.commit()
        return ReconcileProbe(attempt.id, encrypt_session(raw_session), credentials, int(account.developer_app_id or 0))


def _acquire_reconcile_lease(session_factory, probe: ReconcileProbe) -> RateLease | None:
    with session_factory() as session:
        result = acquire_rate_lease(
            session,
            scope_type="developer_app",
            scope_id=str(probe.developer_app_id),
            max_concurrency=get_settings().account_batch_login_developer_app_concurrency,
            min_interval_seconds=0,
        )
    if result.lease:
        return result.lease
    _defer_reconcile_probe(session_factory, probe.attempt_id, result.retry_at)
    return None


def _defer_reconcile_probe(session_factory, attempt_id: int, retry_at) -> None:
    with session_factory() as session:
        attempt = session.get(TgAccountLoginBatchAttempt, attempt_id)
        item = session.get(TgAccountLoginBatchItem, attempt.item_id) if attempt else None
        if not attempt or not item or attempt.reconcile_status != "probing":
            return
        attempt.reconcile_status = "pending"
        item.next_retry_at = retry_at
        attempt.state_version += 1
        session.commit()


def _release_reconcile_lease(session_factory, lease: RateLease) -> None:
    with session_factory() as session:
        release_rate_lease(session, lease)


def _reconcile_expired(session, item: TgAccountLoginBatchItem, attempt: TgAccountLoginBatchAttempt, now) -> bool:
    if attempt.reconcile_until_at and as_beijing_aware(now) >= as_beijing_aware(attempt.reconcile_until_at):
        was_notified = _batch_has_initial_notification(session, item.batch_id)
        item.status = "unresolved"
        item.phase = "unresolved"
        item.failure_type = "manual_review_required"
        item.failure_detail = "自动对账窗口已结束，需要人工核查"
        item.finished_at = item.finished_at or now
        item.next_retry_at = None
        item.state_version += 1
        attempt.reconcile_status = "manual_review_required"
        attempt.state_version += 1
        if was_notified:
            record_batch_correction(
                session,
                item.batch_id,
                changed_item_id=item.id,
                previous_status="unresolved",
            )
        else:
            finalize_batch_if_terminal(session, item.batch_id)
        return True
    if (
        item.status == "reconciling"
        and attempt.deadline_at
        and as_beijing_aware(now) >= as_beijing_aware(attempt.deadline_at)
    ):
        item.status = "unresolved"
        item.phase = "unresolved"
        item.failure_type = "login_remote_unknown"
        item.failure_detail = "单行预算耗尽，远程结果仍未确定"
        item.finished_at = now
        item.state_version += 1
        attempt.reconcile_status = "pending"
        attempt.state_version += 1
        finalize_batch_if_terminal(session, item.batch_id)
    return False


def _apply_reconcile_probe(session_factory, attempt_id: int, health) -> None:
    with session_factory() as session:
        attempt = session.scalar(select(TgAccountLoginBatchAttempt).where(
            TgAccountLoginBatchAttempt.id == attempt_id,
        ).with_for_update())
        if not attempt or attempt.reconcile_status != "probing":
            return
        item = session.get(TgAccountLoginBatchItem, attempt.item_id)
        if not item or item.current_attempt_id != attempt.id:
            attempt.reconcile_status = "superseded"
            session.commit()
            return
        if health and health.status == AccountStatus.ACTIVE.value:
            _apply_authoritative_success(session, item, attempt)
        else:
            attempt.reconcile_status = "pending"
            _schedule_reconcile(item, attempt, _now())
        session.commit()


def _apply_authoritative_success(session, item: TgAccountLoginBatchItem, attempt: TgAccountLoginBatchAttempt) -> None:
    account = session.get(TgAccount, item.account_id)
    flow = session.get(TgLoginFlow, attempt.flow_id)
    if not account or not flow or not flow.temporary_session_ciphertext:
        attempt.reconcile_status = "pending"
        _schedule_reconcile(item, attempt, _now())
        return
    raw_session = decrypt_secret(flow.temporary_session_ciphertext)
    if not raw_session:
        attempt.reconcile_status = "pending"
        _schedule_reconcile(item, attempt, _now())
        return
    account.session_ciphertext = encrypt_session(raw_session)
    account.status = AccountStatus.ACTIVE.value
    account.last_active_at = _now()
    account.health_score = max(account.health_score, 90)
    flow.status = AccountStatus.ACTIVE.value
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    attempt.send_call_state = _confirm_unknown(attempt.send_call_state)
    attempt.code_verify_call_state = _confirm_unknown(attempt.code_verify_call_state)
    attempt.twofa_verify_call_state = _confirm_unknown(attempt.twofa_verify_call_state)
    attempt.reconcile_status = "resolved_authorized"
    attempt.authoritative_evidence_ref = f"flow:{flow.id}:is_user_authorized"
    queue_login_profile_initialization(session, account.id, "account-login-reconciler")
    if item.status == "unresolved":
        _finish_late_success(session, item, attempt, account)
        return
    item.status = "pending"
    item.phase = "pool_transition"
    item.failure_type = ""
    item.failure_detail = ""
    item.next_retry_at = None
    item.state_version += 1
    attempt.phase = "pool_transition"
    attempt.state_version += 1


def _finish_late_success(session, item, attempt, account) -> None:
    batch = session.get(TgAccountLoginBatch, item.batch_id)
    pool = session.get(AccountPool, batch.pool_id) if batch else None
    if not batch or not pool or pool.tenant_id != item.tenant_id:
        item.status = "failed"
        item.phase = "failed"
        item.failure_type = "pool_transition_failed"
        item.failure_detail = "授权已确认，但目标分组迁移失败"
    else:
        try:
            sync_account_usage(session, account, pool, "account-login-reconciler")
            item.status = "succeeded"
            item.phase = "succeeded"
            item.failure_type = ""
            item.failure_detail = ""
            item.code_url_ciphertext = None
        except ValueError:
            item.status = "failed"
            item.phase = "failed"
            item.failure_type = "pool_transition_failed"
            item.failure_detail = "授权已确认，但目标分组迁移失败"
    item.finished_at = _now()
    item.next_retry_at = None
    item.state_version += 1
    attempt.phase = item.phase
    attempt.state_version += 1
    if _batch_has_initial_notification(session, item.batch_id):
        record_batch_correction(
            session,
            item.batch_id,
            changed_item_id=item.id,
            previous_status="unresolved",
        )
    else:
        finalize_batch_if_terminal(session, item.batch_id)


def _schedule_reconcile(item: TgAccountLoginBatchItem, attempt: TgAccountLoginBatchAttempt, now) -> None:
    item.next_retry_at = now + timedelta(seconds=RECONCILE_RETRY_SECONDS)
    attempt.last_reconciled_at = now
    attempt.state_version += 1


def _confirm_unknown(value: str) -> str:
    return "confirmed" if value in {"started", "unknown"} else value


def _batch_has_initial_notification(session, batch_id: int) -> bool:
    from app.models import TgAccountLoginBatchNotification

    return bool(session.scalar(select(TgAccountLoginBatchNotification.id).where(
        TgAccountLoginBatchNotification.batch_id == batch_id,
        TgAccountLoginBatchNotification.event_type == "initial",
    )))


__all__ = ["drain_account_login_reconciliation"]
