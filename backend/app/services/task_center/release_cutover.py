"""Audited ordinary-release reuse of a completed dispatch takeover contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re

from sqlalchemy import select

from app.models import AiContentScopeTakeoverBatch, AuditLog, DispatchClaimScope
from .ai_content_scope_takeover_apply import takeover_chain_is_complete
from .dispatch_claim_ledger import for_update
from .dispatch_runtime_contract import build_dispatch_runtime_contract, require_active_scope_contract
from .dispatch_runtime_control import stage_dispatch_runtime_contract

PREPARED = "worker_release_prepared"
VERIFIED = "worker_release_activation_verified"
ACTIVATED = "共享调度合同activate"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReleaseIdentity:
    sha: str
    source_fingerprint: str
    actor: str
    approval_ref: str


def require_identity(identity: ReleaseIdentity) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", identity.sha):
        raise ValueError("release_sha_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", identity.source_fingerprint):
        raise ValueError("release_fingerprint_invalid")
    if not identity.actor.strip() or not identity.approval_ref.strip():
        raise ValueError("release_actor_and_approval_required")


def latest_audit(session, scope_id: str, action: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.target_type == "dispatch_claim_scope",
        AuditLog.target_id == scope_id, AuditLog.action == action,
    ).order_by(AuditLog.id.desc()).limit(1))


def audit_detail(audit: AuditLog) -> dict:
    value = json.loads(audit.detail)
    if not isinstance(value, dict):
        raise ValueError("release_audit_detail_invalid")
    return value


def prepare_cutover(session, settings, identity: ReleaseIdentity) -> dict:
    require_identity(identity)
    contract = build_dispatch_runtime_contract(settings)
    scope = session.scalar(for_update(session, select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == contract.dispatcher_scope,
    )))
    decision = _reuse_decision(session, scope, identity, contract=contract)
    scope = stage_dispatch_runtime_contract(session, settings)
    detail = {
        **asdict(identity), **decision, "schema_version": SCHEMA_VERSION,
        "scope_id": scope.id,
        "capacity_config_fingerprint": contract.capacity_config_fingerprint,
        "contract_version": contract.rebuild_contract_version,
    }
    audit = write_release_audit(session, PREPARED, detail)
    session.flush()
    return {**detail, "plan_id": audit.id}


def _reuse_decision(session, scope, identity, *, contract) -> dict:
    base = {"mode": "upgrade", "takeover_head_batch_id": "", "source_evidence_id": None}
    if scope is None:
        return {**base, "reason": "initial_scope"}
    previous = latest_audit(session, scope.id, VERIFIED)
    if previous is None:
        return {**base, "reason": "initial_release_evidence"}
    evidence = audit_detail(previous)
    activation = latest_audit(session, scope.id, ACTIVATED)
    if activation is None or activation.id != evidence["activation_audit_id"]:
        return {**base, "reason": "activation_changed"}
    if evidence["source_fingerprint"] != identity.source_fingerprint:
        return {**base, "reason": "contract_source_changed"}
    if evidence["capacity_config_fingerprint"] != contract.capacity_config_fingerprint:
        return {**base, "reason": "runtime_contract_changed"}
    if scope.contract_activation_state != "active":
        return {**base, "reason": "previous_activation_incomplete"}
    require_active_scope_contract(scope, contract)
    _require_completed_batch(session, evidence["takeover_head_batch_id"], contract)
    return {
        "mode": "ordinary", "reason": "verified_contract_unchanged",
        "takeover_head_batch_id": evidence["takeover_head_batch_id"],
        "source_evidence_id": previous.id,
    }


def load_cutover_plan(session, plan_id: int, identity: ReleaseIdentity) -> dict:
    require_identity(identity)
    audit = session.get(AuditLog, plan_id)
    if audit is None or audit.action != PREPARED:
        raise ValueError("release_plan_missing")
    detail = audit_detail(audit)
    for key, value in asdict(identity).items():
        if detail.get(key) != value:
            raise ValueError(f"release_plan_identity_changed:{key}")
    latest = latest_audit(session, detail["scope_id"], PREPARED)
    if latest is None or latest.id != plan_id:
        raise ValueError("release_plan_superseded")
    verified = latest_audit(session, detail["scope_id"], VERIFIED)
    if verified is not None and audit_detail(verified)["plan_id"] == plan_id:
        raise ValueError("release_plan_already_activated")
    return {**detail, "plan_id": plan_id}


def verify_reused_batch(session, settings, plan: dict) -> None:
    contract = build_dispatch_runtime_contract(settings)
    if plan["capacity_config_fingerprint"] != contract.capacity_config_fingerprint:
        raise ValueError("release_plan_runtime_contract_changed")
    _require_completed_batch(session, plan["takeover_head_batch_id"], contract)


def _require_completed_batch(session, batch_id: str, contract) -> None:
    batch = session.get(AiContentScopeTakeoverBatch, batch_id)
    if batch is None or batch.dispatcher_scope != contract.dispatcher_scope:
        raise ValueError("release_takeover_scope_mismatch")
    if batch.config_version != contract.rebuild_contract_version:
        raise ValueError("release_takeover_contract_mismatch")
    if not takeover_chain_is_complete(session, batch_id):
        raise ValueError("release_takeover_chain_incomplete")


def record_verified_cutover(session, plan: dict, batch_id: str) -> dict:
    activation = latest_audit(session, plan["scope_id"], ACTIVATED)
    if activation is None:
        raise ValueError("release_activation_evidence_missing")
    detail = audit_detail(activation)
    if (detail["takeover_head_batch_id"] != batch_id
            or detail["approval_ref"] != plan["approval_ref"]):
        raise ValueError("release_activation_evidence_mismatch")
    verified = {
        **plan, "takeover_head_batch_id": batch_id,
        "activation_audit_id": activation.id,
    }
    write_release_audit(session, VERIFIED, verified)
    return verified


def write_release_audit(session, action: str, detail: dict) -> AuditLog:
    audit = AuditLog(
        tenant_id=None, actor=detail["actor"][:100], action=action,
        target_type="dispatch_claim_scope", target_id=detail["scope_id"],
        detail=json.dumps(detail, sort_keys=True),
    )
    session.add(audit)
    return audit
