from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.models import (
    TERMINAL_FULL_INIT_STATUSES,
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountLoginBatch,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
)
from app.security import encrypt_secret
from app.services._common import _now, audit

from .canonical import ensure_canonical_primary
from .policy import FULL_INIT_POLICY, PROFILE_POLICY_VERSION


def create_or_attach_full_initialization(
    session: Session,
    item: TgAccountLoginBatchItem,
    *,
    actor: str,
    source_two_fa_kind: str = "unknown",
    source_two_fa_password: str = "",
) -> TgAccountFullInitialization:
    account = _locked_account(session, item)
    ensure_canonical_primary(session, account, actor)
    fixed_version = _fixed_version(session, item.tenant_id)
    target_pool_id = _target_pool_id(session, item)
    originating_actor = _originating_actor(session, item, actor)
    bound_owner = _bound_owner(session, item)
    owner = bound_owner
    if owner and not _owner_compatible(owner, account, fixed_version, target_pool_id):
        raise ValueError("login item full initialization binding drifted")
    owner = owner or _active_owner(session, account.id)
    if owner and not _owner_compatible(
        owner, account, fixed_version, target_pool_id,
    ):
        _stop_drifted_owner(owner)
        owner = None
    owner = owner or _compatible_debt_owner(
        session,
        account,
        fixed_version=fixed_version,
        target_pool_id=target_pool_id,
    )
    predecessor = _latest_succeeded_owner(session, account.id)
    owner = owner or _new_owner(
        session,
        account,
        target_pool_id=target_pool_id,
        actor=originating_actor,
        fixed_version=fixed_version,
        predecessor=predecessor,
    )
    if bound_owner is None:
        _resume_safe_readback(owner)
    _capture_source_secret(owner, source_two_fa_kind, source_two_fa_password)
    _attach_binding(session, item, owner)
    _project_item(item, owner)
    _audit_binding(session, item, owner, actor=actor)
    return owner


def _audit_binding(
    session: Session,
    item: TgAccountLoginBatchItem,
    owner: TgAccountFullInitialization,
    *,
    actor: str,
) -> None:
    audit(
        session,
        tenant_id=item.tenant_id,
        actor=actor,
        action="批量登录建立完整初始化义务",
        target_type="tg_account_full_initialization",
        target_id=str(owner.id),
        detail=f"item_id={item.id}; login_generation={item.execution_generation}; status={owner.status}",
    )


def mark_login_authorized_waiting(
    session: Session,
    item: TgAccountLoginBatchItem,
    attempt: TgAccountLoginBatchAttempt,
    *,
    owner: TgAccountFullInitialization,
) -> None:
    if owner.status == "waiting_login_parent":
        owner.status = "pending"
        owner.next_retry_at = None
        owner.version += 1
    item.authorization_status = "confirmed"
    item.post_initialization_status = owner.status
    item.status = "post_initialization_waiting"
    item.phase = "post_initialization_waiting"
    item.failure_type = ""
    item.failure_detail = ""
    item.next_retry_at = None
    item.state_version += 1
    attempt.phase = "post_initialization_waiting"
    attempt.lease_token = ""
    attempt.lease_expires_at = None
    attempt.state_version += 1


def _locked_account(session: Session, item: TgAccountLoginBatchItem) -> TgAccount:
    account = session.scalar(
        select(TgAccount).where(TgAccount.id == item.account_id).with_for_update()
    )
    if not account or account.tenant_id != item.tenant_id or account.deleted_at is not None:
        raise ValueError("post-login initialization account is unavailable")
    if account.account_identity != "normal":
        raise ValueError("post-login initialization requires a normal account")
    return account


def _active_owner(session: Session, account_id: int) -> TgAccountFullInitialization | None:
    return session.scalar(
        select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.account_id == account_id,
            TgAccountFullInitialization.status.not_in(TERMINAL_FULL_INIT_STATUSES),
        ).with_for_update()
    )


def _bound_owner(
    session: Session,
    item: TgAccountLoginBatchItem,
) -> TgAccountFullInitialization | None:
    binding = session.scalar(
        select(TgAccountLoginPostInitializationBinding).where(
            TgAccountLoginPostInitializationBinding.login_item_id == item.id,
            TgAccountLoginPostInitializationBinding.login_execution_generation
            == item.execution_generation,
        ).with_for_update()
    )
    return session.get(TgAccountFullInitialization, binding.full_initialization_id) if binding else None


def _reusable_debt_owner(
    session: Session,
    account_id: int,
) -> TgAccountFullInitialization | None:
    latest = session.scalar(
        select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.account_id == account_id,
        ).order_by(TgAccountFullInitialization.generation.desc()).limit(1)
    )
    if latest and latest.status in {"failed", "manual_required", "reconcile_unknown"}:
        return latest
    return None


def _compatible_debt_owner(
    session: Session,
    account: TgAccount,
    *,
    fixed_version: int,
    target_pool_id: int,
) -> TgAccountFullInitialization | None:
    owner = _reusable_debt_owner(session, account.id)
    if not owner:
        return None
    if not _owner_compatible(owner, account, fixed_version, target_pool_id):
        return None
    return owner


def _latest_succeeded_owner(session: Session, account_id: int):
    return session.scalar(
        select(TgAccountFullInitialization).where(
            TgAccountFullInitialization.account_id == account_id,
            TgAccountFullInitialization.status == "succeeded",
        ).order_by(TgAccountFullInitialization.generation.desc()).limit(1)
    )


def _fixed_version(session: Session, tenant_id: int) -> int:
    tenant = session.get(Tenant, tenant_id)
    return int(tenant.fixed_two_fa_password_version if tenant else 0)


