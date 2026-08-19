from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    AdultSubjectAttestation,
    AiContentPolicyVersion,
    Task,
    TaskAiContentPolicyBinding,
)
from app.services._common import _now


GENERAL_ROUTE = "general"
ADULT_ROUTE_SUBJECTS = {
    "adult_visual": "adult_visual",
    "adult_product": "adult_product",
    "adult_service_inquiry": "adult_service",
    "adult_service_sensory": "adult_service",
}
ADULT_SUBJECT_EVIDENCE = {
    "adult_visual": frozenset({"adult_visual_content_verified"}),
    "adult_product": frozenset({"adult_product_catalog_verified"}),
    "adult_service": frozenset({
        "adult_service_subject_verified",
        "adult_service_listing_verified",
    }),
}
VALID_ROUTES = frozenset({GENERAL_ROUTE, *ADULT_ROUTE_SUBJECTS})
VALID_POLICY_STATUSES = frozenset({"draft", "approved", "active", "retired"})


class AiContentPolicyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class PolicyDraft:
    tenant_id: int
    version: int
    route_rules: dict
    prompt_registry: dict
    gate_config: dict
    example_set: dict


@dataclass(frozen=True)
class AttestationSpec:
    tenant_id: int
    scope_type: str
    scope_id: str
    subject_class: str
    evidence_codes: tuple[str, ...]
    actor_user_id: int
    permission_snapshot: dict
    expires_at: datetime
    task_config_revision: int
    policy_version: int


@dataclass(frozen=True)
class TaskBindingSpec:
    task_id: str
    policy_version_id: str
    allowed_routes: tuple[str, ...]
    attestation_ids: tuple[str, ...]
    scope_refs: tuple[tuple[str, str], ...]
    approved_by: str
    style_overlay_id: str = ""


def create_policy_draft(session: Session, spec: PolicyDraft) -> AiContentPolicyVersion:
    _validate_policy_draft(spec)
    existing = session.scalar(select(AiContentPolicyVersion).where(
        AiContentPolicyVersion.tenant_id == spec.tenant_id,
        AiContentPolicyVersion.version == spec.version,
    ))
    policy_hash = _hash(asdict(spec))
    if existing is not None:
        if existing.policy_hash != policy_hash:
            raise AiContentPolicyConflict("ai_content_policy_version_conflict")
        return existing
    policy = AiContentPolicyVersion(
        **asdict(spec),
        policy_hash=policy_hash,
        status="draft",
    )
    session.add(policy)
    session.flush()
    return policy


def approve_policy(
    session: Session,
    policy_id: str,
    *,
    approved_by: str,
) -> AiContentPolicyVersion:
    policy = _policy_for_update(session, policy_id)
    if policy.status not in {"draft", "approved"}:
        raise AiContentPolicyConflict("ai_content_policy_not_approvable")
    policy.status = "approved"
    policy.approved_by = approved_by
    policy.approved_at = _now()
    session.flush()
    return policy


def activate_policy(session: Session, policy_id: str) -> AiContentPolicyVersion:
    policy = _policy_for_update(session, policy_id)
    if policy.status != "approved" or not policy.approved_by:
        raise AiContentPolicyConflict("ai_content_policy_not_approved")
    session.execute(update(AiContentPolicyVersion).where(
        AiContentPolicyVersion.tenant_id == policy.tenant_id,
        AiContentPolicyVersion.status == "active",
        AiContentPolicyVersion.id != policy.id,
    ).values(status="retired"))
    policy.status = "active"
    session.flush()
    return policy


def create_adult_attestation(
    session: Session,
    spec: AttestationSpec,
) -> AdultSubjectAttestation:
    _validate_attestation_spec(spec)
    evidence_hash = _hash(asdict(spec))
    values = asdict(spec)
    values["evidence_codes"] = list(spec.evidence_codes)
    attestation = AdultSubjectAttestation(
        **values,
        status="active",
        evidence_hash=evidence_hash,
    )
    session.add(attestation)
    session.flush()
    return attestation


