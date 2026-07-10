from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.orm import Session

from app.models import AccountPool, TgAccount


AccountUsage = Literal["normal", "code_receiver", "rank_deboost", "mismatch"]
VALID_ACCOUNT_USAGES = frozenset({"normal", "code_receiver", "rank_deboost"})
DEDICATED_ACCOUNT_USAGES = frozenset({"code_receiver", "rank_deboost"})
AUTHORIZATION_ASSET_ACTIONS = frozenset(
    {
        "login",
        "relogin",
        "authorization_diagnostics",
        "standby_session_repair",
        "readonly_device_diagnostics",
        "account_health_probe",
        "official_verification_code_read",
    }
)
OPERATIONAL_ACTIONS = frozenset({"operational_task", "message_send", "listener", "target_admission"})
PROFILE_ACTIONS = frozenset({"profile_update", "profile_init", "account_mask_init"})
SECURITY_MUTATION_ACTIONS = frozenset({"two_fa_set", "two_fa_rotate", "device_cleanup"})
RANK_DEBOOST_ACTION = "search_rank_deboost"
SUPPORTED_ACTIONS = (
    AUTHORIZATION_ASSET_ACTIONS
    | OPERATIONAL_ACTIONS
    | PROFILE_ACTIONS
    | SECURITY_MUTATION_ACTIONS
    | {RANK_DEBOOST_ACTION}
)


@dataclass(frozen=True)
class AccountUsageSyncSummary:
    account_id: int
    tenant_id: int
    previous_pool_id: int | None
    target_pool_id: int
    previous_usage: AccountUsage
    usage: AccountUsage
    actor: str


def account_usage(account: TgAccount, pool: AccountPool | None) -> AccountUsage:
    if pool is None or account.pool_id != pool.id:
        return "mismatch"
    if account.tenant_id != pool.tenant_id:
        return "mismatch"
    purpose = str(pool.pool_purpose or "")
    if purpose not in VALID_ACCOUNT_USAGES or not _pool_markers_consistent(purpose, pool.system_key):
        return "mismatch"
    if account.account_identity != purpose:
        return "mismatch"
    return purpose  # type: ignore[return-value]


def _pool_markers_consistent(purpose: str, system_key: str) -> bool:
    key = str(system_key or "")
    if key in DEDICATED_ACCOUNT_USAGES:
        return key == purpose
    if purpose in DEDICATED_ACCOUNT_USAGES:
        return not key
    return True


def assert_account_action_allowed(account: TgAccount, pool: AccountPool | None, action_kind: str) -> AccountUsage:
    usage = account_usage(account, pool)
    if usage == "mismatch":
        raise ValueError("account_purpose_mismatch")
    if action_kind not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported_account_action_kind:{action_kind}")
    if _action_allowed(usage, action_kind):
        return usage
    raise ValueError(f"account_action_not_allowed:{usage}:{action_kind}")


def _action_allowed(usage: AccountUsage, action_kind: str) -> bool:
    if action_kind in AUTHORIZATION_ASSET_ACTIONS:
        return True
    if action_kind == RANK_DEBOOST_ACTION:
        return usage == "rank_deboost"
    return usage == "normal"


def apply_operational_account_filters(stmt: Select) -> Select:
    return stmt.where(
        TgAccount.account_identity == "normal",
        _matching_enabled_pool_exists("normal"),
    )


def apply_rank_deboost_account_filters(stmt: Select) -> Select:
    return stmt.where(
        TgAccount.account_identity == "rank_deboost",
        _matching_enabled_pool_exists("rank_deboost"),
    )


def apply_consistent_enabled_account_filters(stmt: Select) -> Select:
    return stmt.where(
        or_(*(_consistent_usage_condition(purpose) for purpose in sorted(VALID_ACCOUNT_USAGES))),
    )


def _consistent_usage_condition(purpose: str):
    return and_(TgAccount.account_identity == purpose, _matching_enabled_pool_exists(purpose))


def _matching_enabled_pool_exists(purpose: str):
    conditions = [
        AccountPool.id == TgAccount.pool_id,
        AccountPool.tenant_id == TgAccount.tenant_id,
        AccountPool.pool_purpose == purpose,
        AccountPool.is_enabled.is_(True),
    ]
    conditions.append(_pool_marker_filter(purpose))
    return exists(select(AccountPool.id).where(*conditions))


def _pool_marker_filter(purpose: str):
    if purpose in DEDICATED_ACCOUNT_USAGES:
        return AccountPool.system_key.in_(("", purpose))
    return AccountPool.system_key.not_in(tuple(DEDICATED_ACCOUNT_USAGES))


def sync_account_usage(
    session: Session,
    account: TgAccount,
    target_pool: AccountPool,
    actor: str,
) -> AccountUsageSyncSummary:
    locked_account = _lock_account(session, account.id)
    locked_pool = _lock_pool(session, target_pool.id)
    _validate_usage_target(locked_account, locked_pool)
    previous_pool = _current_pool(session, locked_account)
    summary = AccountUsageSyncSummary(
        account_id=locked_account.id,
        tenant_id=locked_account.tenant_id,
        previous_pool_id=locked_account.pool_id,
        target_pool_id=locked_pool.id,
        previous_usage=account_usage(locked_account, previous_pool),
        usage=locked_pool.pool_purpose,
        actor=actor,
    )
    locked_account.pool_id = locked_pool.id
    locked_account.account_identity = locked_pool.pool_purpose
    session.flush()
    return summary


def _lock_account(session: Session, account_id: int) -> TgAccount:
    account = session.scalar(select(TgAccount).where(TgAccount.id == account_id).with_for_update())
    if account is None:
        raise ValueError("account not found")
    return account


def _lock_pool(session: Session, pool_id: int) -> AccountPool:
    pool = session.scalar(select(AccountPool).where(AccountPool.id == pool_id).with_for_update())
    if pool is None:
        raise ValueError("account pool not found")
    return pool


def _validate_usage_target(account: TgAccount, pool: AccountPool) -> None:
    if account.tenant_id != pool.tenant_id:
        raise ValueError("account pool tenant mismatch")
    if not pool.is_enabled:
        raise ValueError("account pool disabled")
    if pool.pool_purpose not in VALID_ACCOUNT_USAGES:
        raise ValueError("invalid account pool purpose")
    if not _pool_markers_consistent(pool.pool_purpose, pool.system_key):
        raise ValueError("account_purpose_mismatch")


def _current_pool(session: Session, account: TgAccount) -> AccountPool | None:
    if account.pool_id is None:
        return None
    return session.get(AccountPool, account.pool_id)


__all__ = [
    "AccountUsageSyncSummary",
    "account_usage",
    "apply_operational_account_filters",
    "apply_consistent_enabled_account_filters",
    "apply_rank_deboost_account_filters",
    "assert_account_action_allowed",
    "sync_account_usage",
]