def _originating_actor(
    session: Session,
    item: TgAccountLoginBatchItem,
    fallback: str,
) -> str:
    batch = session.get(TgAccountLoginBatch, item.batch_id)
    return batch.created_by if batch and batch.created_by.strip() else fallback


def _owner_compatible(
    owner: TgAccountFullInitialization,
    account: TgAccount,
    fixed_version: int,
    target_pool_id: int,
) -> bool:
    return bool(
        owner.policy_version == FULL_INIT_POLICY
        and owner.authorization_generation == account.authorization_generation
        and owner.fixed_two_fa_version == fixed_version
        and owner.target_pool_id == target_pool_id
        and owner.profile_policy_version == PROFILE_POLICY_VERSION
    )


def _target_pool_id(session: Session, item: TgAccountLoginBatchItem) -> int:
    batch = session.get(TgAccountLoginBatch, item.batch_id)
    if not batch or batch.tenant_id != item.tenant_id:
        raise ValueError("post-login initialization target pool is unavailable")
    return batch.pool_id


def _stop_drifted_owner(owner: TgAccountFullInitialization) -> None:
    owner.status = "failed"
    owner.stage = "failed"
    owner.failure_type = "post_init_policy_drift"
    owner.failure_detail = "A generation or fixed 2FA policy changed before initialization completed"
    owner.finished_at = _now()
    owner.version += 1


def _resume_safe_readback(owner: TgAccountFullInitialization) -> None:
    if owner.status != "reconcile_unknown":
        return
    owner.status = "pending"
    owner.stage = "reconcile"
    owner.finished_at = None
    owner.next_retry_at = _now()
    owner.version += 1


def _new_owner(
    session: Session,
    account: TgAccount,
    *,
    target_pool_id: int,
    actor: str,
    fixed_version: int,
    predecessor: TgAccountFullInitialization | None,
) -> TgAccountFullInitialization:
    maximum = session.scalar(
        select(func.max(TgAccountFullInitialization.generation)).where(
            TgAccountFullInitialization.account_id == account.id
        )
    )
    owner = TgAccountFullInitialization(
        tenant_id=account.tenant_id,
        account_id=account.id,
        generation=int(maximum or 0) + 1,
        authorization_generation=account.authorization_generation,
        fixed_two_fa_version=fixed_version,
        predecessor_initialization_id=predecessor.id if predecessor else None,
        target_pool_id=target_pool_id,
        profile_policy_version=PROFILE_POLICY_VERSION,
        status="waiting_login_parent",
        originating_actor=actor,
    )
    _copy_profile_target(owner, predecessor, target_pool_id)
    if fixed_version < 1:
        owner.status = "manual_required"
        owner.stage = "manual_required"
        owner.failure_type = "tenant_fixed_two_fa_not_configured"
        owner.failure_detail = "租户固定 2FA 配置在授权后发生漂移"
        owner.finished_at = _now()
    session.add(owner)
    session.flush()
    return owner


def _copy_profile_target(
    owner: TgAccountFullInitialization,
    predecessor: TgAccountFullInitialization | None,
    target_pool_id: int,
) -> None:
    if not predecessor:
        return
    if predecessor.target_pool_id != target_pool_id:
        return
    if predecessor.profile_policy_version != PROFILE_POLICY_VERSION:
        return
    owner.profile_target_name = predecessor.profile_target_name
    owner.profile_target_avatar_source = predecessor.profile_target_avatar_source
    owner.profile_target_avatar_object_key = predecessor.profile_target_avatar_object_key


def _capture_source_secret(
    owner: TgAccountFullInitialization,
    source_kind: str,
    password: str,
) -> None:
    if not password or owner.two_fa_status != "pending":
        if source_kind == "telegram_missing" and owner.source_two_fa_kind == "unknown":
            owner.source_two_fa_kind = source_kind
        return
    if source_kind != "telegram_accepted":
        return
    owner.source_two_fa_kind = source_kind
    owner.source_two_fa_password_ciphertext = encrypt_secret(password)
    ttl = get_settings().account_post_login_init_secret_ttl_seconds
    owner.source_secret_expires_at = _now() + timedelta(seconds=ttl)
    owner.version += 1


def _attach_binding(
    session: Session,
    item: TgAccountLoginBatchItem,
    owner: TgAccountFullInitialization,
) -> None:
    existing = session.scalar(
        select(TgAccountLoginPostInitializationBinding).where(
            TgAccountLoginPostInitializationBinding.login_item_id == item.id,
            TgAccountLoginPostInitializationBinding.login_execution_generation
            == item.execution_generation,
        )
    )
    if existing:
        if existing.full_initialization_id != owner.id:
            raise ValueError("login item is already bound to another full initialization")
        return
    session.add(
        TgAccountLoginPostInitializationBinding(
            tenant_id=item.tenant_id,
            account_id=owner.account_id,
            login_item_id=item.id,
            login_execution_generation=item.execution_generation,
            full_initialization_id=owner.id,
        )
    )


def _project_item(
    item: TgAccountLoginBatchItem,
    owner: TgAccountFullInitialization,
) -> None:
    item.initialization_policy = FULL_INIT_POLICY
    item.post_initialization_id = owner.id
    item.post_initialization_status = owner.status
    session = object_session(item)
    batch = session.get(TgAccountLoginBatch, item.batch_id) if session else None
    if batch:
        batch.initialization_policy = FULL_INIT_POLICY


__all__ = ["create_or_attach_full_initialization", "mark_login_authorized_waiting"]
