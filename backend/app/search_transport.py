"""Versioned search transport facts, separate from click/rank fulfillment facts."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any
from uuid import UUID

DIRECT_SEARCH_TRANSPORT_VERSION = "sv_current_direct_v1"
LEGACY_SEARCH_TRANSPORT_VERSION = "legacy_proxy_v1"
DIRECT_SEARCH_REGION = "sv"
DIRECT_METADATA_POLICY = "owner_managed"
DIRECT_EGRESS_ID = "sv:direct"
DIRECT_PROOF_SOURCE = "owner_direct_https"
DIRECT_FENCE_FIELDS = (
    "tenant_id", "account_id", "authorization_id", "authorization_fact_version", "authorization_generation",
    "connection_generation", "developer_app_id", "developer_app_api_id",
    "developer_app_version", "authorization_role",
)


@dataclass(frozen=True)
class DirectEgressPolicy:
    region: str
    ip: str

    def validate(self) -> None:
        if self.region != DIRECT_SEARCH_REGION:
            raise ValueError("direct_search_region_unavailable")
        address = ipaddress.ip_address(self.ip)
        if not address.is_global:
            raise ValueError("direct_search_public_egress_required")


def is_direct_search(runtime: object) -> bool:
    return bool(isinstance(runtime, dict) and runtime.get("transport_contract_version")
                == DIRECT_SEARCH_TRANSPORT_VERSION)


def require_direct_declaration(runtime: dict[str, Any], policy: DirectEgressPolicy) -> None:
    if not is_direct_search(runtime) or runtime.get("transport_mode") != "direct":
        raise ValueError("direct_search_transport_contract_invalid")
    policy.validate()
    if runtime.get("client_metadata_policy") != DIRECT_METADATA_POLICY:
        raise ValueError("direct_search_metadata_policy_invalid")
    if runtime.get("direct_egress_region") != policy.region or runtime.get("direct_egress_ip") != policy.ip:
        raise ValueError("direct_search_egress_contract_stale")
    proxy_fields = ("proxy_id", "runtime_proxy_id", "proxy_binding_id",
                    "group_proxy_binding_id", "proxy_airport_node_id", "proxy_egress_guard")
    if any(runtime.get(field) is not None for field in proxy_fields):
        raise ValueError("direct_search_proxy_fields_forbidden")


def direct_proof_matches(runtime: dict[str, Any], proof: object) -> bool:
    if not isinstance(proof, dict) or proof.get("verified") is not True:
        return False
    if proof.get("source") != DIRECT_PROOF_SOURCE or not proof.get("observed_at"):
        return False
    try:
        UUID(str(proof.get("owner_instance_id") or ""))
        policy = DirectEgressPolicy(str(proof.get("region") or ""), str(proof.get("observed_ip") or ""))
        require_direct_declaration(runtime, policy)
    except (ValueError, TypeError):
        return False
    return all(proof.get(field) == runtime.get(field) for field in DIRECT_FENCE_FIELDS)


def direct_result_proven(result: dict[str, Any]) -> bool:
    runtime = result.get("transport_contract")
    return isinstance(runtime, dict) and is_direct_search(runtime) and direct_proof_matches(
        runtime, result.get("transport_evidence"),
    )
