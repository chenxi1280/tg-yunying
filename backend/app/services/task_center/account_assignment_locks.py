"""Separate batch eligibility reads from serialization of one real execution."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models import TgAccount, TgAccountAuthorization, TgAccountOnlineState
from .engagement_runtime_error import RuntimeResourceBlocked

ELIGIBILITY_BUSY = "account_eligibility_busy"


@dataclass(frozen=True)
class IdentityLocks:
    accounts: frozenset[int]
    authorizations: frozenset[int]
    online_accounts: frozenset[int]

    def covers(self, row) -> bool:
        return (row.id in self.accounts
            and (row.current_authorization_id is None or row.current_authorization_id in self.authorizations)
            and (row.online_account_id is None or row.online_account_id in self.online_accounts))


def lock_identities(session, tenant_id, ids, *, skip_busy=False):
    options = {"read": True, "skip_locked": skip_busy, "nowait": not skip_busy}
    try:
        with session.begin_nested():
            rows = session.execute(select(TgAccount.id, TgAccount.current_authorization_id).where(
                TgAccount.tenant_id == tenant_id, TgAccount.id.in_(ids),
            ).order_by(TgAccount.id).with_for_update(**options)).all()
            account_ids = frozenset(row.id for row in rows)
            current_ids = {row.current_authorization_id for row in rows if row.current_authorization_id is not None}
            authorizations = frozenset(session.scalars(select(TgAccountAuthorization.id).where(
                TgAccountAuthorization.id.in_(current_ids),
            ).order_by(TgAccountAuthorization.id).with_for_update(**options))) if current_ids else frozenset()
            online_ids = frozenset(session.scalars(select(TgAccountOnlineState.account_id).where(
                TgAccountOnlineState.tenant_id == tenant_id, TgAccountOnlineState.account_id.in_(account_ids),
            ).order_by(TgAccountOnlineState.account_id).with_for_update(**options)))
            return IdentityLocks(account_ids, authorizations, online_ids)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeResourceBlocked(ELIGIBILITY_BUSY, "账号资格更新中，等待当前事务完成") from error


def lock_execution_account(session, tenant_id, account_id):
    try:
        with session.begin_nested():
            return session.scalar(select(TgAccount).where(
                TgAccount.id == account_id, TgAccount.tenant_id == tenant_id,
            ).with_for_update(nowait=True).execution_options(populate_existing=True))
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeResourceBlocked("account_execution_busy", "账号执行事务正在占用，原工作等待后再准入") from error
