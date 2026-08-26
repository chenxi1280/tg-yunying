from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.config import get_settings
from app.models import (
    TgAccountFullInitialization,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
)
from app.services._common import _now
from app.services.code_source_client import CodeSourceClient

from .abc import execute_abc_stage
from .contracts import FullInitializationClaim
from .parent import sync_parent_bindings
from .profile import execute_profile_stage
from .reconcile import execute_reconcile_stage
from .two_fa import execute_two_fa_stage


LEASE_SECONDS = 90
CLAIMABLE_STATUSES = {"pending", "waiting_profile", "waiting_abc", "running"}
RECONCILE_ONLY_WAITING_STATUSES = {"waiting_profile", "waiting_abc"}
PARENT_BARRIER_DONE_STATUSES = {
    "post_initialization_waiting",
    "succeeded",
    "succeeded_with_warning",
    "failed",
    "unresolved",
    "skipped",
}


def drain_account_post_login_initializations(
    session_factory,
    limit: int,
    *,
    code_client: CodeSourceClient | None = None,
) -> int:
    mode = get_settings().account_post_login_init_mode
    _purge_expired_source_secrets(session_factory)
    _reconcile_stale_two_fa_calls(session_factory)
    if mode == "off":
        return 0
    _release_parent_barriers(session_factory)
    processed = 0
    client = code_client or CodeSourceClient()
    capacity = min(max(1, limit), get_settings().account_post_login_init_worker_concurrency)
    for _ in range(capacity):
        claim = _claim_next(session_factory, reconcile_only=mode == "reconcile_only")
        if not claim:
            break
        _execute_claim(session_factory, claim, client)
        _sync_claim_parent(session_factory, claim.initialization_id)
        processed += 1
    return processed


def _purge_expired_source_secrets(session_factory) -> None:
    with session_factory() as session:
        owners = session.scalars(
            select(TgAccountFullInitialization).where(
                TgAccountFullInitialization.source_two_fa_password_ciphertext != "",
                TgAccountFullInitialization.source_secret_expires_at.is_not(None),
                TgAccountFullInitialization.source_secret_expires_at <= _now(),
            ).with_for_update(skip_locked=True)
        )
        for owner in owners:
            if _secret_is_required_for_remote_closure(owner):
                continue
            owner.source_two_fa_password_ciphertext = ""
            owner.source_secret_expires_at = None
            owner.version += 1
        session.commit()


def _secret_is_required_for_remote_closure(owner) -> bool:
    if owner.status == "reconcile_unknown":
        return True
    if owner.two_fa_call_state in {"started", "unknown"}:
        return True
    return bool(
        owner.status == "manual_required"
        and "email" in owner.failure_type
    )


def _reconcile_stale_two_fa_calls(session_factory) -> None:
    with session_factory() as session:
        now = _now()
        owners = session.scalars(
            select(TgAccountFullInitialization).where(
                TgAccountFullInitialization.stage == "two_fa",
                TgAccountFullInitialization.two_fa_call_state == "started",
                TgAccountFullInitialization.lease_expires_at.is_not(None),
                TgAccountFullInitialization.lease_expires_at <= now,
            ).with_for_update(skip_locked=True)
        )
        for owner in owners:
            _mark_stale_two_fa_unknown(owner)
            sync_parent_bindings(session, owner)
        session.commit()


def _release_parent_barriers(session_factory) -> None:
    with session_factory() as session:
        owners = list(
            session.scalars(
                select(TgAccountFullInitialization).where(
                    TgAccountFullInitialization.status == "waiting_login_parent"
                ).with_for_update(skip_locked=True)
            )
        )
        for owner in owners:
            statuses = _bound_parent_statuses(session, owner.id)
            if not statuses or any(status not in PARENT_BARRIER_DONE_STATUSES for status in statuses):
                continue
            owner.status = "pending"
            owner.next_retry_at = None
            owner.version += 1
        session.commit()


def _bound_parent_statuses(session, owner_id: int) -> list[str]:
    return list(
        session.scalars(
            select(TgAccountLoginBatchItem.status)
            .join(
                TgAccountLoginPostInitializationBinding,
                TgAccountLoginPostInitializationBinding.login_item_id
                == TgAccountLoginBatchItem.id,
            )
            .where(
                TgAccountLoginPostInitializationBinding.full_initialization_id == owner_id
            )
        )
    )


def _claim_next(session_factory, *, reconcile_only: bool) -> FullInitializationClaim | None:
    with session_factory() as session:
        now = _now()
        query = select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.status.in_(CLAIMABLE_STATUSES),
            or_(
                TgAccountFullInitialization.next_retry_at.is_(None),
                TgAccountFullInitialization.next_retry_at <= now,
            ),
            or_(
                TgAccountFullInitialization.lease_expires_at.is_(None),
                TgAccountFullInitialization.lease_expires_at <= now,
            ),
        )
        if reconcile_only:
            query = query.where(
                or_(
                    TgAccountFullInitialization.stage == "reconcile",
                    TgAccountFullInitialization.status.in_(RECONCILE_ONLY_WAITING_STATUSES),
                )
            )
        owner = session.scalar(
            query.order_by(TgAccountFullInitialization.created_at, TgAccountFullInitialization.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not owner:
            return None
        token = uuid4().hex
        owner.status = "running"
        owner.lease_token = token
        owner.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        owner.started_at = owner.started_at or now
        owner.version += 1
        claim = FullInitializationClaim(owner.id, owner.stage, token)
        session.commit()
        return claim


def _execute_claim(session_factory, claim, client) -> None:
    try:
        if claim.stage == "two_fa":
            execute_two_fa_stage(session_factory, claim, code_client=client)
            return
        if claim.stage == "profile":
            execute_profile_stage(session_factory, claim)
            return
        if claim.stage == "abc":
            execute_abc_stage(session_factory, claim)
            return
        if claim.stage == "reconcile":
            execute_reconcile_stage(session_factory, claim)
            return
        raise RuntimeError(f"unsupported post-login initialization stage: {claim.stage}")
    except Exception as exc:
        _mark_unexpected_failure(session_factory, claim, exc)


def _mark_unexpected_failure(session_factory, claim, exc) -> None:
    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        if not owner or owner.lease_token != claim.lease_token:
            return
        owner.status = "failed"
        owner.stage = "failed"
        owner.failure_type = "post_init_worker_error"
        owner.failure_detail = type(exc).__name__
        owner.finished_at = _now()
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.version += 1
        session.commit()


def _mark_stale_two_fa_unknown(owner) -> None:
    owner.status = "reconcile_unknown"
    owner.stage = "reconcile_unknown"
    owner.two_fa_status = "reconcile_unknown"
    owner.two_fa_call_state = "unknown"
    owner.failure_type = "two_fa_remote_unknown"
    owner.failure_detail = "2FA mutation lease expired after remote call started"
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _sync_claim_parent(session_factory, initialization_id: int) -> None:
    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, initialization_id)
        if not owner:
            return
        sync_parent_bindings(session, owner)
        session.commit()


__all__ = ["drain_account_post_login_initializations"]
