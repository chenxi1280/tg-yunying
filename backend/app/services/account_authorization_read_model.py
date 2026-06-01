from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountAuthorization

from .account_authorization_constants import (
    ACTIVE_STATUSES,
    EXPLICIT_PRIMARY_SOURCE,
    LEGACY_PRIMARY_SOURCE,
    NO_STANDBY_HINT,
    PRIMARY_ROLE,
    STANDBY_ROLES,
)


def authorization_summary_for_account(session: Session, account: TgAccount) -> dict[str, Any]:
    rows = _authorization_rows(session, account)
    if rows:
        return _summary_with_legacy_primary(account, rows)
    return _legacy_summary(account)


def authorization_summaries_for_accounts(session: Session, accounts: list[TgAccount]) -> dict[int, dict[str, Any]]:
    if not accounts:
        return {}
    rows_by_account = _rows_by_account(session, [account.id for account in accounts])
    return {account.id: _summary_for_rows_or_legacy(rows_by_account.get(account.id), account) for account in accounts}


def list_account_authorizations(session: Session, account_id: int) -> list[dict[str, Any]]:
    account = _require_account(session, account_id)
    rows = _authorization_rows(session, account)
    if not rows and account.session_ciphertext:
        return [_legacy_authorization_snapshot(account)]
    return [_authorization_snapshot(row) for row in rows]


def _authorization_rows(session: Session, account: TgAccount) -> list[TgAccountAuthorization]:
    return list(
        session.scalars(
            select(TgAccountAuthorization)
            .where(TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.disabled_at.is_(None))
            .order_by(TgAccountAuthorization.is_current.desc(), TgAccountAuthorization.id.asc())
        )
    )


def _rows_by_account(session: Session, account_ids: list[int]) -> dict[int, list[TgAccountAuthorization]]:
    rows = list(
        session.scalars(
            select(TgAccountAuthorization)
            .where(TgAccountAuthorization.account_id.in_(account_ids), TgAccountAuthorization.disabled_at.is_(None))
            .order_by(TgAccountAuthorization.account_id.asc(), TgAccountAuthorization.is_current.desc())
        )
    )
    result: dict[int, list[TgAccountAuthorization]] = {account_id: [] for account_id in account_ids}
    for row in rows:
        result.setdefault(row.account_id, []).append(row)
    return result


def _summary_for_rows_or_legacy(rows: list[TgAccountAuthorization] | None, account: TgAccount) -> dict[str, Any]:
    return _summary_with_legacy_primary(account, rows) if rows else _legacy_summary(account)


def _summary_with_legacy_primary(account: TgAccount, rows: list[TgAccountAuthorization]) -> dict[str, Any]:
    if _has_explicit_primary(rows) or not account.session_ciphertext:
        return _explicit_summary(rows)
    standby_count = sum(1 for row in rows if _is_healthy_standby(row))
    return _summary(
        primary_status="active",
        primary_source=LEGACY_PRIMARY_SOURCE,
        standby_count=standby_count,
        is_blocking=False,
        risk_hint="" if standby_count else NO_STANDBY_HINT,
    )


def _explicit_summary(rows: list[TgAccountAuthorization]) -> dict[str, Any]:
    primary = _primary_row(rows)
    standby_count = sum(1 for row in rows if _is_healthy_standby(row))
    primary_status = primary.status if primary else "missing"
    is_blocking = primary_status not in {"active", "standby"} and standby_count == 0
    return _summary(
        primary_status=primary_status,
        primary_source=EXPLICIT_PRIMARY_SOURCE,
        standby_count=standby_count,
        is_blocking=is_blocking,
        risk_hint="" if standby_count else NO_STANDBY_HINT,
    )


def _legacy_summary(account: TgAccount) -> dict[str, Any]:
    has_session = bool(account.session_ciphertext)
    return _summary(
        primary_status="active" if has_session else "missing",
        primary_source=LEGACY_PRIMARY_SOURCE,
        standby_count=0,
        is_blocking=not has_session,
        risk_hint=NO_STANDBY_HINT if has_session else "账号没有可用主授权 session",
    )


def _summary(
    *,
    primary_status: str,
    primary_source: str,
    standby_count: int,
    is_blocking: bool,
    risk_hint: str,
) -> dict[str, Any]:
    return {
        "primary_status": primary_status,
        "primary_source": primary_source,
        "standby_count": standby_count,
        "target_standby_count": 2,
        "has_standby": standby_count > 0,
        "is_blocking": is_blocking,
        "risk_hint": risk_hint,
    }


def _primary_row(rows: list[TgAccountAuthorization]) -> TgAccountAuthorization | None:
    for row in rows:
        if row.is_current or row.role == PRIMARY_ROLE:
            return row
    return rows[0] if rows else None


def _is_healthy_standby(row: TgAccountAuthorization) -> bool:
    return row.role in STANDBY_ROLES and row.status in ACTIVE_STATUSES and bool(row.session_ciphertext)


def _has_explicit_primary(rows: list[TgAccountAuthorization]) -> bool:
    return any(row.is_current or row.role == PRIMARY_ROLE for row in rows)


def _require_account(session: Session, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def _legacy_authorization_snapshot(account: TgAccount) -> dict[str, Any]:
    return {
        "id": None,
        "account_id": account.id,
        "role": PRIMARY_ROLE,
        "developer_app_id": account.developer_app_id,
        "proxy_id": account.proxy_id,
        "status": "active",
        "health_status": "legacy",
        "is_current": True,
        "session_available": True,
        "primary_source": LEGACY_PRIMARY_SOURCE,
        "failure_reason": "",
        "last_health_check_at": None,
        "last_success_at": None,
        "last_switched_at": None,
        "disabled_at": None,
    }


def _authorization_snapshot(row: TgAccountAuthorization) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "role": row.role,
        "developer_app_id": row.developer_app_id,
        "proxy_id": row.proxy_id,
        "status": row.status,
        "health_status": row.health_status,
        "is_current": row.is_current,
        "session_available": bool(row.session_ciphertext),
        "primary_source": EXPLICIT_PRIMARY_SOURCE,
        "failure_reason": row.failure_reason,
        "last_health_check_at": row.last_health_check_at,
        "last_success_at": row.last_success_at,
        "last_switched_at": row.last_switched_at,
        "disabled_at": row.disabled_at,
    }
