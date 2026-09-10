"""Owner-scoped direct egress observation and per-request authorization evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Callable

import requests

from app.search_transport import (
    DIRECT_FENCE_FIELDS, DIRECT_PROOF_SOURCE, DIRECT_METADATA_POLICY, DirectEgressPolicy, is_direct_search,
    require_direct_declaration,
)


@dataclass(frozen=True)
class DirectSearchContext:
    owner_mode: str
    owner_instance_id: str
    policy: DirectEgressPolicy


class DirectEgressObserver:
    def __init__(self, probe: Callable[[], str]):
        self._probe = probe
        self._lock = threading.Lock()
        self._observations: dict[tuple[str, str, str], dict] = {}

    def observe(self, context: DirectSearchContext) -> dict:
        key = (context.owner_instance_id, context.policy.region, context.policy.ip)
        with self._lock:
            if key in self._observations:
                return dict(self._observations[key])
            observed = self._probe()
            if observed != context.policy.ip:
                raise ValueError("direct_search_egress_mismatch")
            result = {
                "source": DIRECT_PROOF_SOURCE, "evidence_scope": "owner_instance",
                "owner_instance_id": context.owner_instance_id,
                "region": context.policy.region, "observed_ip": observed,
                "observed_at": datetime.now(timezone.utc).isoformat(), "verified": True,
            }
            self._observations[key] = result
            return dict(result)


def validate_direct_search_context(payload, credentials, context: DirectSearchContext) -> dict:
    runtime = payload.get("runtime_environment") or {}
    require_direct_declaration(runtime, context.policy)
    if context.owner_mode != "server" or not context.owner_instance_id:
        raise ValueError("direct_search_owner_required")
    if payload.get("client_metadata") or payload.get("proxy_airport_node_id") is not None:
        raise ValueError("direct_search_legacy_environment_forbidden")
    _validate_direct_credentials(runtime, credentials)
    return runtime


def _validate_direct_credentials(runtime, credentials) -> None:
    proxy_fields = ("proxy_id", "proxy_protocol", "proxy_host", "proxy_port",
                    "proxy_username", "proxy_password")
    if any(getattr(credentials, name) for name in proxy_fields):
        raise ValueError("telegram_account_proxy_forbidden")
    fields = {
        "tenant_id": credentials.tenant_id, "account_id": credentials.account_id,
        "authorization_id": credentials.authorization_id,
        "authorization_fact_version": credentials.authorization_fact_version,
        "authorization_generation": credentials.authorization_generation,
        "connection_generation": credentials.connection_generation,
        "developer_app_id": credentials.app_id, "developer_app_api_id": credentials.api_id,
        "developer_app_version": credentials.credentials_version,
    }
    if any(value is None or runtime.get(name) != value for name, value in fields.items()):
        raise ValueError("direct_search_credential_fence_mismatch")


def attach_direct_search_evidence(result: dict, runtime: dict, observation: dict) -> dict:
    proof = {**observation, **{name: runtime[name] for name in DIRECT_FENCE_FIELDS}}
    return {**result, "transport_contract": dict(runtime), "transport_evidence": proof}


def direct_search_rejected(error: Exception) -> dict:
    code = str(error) if isinstance(error, ValueError) else "direct_search_egress_probe_failed"
    return {"success": False, "error_code": code, "detail": str(error),
            "error_type": type(error).__name__, "remote_mutation_started": False,
            "search_transport_phase": "direct_transport_pre_gateway"}


class DirectSearchExecution:
    def __init__(self, settings, *, probe, owner_identity):
        self._mode = settings.telegram_owner_mode
        self._policy = DirectEgressPolicy(settings.telegram_direct_egress_region,
                                          settings.telegram_direct_egress_ip)
        self._owner_identity = owner_identity
        self._observer = DirectEgressObserver(probe)

    def run(self, payload, credentials, operation):
        context = DirectSearchContext(self._mode, self._owner_identity(), self._policy)
        try:
            runtime = validate_direct_search_context(payload, credentials, context)
            observation = self._observer.observe(context)
        except (ValueError, requests.RequestException) as exc:
            return direct_search_rejected(exc)
        return attach_direct_search_evidence(operation(), runtime, observation)


def probe_direct_egress(settings) -> str:
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(settings.rank_deboost_egress_probe_url,
                               timeout=settings.telethon_client_connect_timeout_seconds)
        response.raise_for_status()
        return response.text.strip()


def owner_client_metadata(payload: dict) -> dict[str, str]:
    runtime = payload.get("runtime_environment") or {}
    if not is_direct_search(runtime) or runtime.get("transport_mode") != "direct":
        raise ValueError("direct_search_transport_contract_invalid")
    if runtime.get("client_metadata_policy") != DIRECT_METADATA_POLICY:
        raise ValueError("direct_search_metadata_policy_invalid")
    if payload.get("client_metadata") not in (None, {}):
        raise ValueError("direct_search_legacy_environment_forbidden")
    return {}