def bind_task_policy(
    session: Session,
    spec: TaskBindingSpec,
) -> TaskAiContentPolicyBinding:
    task = session.get(Task, spec.task_id)
    if task is None or task.deleted_at is not None:
        raise AiContentPolicyConflict("ai_content_binding_task_missing")
    policy = session.get(AiContentPolicyVersion, spec.policy_version_id)
    if policy is None or policy.tenant_id != task.tenant_id or policy.status != "active":
        raise AiContentPolicyConflict("ai_content_binding_policy_not_active")
    routes = _validate_binding_routes(policy, spec.allowed_routes)
    attestations = _load_attestations(session, task, policy, spec.attestation_ids)
    _validate_adult_route_coverage(routes, spec.scope_refs, attestations)
    values = _binding_values(task, policy, spec, routes, attestations)
    existing = session.scalar(select(TaskAiContentPolicyBinding).where(
        TaskAiContentPolicyBinding.task_id == task.id,
        TaskAiContentPolicyBinding.task_lifecycle_epoch == task.task_lifecycle_epoch,
        TaskAiContentPolicyBinding.task_config_revision == task.config_revision,
    ))
    if existing is not None:
        if existing.evidence_hash != values["evidence_hash"]:
            raise AiContentPolicyConflict("ai_content_binding_revision_conflict")
        return existing
    binding = TaskAiContentPolicyBinding(**values)
    session.add(binding)
    session.flush()
    return binding


def assert_route_authorized(
    session: Session,
    binding: TaskAiContentPolicyBinding,
    *,
    route: str,
    scope_type: str,
    scope_id: str,
) -> None:
    if route not in set(binding.allowed_routes or []):
        raise AiContentPolicyConflict("content_route_not_allowed")
    if route == GENERAL_ROUTE:
        return
    attestations = list(session.scalars(select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(tuple(binding.attestation_ids or ())),
    )))
    valid = any(
        _attestation_current(item, binding, route, scope_type, scope_id)
        for item in attestations
    )
    if not valid:
        raise AiContentPolicyConflict("adult_attestation_stale")


def _validate_policy_draft(spec: PolicyDraft) -> None:
    if spec.version < 1:
        raise ValueError("ai_content_policy_version_invalid")
    routes = set(spec.route_rules.get("allowed_routes") or ())
    if not routes or not routes <= VALID_ROUTES:
        raise ValueError("ai_content_policy_routes_invalid")
    missing_prompts = routes - set(spec.prompt_registry)
    if missing_prompts:
        raise ValueError("ai_content_policy_prompt_registry_incomplete")


def _validate_attestation_spec(spec: AttestationSpec) -> None:
    if spec.scope_type not in {"task_group", "task_source"}:
        raise ValueError("adult_attestation_scope_invalid")
    if spec.subject_class not in set(ADULT_ROUTE_SUBJECTS.values()):
        raise ValueError("adult_attestation_subject_invalid")
    if not spec.evidence_codes or any(not item.strip() for item in spec.evidence_codes):
        raise ValueError("adult_attestation_evidence_missing")
    allowed = ADULT_SUBJECT_EVIDENCE[spec.subject_class]
    if not set(spec.evidence_codes) & allowed:
        raise ValueError("adult_attestation_evidence_weak")
    if spec.permission_snapshot.get("adult_content_attest") is not True:
        raise ValueError("adult_attestation_permission_missing")
    if spec.expires_at <= _now_like(spec.expires_at):
        raise ValueError("adult_attestation_expiry_invalid")


def _policy_for_update(session: Session, policy_id: str) -> AiContentPolicyVersion:
    policy = session.scalar(
        select(AiContentPolicyVersion)
        .where(AiContentPolicyVersion.id == policy_id)
        .with_for_update()
    )
    if policy is None:
        raise AiContentPolicyConflict("ai_content_policy_missing")
    if policy.status not in VALID_POLICY_STATUSES:
        raise AiContentPolicyConflict("ai_content_policy_status_invalid")
    return policy


