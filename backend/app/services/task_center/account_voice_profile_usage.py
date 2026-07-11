from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPool, AccountStatus, TgAccount
from app.services.account_usage_policy import apply_operational_account_filters, assert_account_action_allowed


VOICE_PROFILE_MUTATION_ACTION = "account_mask_init"


def assert_voice_profile_account_ids_allowed(session: Session, tenant_id: int, account_ids: list[int]) -> None:
    for account_id in list(dict.fromkeys(int(account_id) for account_id in account_ids)):
        account = session.get(TgAccount, account_id)
        if account and account.tenant_id == tenant_id and account.deleted_at is None:
            assert_voice_profile_mutation_allowed(session, account)


def assert_voice_profile_mutation_allowed(session: Session, account: TgAccount) -> None:
    pool = session.get(AccountPool, account.pool_id) if account.pool_id is not None else None
    assert_account_action_allowed(account, pool, VOICE_PROFILE_MUTATION_ACTION)


def voice_profile_allowed_ids(
    session: Session,
    tenant_id: int,
    candidate_ids: list[int],
) -> tuple[list[int], dict[int, str]]:
    allowed: list[int] = []
    skipped: dict[int, str] = {}
    for account_id in candidate_ids:
        error = _voice_profile_usage_error(session, tenant_id, account_id)
        if error:
            skipped[account_id] = error
            continue
        allowed.append(account_id)
    return allowed, skipped


def batch_candidate_account_ids(
    session: Session,
    tenant_id: int,
    account_ids: list[int],
    missing_only: bool,
) -> list[int]:
    unique_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not unique_ids and missing_only:
        stmt = select(TgAccount.id).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        return list(session.scalars(apply_operational_account_filters(stmt).order_by(TgAccount.id.asc())))
    for account_id in unique_ids:
        _require_account(session, tenant_id, account_id)
    return unique_ids


def _voice_profile_usage_error(session: Session, tenant_id: int, account_id: int) -> str:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        return ""
    try:
        assert_voice_profile_mutation_allowed(session, account)
        return ""
    except ValueError as exc:
        return str(exc)


def _require_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.scalar(
        select(TgAccount).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.id == account_id,
            TgAccount.deleted_at.is_(None),
        )
    )
    if not account:
        raise ValueError(f"account not found: {account_id}")
    return account


__all__ = [
    "assert_voice_profile_account_ids_allowed",
    "assert_voice_profile_mutation_allowed",
    "batch_candidate_account_ids",
    "voice_profile_allowed_ids",
]
