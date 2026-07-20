from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.telegram import DeveloperAppCredentials
from app.models import AccountProxy, AccountStatus, TelegramDeveloperApp, TgAccount
from app.models.search_rank_deboost import AccountGroupProxyBinding
from app.services.developer_apps import credentials_for_developer_app
from app.services.proxy_airport_accounts import AVAILABLE_NODE_STATUS, EXECUTABLE_PROXY_PROTOCOLS


@dataclass(frozen=True)
class RankDeboostRuntimeAuthorization:
    session_ciphertext: str
    credentials: DeveloperAppCredentials


def resolve_rank_deboost_runtime_authorization(
    session: Session,
    account: TgAccount,
    payload: Any,
) -> RankDeboostRuntimeAuthorization:
    runtime = _runtime_environment(payload)
    binding = _binding(session, account, payload, runtime)
    proxy = _runtime_proxy(session, account.tenant_id, binding, runtime)
    app = _developer_app(session, account)
    _assert_account_session(account)
    return RankDeboostRuntimeAuthorization(
        session_ciphertext=str(account.session_ciphertext),
        credentials=credentials_for_developer_app(app, proxy),
    )


def _runtime_environment(payload: Any) -> dict[str, Any]:
    runtime = getattr(payload, "runtime_environment", None)
    if not isinstance(runtime, dict):
        raise ValueError("rank_deboost_runtime_environment_missing")
    return runtime


def _binding(
    session: Session,
    account: TgAccount,
    payload: Any,
    runtime: dict[str, Any],
) -> AccountGroupProxyBinding:
    binding_id = _positive_int(runtime.get("group_proxy_binding_id"))
    if binding_id <= 0:
        raise ValueError("rank_deboost_group_proxy_binding_missing")
    binding = session.get(AccountGroupProxyBinding, binding_id)
    if binding is None or binding.tenant_id != account.tenant_id:
        raise ValueError("rank_deboost_group_proxy_binding_not_found")
    _assert_binding_matches_account(binding, account, payload, runtime)
    return binding


def _assert_binding_matches_account(
    binding: AccountGroupProxyBinding,
    account: TgAccount,
    payload: Any,
    runtime: dict[str, Any],
) -> None:
    expected_pool_id = _positive_int(runtime.get("account_pool_id"))
    if expected_pool_id != int(account.pool_id or 0) or expected_pool_id != int(getattr(payload, "account_pool_id", 0)):
        raise ValueError("rank_deboost_account_pool_mismatch")
    if binding.account_pool_id != expected_pool_id or binding.status != "active" or binding.unbound_at is not None:
        raise ValueError("rank_deboost_group_proxy_binding_inactive")
    if _positive_int(runtime.get("binding_generation")) != int(binding.binding_generation or 0):
        raise ValueError("rank_deboost_group_proxy_binding_generation_stale")
    if _positive_int(runtime.get("runtime_proxy_id")) != int(binding.runtime_proxy_id or 0):
        raise ValueError("rank_deboost_runtime_proxy_mismatch")
    if int(getattr(payload, "proxy_airport_node_id", 0)) != int(binding.proxy_airport_node_id):
        raise ValueError("rank_deboost_proxy_airport_node_mismatch")


def _runtime_proxy(
    session: Session,
    tenant_id: int,
    binding: AccountGroupProxyBinding,
    runtime: dict[str, Any],
) -> AccountProxy:
    proxy = session.get(AccountProxy, int(binding.runtime_proxy_id or 0))
    if proxy is None or proxy.tenant_id != tenant_id:
        raise ValueError("rank_deboost_runtime_proxy_missing")
    if _positive_int(runtime.get("runtime_proxy_id")) != proxy.id:
        raise ValueError("rank_deboost_runtime_proxy_mismatch")
    if not _proxy_is_executable(proxy):
        raise ValueError("rank_deboost_runtime_proxy_invalid")
    return proxy


def _proxy_is_executable(proxy: AccountProxy) -> bool:
    return (
        str(proxy.protocol or "").strip().lower() in EXECUTABLE_PROXY_PROTOCOLS
        and bool(str(proxy.host or "").strip())
        and int(proxy.port or 0) > 0
        and proxy.status == AVAILABLE_NODE_STATUS
        and proxy.alert_status == "normal"
    )


def _developer_app(session: Session, account: TgAccount) -> TelegramDeveloperApp:
    app = session.get(TelegramDeveloperApp, int(account.developer_app_id or 0))
    if app is None:
        raise ValueError("rank_deboost_developer_app_missing")
    if app.credentials_version > int(account.developer_app_version or 0):
        account.status = AccountStatus.NEED_RELOGIN.value
        raise ValueError("rank_deboost_developer_app_credentials_stale")
    return app


def _assert_account_session(account: TgAccount) -> None:
    if not account.session_ciphertext:
        raise ValueError("rank_deboost_account_session_missing")


def _positive_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["RankDeboostRuntimeAuthorization", "resolve_rank_deboost_runtime_authorization"]
