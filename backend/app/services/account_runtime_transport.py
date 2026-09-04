from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountAuthorization

from .developer_apps import credentials_for_account, credentials_for_authorization


@dataclass(frozen=True)
class AccountRuntimeTransport:
    account_id: int
    session_ciphertext: str
    credentials: object
    authorization_id: int | None
    dependency_snapshot: dict


def task_account_runtime_transport(
    session: Session,
    account: TgAccount,
    task_type: str | None = None,
) -> AccountRuntimeTransport:
    del task_type
    authorization = _current_authorization(session, account)
    if account.current_authorization_id and authorization is None:
        raise ValueError("current_account_authorization_unavailable")
    if authorization is not None:
        credentials = credentials_for_authorization(
            session,
            authorization,
            use_proxy=True,
        )
        return AccountRuntimeTransport(
            account_id=account.id,
            session_ciphertext=str(authorization.session_ciphertext),
            credentials=credentials,
            authorization_id=authorization.id,
            dependency_snapshot=_dependency_snapshot(account, authorization),
        )
    if not account.session_ciphertext:
        raise ValueError("account_session_unavailable")
    credentials = credentials_for_account(session, account, use_proxy=True)
    return AccountRuntimeTransport(
        account_id=account.id,
        session_ciphertext=str(account.session_ciphertext),
        credentials=credentials,
        authorization_id=None,
        dependency_snapshot=_dependency_snapshot(account, None),
    )


def _current_authorization(
    session: Session,
    account: TgAccount,
) -> TgAccountAuthorization | None:
    if not account.current_authorization_id:
        return None
    authorization = session.get(
        TgAccountAuthorization,
        account.current_authorization_id,
    )
    if (
        authorization is None
        or authorization.tenant_id != account.tenant_id
        or authorization.account_id != account.id
        or not authorization.is_current
        or authorization.status != "active"
        or not authorization.session_ciphertext
    ):
        return None
    return authorization


def _dependency_snapshot(
    account: TgAccount,
    authorization: TgAccountAuthorization | None,
) -> dict:
    return {
        "account_id": account.id,
        "authorization_generation": int(account.authorization_generation or 0),
        "authorization_fact_generation": int(
            account.authorization_fact_generation or 0
        ),
        "connection_generation": int(account.connection_generation or 0),
        "current_authorization_id": account.current_authorization_id,
        "authorization_id": authorization.id if authorization else None,
        "authorization_fact_version": int(authorization.fact_version or 0)
        if authorization
        else 0,
        "proxy_id": authorization.proxy_id if authorization else account.proxy_id,
    }


__all__ = ["AccountRuntimeTransport", "task_account_runtime_transport"]
