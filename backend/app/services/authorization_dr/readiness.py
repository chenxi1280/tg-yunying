from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    DeveloperAppSlotAssignment,
    TelegramEgressAssignment,
)
from app.services._common import _now

from .contracts import AuthorizationDrError


REQUIRED_SLOT_PURPOSES = ("primary_sv", "standby_1_sv", "standby_2_my")
MY_NODE_STALE_SECONDS = 120


@dataclass(frozen=True)
class MigrationReadiness:
    contract_epoch: int
    node: AuthorizationDrExecutionNode
    egress: TelegramEgressAssignment
    standby_assignment: DeveloperAppSlotAssignment
    assignment_version: int


def require_migration_readiness(session) -> MigrationReadiness:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.mode != "migrate":
        raise AuthorizationDrError("runtime_capability_unproven", "DR runtime is not in migrate mode")
    if contract.mutation_hold_reason:
        raise AuthorizationDrError(contract.mutation_hold_reason, "Authorization mutation is on hold")
    assignments = _require_slot_assignments(session)
    node = _require_my_node(session)
    egress = _require_my_egress(session, node.standby_egress_id)
    assignment = assignments["standby_2_my"]
    return MigrationReadiness(contract.contract_epoch, node, egress, assignment, assignment.assignment_version)


def record_node_heartbeat(
    session,
    node_id: str,
    *,
    region_code: str,
    purpose: str,
    capability_version: str,
    standby_egress_id: str,
    active_client_count: int,
    node_version: int,
) -> AuthorizationDrExecutionNode:
    existing_ids = set(session.scalars(select(AuthorizationDrExecutionNode.id).where(
        AuthorizationDrExecutionNode.region_code == "my",
        AuthorizationDrExecutionNode.id != node_id,
    )))
    if existing_ids:
        raise AuthorizationDrError("execution_node_mismatch", "A different MY execution node is registered")
    node = session.get(AuthorizationDrExecutionNode, node_id)
    if node and node.version != node_version:
        raise AuthorizationDrError("authorization_version_conflict", "MY node version changed")
    if not node:
        node = AuthorizationDrExecutionNode(
            id=node_id,
            region_code=region_code,
            purpose=purpose,
            capability_version=capability_version,
            standby_egress_id=standby_egress_id,
            version=node_version,
        )
        session.add(node)
    node.region_code = region_code
    node.purpose = purpose
    node.capability_version = capability_version
    node.standby_egress_id = standby_egress_id
    node.active_client_count = active_client_count
    node.status = "ready" if active_client_count == 0 else "busy"
    node.last_heartbeat_at = _now()
    session.commit()
    return node


def _require_slot_assignments(session) -> dict[str, DeveloperAppSlotAssignment]:
    rows = list(session.scalars(select(DeveloperAppSlotAssignment).where(
        DeveloperAppSlotAssignment.status == "active",
        DeveloperAppSlotAssignment.slot_purpose.in_(REQUIRED_SLOT_PURPOSES),
    )))
    mapping = {row.slot_purpose: row for row in rows}
    if set(mapping) != set(REQUIRED_SLOT_PURPOSES):
        raise AuthorizationDrError(
            "developer_app_slot_assignment_incomplete",
            "Exactly three active Developer App slot assignments are required",
        )
    if len({row.developer_app_id for row in rows}) != len(REQUIRED_SLOT_PURPOSES):
        raise AuthorizationDrError(
            "developer_app_slot_assignment_conflict",
            "Developer App slot assignments must use three distinct apps",
        )
    return mapping


def _require_my_node(session) -> AuthorizationDrExecutionNode:
    rows = list(session.scalars(select(AuthorizationDrExecutionNode).where(
        AuthorizationDrExecutionNode.region_code == "my",
        AuthorizationDrExecutionNode.purpose == "standby_session_dr",
        AuthorizationDrExecutionNode.status == "ready",
    )))
    if len(rows) != 1:
        raise AuthorizationDrError("malaysia_wake_unavailable", "Exactly one ready MY execution node is required")
    node = rows[0]
    cutoff = _now() - timedelta(seconds=MY_NODE_STALE_SECONDS)
    if not node.last_heartbeat_at or node.last_heartbeat_at <= cutoff:
        raise AuthorizationDrError("malaysia_wake_unavailable", "MY execution node heartbeat is stale")
    if node.active_client_count != 0:
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "MY node has an active Telegram client")
    return node


def _require_my_egress(session, egress_id: str) -> TelegramEgressAssignment:
    egress = session.get(TelegramEgressAssignment, egress_id)
    if not egress or egress.purpose != "standby_my" or egress.region_code != "my":
        raise AuthorizationDrError("malaysia_egress_unproven", "MY standby egress assignment is missing")
    if egress.status != "active" or egress.connectivity_status != "verified":
        raise AuthorizationDrError("malaysia_egress_unproven", "MY standby egress is not verified")
    if not egress.secret_ref_digest or not egress.observed_ip_hmac or not egress.last_verified_at:
        raise AuthorizationDrError("malaysia_egress_unproven", "MY standby egress evidence is incomplete")
    return egress


__all__ = ["MigrationReadiness", "record_node_heartbeat", "require_migration_readiness"]
