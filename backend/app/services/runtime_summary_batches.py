from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountRuntimeSummary, TgAccount

from .runtime_summary import refresh_account_summary

DEFAULT_ACCOUNT_SUMMARY_BATCH_SIZE = 20
MAX_ACCOUNT_SUMMARY_BATCH_SIZE = 100


def refresh_account_runtime_summary_batch(session: Session, *, limit: int) -> int:
    batch_size = min(MAX_ACCOUNT_SUMMARY_BATCH_SIZE, max(1, int(limit)))
    rows = _missing_account_rows(session, batch_size)
    if not rows:
        rows = _oldest_account_rows(session, batch_size)
    for tenant_id, account_id in rows:
        refresh_account_summary(session, int(tenant_id), int(account_id))
    return len(rows)


def _missing_account_rows(session: Session, limit: int) -> list[tuple[int, int]]:
    return list(
        session.execute(
            select(TgAccount.tenant_id, TgAccount.id)
            .outerjoin(
                AccountRuntimeSummary,
                (AccountRuntimeSummary.tenant_id == TgAccount.tenant_id)
                & (AccountRuntimeSummary.account_id == TgAccount.id),
            )
            .where(TgAccount.deleted_at.is_(None), AccountRuntimeSummary.id.is_(None))
            .order_by(TgAccount.tenant_id, TgAccount.id)
            .limit(limit)
        )
    )


def _oldest_account_rows(session: Session, limit: int) -> list[tuple[int, int]]:
    return list(
        session.execute(
            select(TgAccount.tenant_id, TgAccount.id)
            .join(
                AccountRuntimeSummary,
                (AccountRuntimeSummary.tenant_id == TgAccount.tenant_id)
                & (AccountRuntimeSummary.account_id == TgAccount.id),
            )
            .where(TgAccount.deleted_at.is_(None))
            .order_by(AccountRuntimeSummary.updated_at, TgAccount.tenant_id, TgAccount.id)
            .limit(limit)
        )
    )


__all__ = [
    "DEFAULT_ACCOUNT_SUMMARY_BATCH_SIZE",
    "MAX_ACCOUNT_SUMMARY_BATCH_SIZE",
    "refresh_account_runtime_summary_batch",
]