def _validate_binding_routes(
    policy: AiContentPolicyVersion,
    allowed_routes: tuple[str, ...],
) -> tuple[str, ...]:
    routes = tuple(dict.fromkeys(allowed_routes))
    policy_routes = set(dict(policy.route_rules or {}).get("allowed_routes") or ())
    if not routes or not set(routes) <= policy_routes:
        raise AiContentPolicyConflict("content_route_not_allowed")
    return routes


def _load_attestations(
    session: Session,
    task: Task,
    policy: AiContentPolicyVersion,
    attestation_ids: tuple[str, ...],
) -> tuple[AdultSubjectAttestation, ...]:
    if not attestation_ids:
        return ()
    items = tuple(session.scalars(select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(attestation_ids),
    )))
    if len(items) != len(set(attestation_ids)):
        raise AiContentPolicyConflict("adult_attestation_missing")
    if any(
        item.tenant_id != task.tenant_id
        or item.policy_version != policy.version
        or item.task_config_revision != task.config_revision
        for item in items
    ):
        raise AiContentPolicyConflict("adult_attestation_scope_mismatch")
    return items


def _validate_adult_route_coverage(
    routes: tuple[str, ...],
    scope_refs: tuple[tuple[str, str], ...],
    attestations: tuple[AdultSubjectAttestation, ...],
) -> None:
    adult_routes = set(routes) & set(ADULT_ROUTE_SUBJECTS)
    if not adult_routes:
        return
    required_scopes = set(scope_refs)
    if not required_scopes:
        raise AiContentPolicyConflict("adult_attestation_scope_missing")
    now_value = _now()
    for route in adult_routes:
        subject = ADULT_ROUTE_SUBJECTS[route]
        covered = {
            (item.scope_type, item.scope_id)
            for item in attestations
            if item.subject_class == subject
            and item.status == "active"
            and item.expires_at > _now_like(item.expires_at, now_value)
        }
        if not required_scopes <= covered:
            raise AiContentPolicyConflict("adult_attestation_scope_missing")


def _binding_values(
    task: Task,
    policy: AiContentPolicyVersion,
    spec: TaskBindingSpec,
    routes: tuple[str, ...],
    attestations: tuple[AdultSubjectAttestation, ...],
) -> dict:
    evidence = {
        "task_id": task.id,
        "task_lifecycle_epoch": task.task_lifecycle_epoch,
        "task_config_revision": task.config_revision,
        "policy_hash": policy.policy_hash,
        "allowed_routes": routes,
        "attestation_hashes": sorted(item.evidence_hash for item in attestations),
        "style_overlay_id": spec.style_overlay_id,
    }
    return {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "task_lifecycle_epoch": task.task_lifecycle_epoch,
        "task_config_revision": task.config_revision,
        "policy_version_id": policy.id,
        "allowed_routes": list(routes),
        "attestation_ids": [item.id for item in attestations],
        "evidence_hash": _hash(evidence),
        "style_overlay_id": spec.style_overlay_id,
        "approved_by": spec.approved_by,
    }


def _attestation_current(
    item: AdultSubjectAttestation,
    binding: TaskAiContentPolicyBinding,
    route: str,
    scope_type: str,
    scope_id: str,
) -> bool:
    return bool(
        item.status == "active"
        and item.tenant_id == binding.tenant_id
        and item.task_config_revision == binding.task_config_revision
        and item.subject_class == ADULT_ROUTE_SUBJECTS.get(route)
        and item.scope_type == scope_type
        and item.scope_id == scope_id
        and item.expires_at > _now_like(item.expires_at)
    )


def _now_like(value: datetime, now_value: datetime | None = None) -> datetime:
    current = now_value or _now()
    if value.tzinfo is None:
        return current.replace(tzinfo=None)
    if current.tzinfo is None:
        return current.replace(tzinfo=value.tzinfo)
    return current.astimezone(value.tzinfo)


def _hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ADULT_ROUTE_SUBJECTS",
    "AiContentPolicyConflict",
    "AttestationSpec",
    "PolicyDraft",
    "TaskBindingSpec",
    "activate_policy",
    "approve_policy",
    "assert_route_authorized",
    "bind_task_policy",
    "create_adult_attestation",
    "create_policy_draft",
]
