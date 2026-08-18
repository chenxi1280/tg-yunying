from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPool,
    AccountStatus,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountLoginBatchItem,
)
from app.services._common import _now


SUCCESS_LOGIN_ITEM_STATUSES = frozenset({"succeeded", "succeeded_with_warning"})
TERMINAL_LOGIN_BATCH_STATUSES = frozenset({"completed", "completed_with_unresolved", "cancelled"})
DISCOVERY_DAYS = 7
DISCOVERY_BATCH_LIMIT = 20


@dataclass(frozen=True)
class LoginBatchInitializationSpec:
    tenant_id: int
    login_batch_ids: tuple[int, ...]
    expected_target_count: int
    style_group_ids: tuple[int, ...]
    seed: str
    deployed_sha: str


@dataclass(frozen=True)
class LoginBatchTargets:
    batches: tuple[TgAccountLoginBatch, ...]
    items: tuple[TgAccountLoginBatchItem, ...]
    accounts: tuple[TgAccount, ...]


@dataclass(frozen=True)
class LoginBatchCandidate:
    batch: TgAccountLoginBatch
    items: tuple[TgAccountLoginBatchItem, ...]
    account_ids: frozenset[int]
    success_count: int


def load_login_batch_targets(
    session: Session,
    spec: LoginBatchInitializationSpec,
) -> LoginBatchTargets:
    candidates = resolve_login_batches(session, spec)
    items = _latest_success_items(candidates)
    account_ids = [int(item.account_id) for item in items if item.account_id is not None]
    if len(account_ids) != spec.expected_target_count or len(set(account_ids)) != len(account_ids):
        raise RuntimeError("login_batch_target_count_or_identity_mismatch")
    accounts_by_id = _accounts_by_id(session, spec.tenant_id, account_ids)
    accounts = tuple(accounts_by_id[account_id] for account_id in account_ids)
    _validate_target_accounts(session, accounts)
    return LoginBatchTargets(
        batches=tuple(candidate.batch for candidate in candidates),
        items=items,
        accounts=accounts,
    )


def resolve_login_batches(
    session: Session,
    spec: LoginBatchInitializationSpec,
) -> tuple[LoginBatchCandidate, ...]:
    candidates = _explicit_candidates(session, spec) if spec.login_batch_ids else _recent_candidates(session, spec)
    if spec.login_batch_ids:
        _validate_candidate_set(candidates, spec.expected_target_count)
        return candidates
    actual_count = len(_combined_account_ids(candidates))
    if not candidates or actual_count != spec.expected_target_count:
        detail = "|".join(_candidate_summary(candidate) for candidate in candidates)
        raise RuntimeError(
            "login_batch_set_discovery_target_mismatch: "
            f"expected={spec.expected_target_count};actual={actual_count};candidates={detail}"
        )
    return candidates


def _explicit_candidates(
    session: Session,
    spec: LoginBatchInitializationSpec,
) -> tuple[LoginBatchCandidate, ...]:
    if len(set(spec.login_batch_ids)) != len(spec.login_batch_ids):
        raise RuntimeError("login_batch_ids_must_be_unique")
    batches = [session.get(TgAccountLoginBatch, batch_id) for batch_id in spec.login_batch_ids]
    if any(batch is None or batch.tenant_id != spec.tenant_id for batch in batches):
        raise RuntimeError("login_batch_not_found_for_tenant")
    return tuple(_candidate(session, batch) for batch in batches if batch is not None)


def _recent_candidates(
    session: Session,
    spec: LoginBatchInitializationSpec,
) -> tuple[LoginBatchCandidate, ...]:
    cutoff = _now() - timedelta(days=DISCOVERY_DAYS)
    batches = list(session.scalars(
        select(TgAccountLoginBatch).where(
            TgAccountLoginBatch.tenant_id == spec.tenant_id,
            TgAccountLoginBatch.status.in_(TERMINAL_LOGIN_BATCH_STATUSES),
            TgAccountLoginBatch.success_count > 0,
            TgAccountLoginBatch.finished_at >= cutoff,
        ).order_by(TgAccountLoginBatch.finished_at.desc(), TgAccountLoginBatch.id.desc()).limit(DISCOVERY_BATCH_LIMIT)
    ))
    return tuple(_candidate(session, batch) for batch in batches)


