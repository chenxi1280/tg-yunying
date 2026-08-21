from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select

from app.models import (
    AuthorizationDrRuntimeContract,
    DeveloperAppHealthStatus,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TelegramEgressAssignment,
    TgAuthorizationDrOperation,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .readiness import ABC_CAPABILITY_VERSION, require_migration_readiness


SLOT_APPS = (("primary_sv", "app_a_id"), ("standby_1_sv", "app_b_id"), ("standby_2_my", "app_c_id"))


def preview_runtime_configuration(session, desired: dict) -> dict:
    normalized = _normalize_desired(desired)
    _validate_claim_scope(session, normalized)
    apps = [_require_app(session, normalized[key]) for _, key in SLOT_APPS]
    if len({app.id for app in apps}) != 3:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", "Three distinct Developer Apps are required")
    payload = {
        "desired": normalized,
        "app_credentials_versions": {str(app.id): app.credentials_version for app in apps},
        "current": _current_configuration(session),
    }
    return {**payload, "configuration_fingerprint": _fingerprint(payload)}


def apply_runtime_configuration(session, desired: dict, *, expected_fingerprint: str, actor: str) -> dict:
    if not actor.strip():
        raise AuthorizationDrError("approval_ref_required", "Configuration actor is required")
    preview = preview_runtime_configuration(session, desired)
    if preview["configuration_fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "DR runtime configuration changed")
    normalized = preview["desired"]
    _apply_assignments(session, normalized, actor)
    _apply_egress(session, normalized)
    _apply_contract(session, normalized, actor)
    session.flush()
    if normalized["mode"] == "migrate":
        require_migration_readiness(session)
    audit(
        session,
        tenant_id=None,
        actor=actor,
        action="配置 MY 授权 DR 运行时",
        target_type="authorization_dr_runtime",
        target_id="1",
        detail=f"mode={normalized['mode']}; egress_id={normalized['egress_id']}",
    )
    session.commit()
    return _current_configuration(session)


def _normalize_desired(desired: dict) -> dict:
    mode = str(desired.get("mode") or "").strip().lower()
    if mode not in {"off", "shadow", "migrate"}:
        raise AuthorizationDrError("runtime_capability_unproven", "DR mode must be off, shadow, or migrate")
    normalized = {
        "mode": mode,
        "app_a_id": int(desired["app_a_id"]),
        "app_b_id": int(desired["app_b_id"]),
        "app_c_id": int(desired["app_c_id"]),
        "egress_id": str(desired["egress_id"]).strip(),
        "egress_secret_ref_digest": str(desired["egress_secret_ref_digest"]).strip(),
        "observed_ip_hmac": str(desired["observed_ip_hmac"]).strip(),
        "required_node_capability_version": str(
            desired.get("required_node_capability_version") or ""
        ).strip(),
        "required_node_runtime_image_sha": str(
            desired.get("required_node_runtime_image_sha") or ""
        ).strip().lower(),
        "claim_scope_operation_id": str(desired.get("claim_scope_operation_id") or "").strip(),
    }
    if not normalized["egress_id"] or any(len(normalized[key]) != 64 for key in ("egress_secret_ref_digest", "observed_ip_hmac")):
        raise AuthorizationDrError("malaysia_egress_unproven", "MY egress evidence is incomplete")
    return normalized


def _validate_claim_scope(session, desired: dict) -> None:
    if desired["mode"] != "migrate":
        return
    if desired["required_node_capability_version"] != ABC_CAPABILITY_VERSION:
        raise AuthorizationDrError("runtime_capability_unproven", "ABC capability contract is required")
    image_sha = desired["required_node_runtime_image_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", image_sha):
        raise AuthorizationDrError("runtime_image_mismatch", "An exact release image SHA is required")
    operation_id = desired["claim_scope_operation_id"]
    operation = session.get(TgAuthorizationDrOperation, operation_id) if operation_id else None
    if not operation or operation.operation_type != "migrate_standby_2":
        raise AuthorizationDrError("claim_scope_invalid", "An exact standby_2 operation is required")
    if operation.status not in {"pending", "waiting_login"}:
        raise AuthorizationDrError("claim_scope_invalid", "Scoped operation is not claimable")


def _require_app(session, app_id: int) -> TelegramDeveloperApp:
    app = session.get(TelegramDeveloperApp, app_id)
    if not app or not app.is_active or app.health_status != DeveloperAppHealthStatus.HEALTHY.value:
        raise AuthorizationDrError("developer_app_slot_assignment_conflict", f"Developer App {app_id} is unavailable")
    return app


def _apply_assignments(session, desired: dict, actor: str) -> None:
    for purpose, key in SLOT_APPS:
        app = _require_app(session, desired[key])
        row = session.get(DeveloperAppSlotAssignment, purpose)
        if not row:
            row = DeveloperAppSlotAssignment(slot_purpose=purpose, assigned_by=actor, credentials_version=app.credentials_version)
            session.add(row)
        elif row.developer_app_id != app.id or row.credentials_version != app.credentials_version:
            row.assignment_version += 1
        row.developer_app_id = app.id
        row.credentials_version = app.credentials_version
        row.status = "active"
        row.assigned_by = actor
        row.assigned_at = _now()


def _apply_egress(session, desired: dict) -> None:
    row = session.get(TelegramEgressAssignment, desired["egress_id"])
    if not row:
        row = session.scalar(select(TelegramEgressAssignment).where(
            TelegramEgressAssignment.purpose == "standby_my",
        ))
    if not row:
        row = TelegramEgressAssignment(id=desired["egress_id"], purpose="standby_my", region_code="my")
        session.add(row)
    elif row.id != desired["egress_id"] or row.observed_ip_hmac != desired["observed_ip_hmac"]:
        row.version += 1
        row.id = desired["egress_id"]
    row.secret_ref_digest = desired["egress_secret_ref_digest"]
    row.observed_ip_hmac = desired["observed_ip_hmac"]
    row.status = "active"
    row.connectivity_status = "verified"
    row.last_verified_at = _now()


def _apply_contract(session, desired: dict, actor: str) -> None:
    row = session.get(AuthorizationDrRuntimeContract, 1)
    if not row:
        row = AuthorizationDrRuntimeContract(id=1, contract_epoch=1, version=1)
        session.add(row)
    if row.mode != desired["mode"]:
        row.contract_epoch += 1
    row.mode = desired["mode"]
    row.cluster_incarnation = "my-node-1"
    row.mutation_hold_reason = ""
    row.required_node_capability_version = desired["required_node_capability_version"]
    row.required_node_runtime_image_sha = desired["required_node_runtime_image_sha"]
    row.claim_scope_operation_id = (
        desired["claim_scope_operation_id"] if desired["mode"] == "migrate" else ""
    )
    row.version = (row.version or 0) + 1
    row.updated_by = actor
    row.updated_at = _now()


def _current_configuration(session) -> dict:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    assignments = list(session.scalars(select(DeveloperAppSlotAssignment).order_by(DeveloperAppSlotAssignment.slot_purpose)))
    egress = list(session.scalars(select(TelegramEgressAssignment).order_by(TelegramEgressAssignment.id)))
    return {
        "mode": contract.mode if contract else "missing",
        "contract_epoch": contract.contract_epoch if contract else 0,
        "required_node_capability_version": (
            contract.required_node_capability_version if contract else ""
        ),
        "required_node_runtime_image_sha": contract.required_node_runtime_image_sha if contract else "",
        "claim_scope_operation_id": contract.claim_scope_operation_id if contract else "",
        "assignments": [
            {"purpose": row.slot_purpose, "app_id": row.developer_app_id, "version": row.assignment_version, "status": row.status}
            for row in assignments
        ],
        "egress": [
            {"id": row.id, "status": row.status, "connectivity": row.connectivity_status, "version": row.version}
            for row in egress
        ],
    }


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


__all__ = ["apply_runtime_configuration", "preview_runtime_configuration"]
