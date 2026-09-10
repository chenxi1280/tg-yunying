"""Canonical current-authorization search context; no proxy binding or standby selection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountAuthorization
from app.search_transport import (
    DIRECT_EGRESS_ID, DIRECT_FENCE_FIELDS, DIRECT_METADATA_POLICY,
    DIRECT_SEARCH_TRANSPORT_VERSION, DirectEgressPolicy, require_direct_declaration,
)
from app.services.account_runtime_transport import (
    AccountRuntimeTransport, task_account_runtime_transport,
)


@dataclass(frozen=True)
class DirectSearchEnvironment:
    authorization_id: int
    session_role: str
    developer_app_id: int
    developer_app_api_id: int
    client_metadata: dict[str, str]
    runtime_environment: dict[str, Any]


def egress_policy(settings) -> DirectEgressPolicy:
    return DirectEgressPolicy(settings.telegram_direct_egress_region, settings.telegram_direct_egress_ip)


def build_direct_search_environment(
    session: Session, account: TgAccount, *, settings,
) -> DirectSearchEnvironment:
    policy = egress_policy(settings)
    policy.validate()
    transport = task_account_runtime_transport(session, account)
    if transport.authorization_id is None:
        raise ValueError("direct_search_current_authorization_required")
    authorization = session.get(TgAccountAuthorization, transport.authorization_id)
    runtime = _runtime_snapshot(account, authorization, transport)
    runtime.update(transport_contract_version=DIRECT_SEARCH_TRANSPORT_VERSION,
                   transport_mode="direct", client_metadata_policy=DIRECT_METADATA_POLICY,
                   direct_egress_region=policy.region, direct_egress_ip=policy.ip,
                   direct_egress_id=DIRECT_EGRESS_ID)
    return DirectSearchEnvironment(
        authorization.id, authorization.role, transport.credentials.app_id,
        transport.credentials.api_id, {}, runtime,
    )


def resolve_direct_search_runtime(
    session: Session, account: TgAccount, runtime: dict[str, Any], *, settings,
) -> AccountRuntimeTransport:
    require_direct_declaration(runtime, egress_policy(settings))
    transport = task_account_runtime_transport(session, account)
    if transport.authorization_id is None:
        raise ValueError("direct_search_current_authorization_required")
    authorization = session.get(TgAccountAuthorization, transport.authorization_id)
    current = _runtime_snapshot(account, authorization, transport)
    if any(runtime.get(field) != current[field] for field in DIRECT_FENCE_FIELDS):
        raise ValueError("direct_search_authorization_generation_stale")
    if runtime.get("account_id") != account.id:
        raise ValueError("direct_search_account_scope_mismatch")
    return transport


def _runtime_snapshot(account, authorization, transport) -> dict[str, Any]:
    credentials = transport.credentials
    return {
        "tenant_id": account.tenant_id,
        "account_id": account.id,
        "authorization_id": authorization.id,
        "authorization_role": authorization.role,
        "authorization_fact_version": int(authorization.fact_version),
        "authorization_generation": int(account.authorization_generation),
        "connection_generation": int(account.connection_generation),
        "developer_app_id": credentials.app_id,
        "developer_app_api_id": credentials.api_id,
        "developer_app_version": credentials.credentials_version,
    }