def _candidate(session: Session, batch: TgAccountLoginBatch) -> LoginBatchCandidate:
    if batch.status not in TERMINAL_LOGIN_BATCH_STATUSES:
        raise RuntimeError(f"login_batch_not_terminal: batch_id={batch.id};status={batch.status}")
    success_items = tuple(session.scalars(
        select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
            TgAccountLoginBatchItem.status.in_(SUCCESS_LOGIN_ITEM_STATUSES),
        ).order_by(TgAccountLoginBatchItem.line_no.asc())
    ))
    success_account_ids = [int(item.account_id) for item in success_items if item.account_id is not None]
    if len(success_account_ids) != len(success_items) or len(set(success_account_ids)) != len(success_account_ids):
        raise RuntimeError(f"login_batch_success_binding_invalid: batch_id={batch.id}")
    if len(success_items) != int(batch.success_count):
        raise RuntimeError(f"login_batch_success_count_drift: batch_id={batch.id}")
    account_ids = frozenset(success_account_ids)
    return LoginBatchCandidate(
        batch=batch,
        items=success_items,
        account_ids=account_ids,
        success_count=len(success_items),
    )


def _latest_success_items(
    candidates: tuple[LoginBatchCandidate, ...],
) -> tuple[TgAccountLoginBatchItem, ...]:
    by_account_id: dict[int, TgAccountLoginBatchItem] = {}
    for candidate in candidates:
        for item in candidate.items:
            account_id = int(item.account_id)
            current = by_account_id.get(account_id)
            if current is None or int(item.id) > int(current.id):
                by_account_id[account_id] = item
    return tuple(by_account_id[account_id] for account_id in sorted(by_account_id))


def _combined_account_ids(candidates: tuple[LoginBatchCandidate, ...]) -> frozenset[int]:
    return frozenset(account_id for candidate in candidates for account_id in candidate.account_ids)


def _validate_candidate_set(candidates: tuple[LoginBatchCandidate, ...], expected_count: int) -> None:
    combined = _combined_account_ids(candidates)
    if len(combined) != expected_count:
        raise RuntimeError(
            f"login_batch_set_target_count_mismatch: expected={expected_count};actual={len(combined)}"
        )


def _candidate_summary(candidate: LoginBatchCandidate) -> str:
    batch = candidate.batch
    return (
        f"{batch.id}:{batch.status}:total={batch.total_count}:success={candidate.success_count}:"
        f"failed={batch.failed_count}:unresolved={batch.unresolved_count}"
    )


def _accounts_by_id(session: Session, tenant_id: int, account_ids: list[int]) -> dict[int, TgAccount]:
    accounts = list(session.scalars(select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.id.in_(account_ids),
    )))
    result = {int(account.id): account for account in accounts}
    if set(result) != set(account_ids):
        raise RuntimeError("login_batch_account_binding_missing")
    return result


def _validate_target_accounts(session: Session, accounts: tuple[TgAccount, ...]) -> None:
    for account in accounts:
        pool = session.get(AccountPool, account.pool_id) if account.pool_id else None
        valid = (
            account.deleted_at is None
            and account.status == AccountStatus.ACTIVE.value
            and bool(account.session_ciphertext)
            and account.account_identity == "normal"
            and pool is not None
            and pool.pool_purpose == "normal"
        )
        if not valid:
            raise RuntimeError(f"login_batch_account_not_operational_normal: account_id={account.id}")


__all__ = [
    "LoginBatchInitializationSpec",
    "LoginBatchTargets",
    "load_login_batch_targets",
    "resolve_login_batches",
]
