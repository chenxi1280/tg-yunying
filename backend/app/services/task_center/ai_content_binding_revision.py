"""Carry proven content authority across an adjacent non-content revision."""
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.models import AdultSubjectAttestation, AiContentPolicyVersion, Task, TaskAiContentPolicyBinding
from app.services._common import audit

from .ai_content_policy import (
    AttestationSpec, TaskBindingSpec, _binding_values, _hash, _load_attestations,
    _validate_adult_route_coverage, _validate_attestation_spec, _validate_binding_routes,
    bind_task_policy,
)
from .runtime_state_hash import canonical_state_hash
from .task_ai_content_activation import _scope_refs, _validate_provider_routes


def preview_content_binding_revision(session, task_id, *, source_revision, lock=False, restore_original_references=False):
    if not isinstance(restore_original_references, bool):
        raise ValueError("content_revision_restore_option_invalid")
    task, binding, policy = _load_scope(session, task_id, source_revision=source_revision, lock=lock,
        restore_original_references=restore_original_references)
    specs = _source_authority(session, task, binding=binding, policy=policy, lock=lock)
    projected = [_successor(task, item) for item in specs]
    state = {
        "task_id": task.id, "tenant_id": task.tenant_id, "epoch": task.task_lifecycle_epoch,
        "revision": task.config_revision, "source_revision": source_revision,
        "restore_original_references": restore_original_references,
        "task_hash": canonical_state_hash({"config": task.type_config,
            "prejoin": task.group_ai_prejoin_channel_ids, "status": task.status}),
        "binding_id": binding.id, "binding_hash": binding.evidence_hash,
        "policy_hash": canonical_state_hash(_row_values(policy)),
        "attestation_hashes": [canonical_state_hash(_row_values(item)) for item in specs],
        "successor_ids": [item[0] for item in projected],
    }
    return {**state, "fingerprint": canonical_state_hash(state)}


def apply_content_binding_revision(session, preview, *, actor, approval_reference):
    if not actor or not approval_reference:
        raise ValueError("content_revision_audit_required")
    current = preview_content_binding_revision(session, preview["task_id"],
        source_revision=preview["source_revision"], lock=True,
        restore_original_references=preview["restore_original_references"])
    if current != preview:
        raise RuntimeError("content_revision_fingerprint_drift")
    task, binding, policy = _load_scope(session, preview["task_id"],
        source_revision=preview["source_revision"], lock=True,
        restore_original_references=preview["restore_original_references"])
    sources = _source_authority(session, task, binding=binding, policy=policy, lock=True)
    attestation_ids = [_append_attestation(session, task, source) for source in sources]
    task.type_config = {**task.type_config, "ai_content_attestation_ids": attestation_ids}
    spec = _binding_spec(task, binding, attestation_ids=tuple(attestation_ids))
    result = bind_task_policy(session, spec)
    audit(session, tenant_id=task.tenant_id, actor=actor,
        action="补齐非内容修订的AI策略绑定", target_type="task", target_id=task.id,
        detail=f"approval={approval_reference};fingerprint={preview['fingerprint']};binding={result.id};restore_refs={preview['restore_original_references']}")
    session.flush()
    return {"task_id": task.id, "binding_id": result.id, "revision": task.config_revision,
            "evidence_hash": result.evidence_hash, "fingerprint": preview["fingerprint"]}


def carry_prejoin_content_binding(session, task, *, source_revision, actor):
    if not (task.type_config or {}).get("ai_content_route_v2_enabled"):
        return
    preview = preview_content_binding_revision(session, task.id, source_revision=source_revision)
    apply_content_binding_revision(session, preview, actor=actor,
        approval_reference="task_prejoin_config_update")


def _load_scope(session, task_id, *, source_revision, lock, restore_original_references=False):
    task = _one(session, select(Task).where(Task.id == task_id), lock=lock)
    if (task is None or task.type != "group_ai_chat" or task.deleted_at is not None
            or task.retired_at is not None or task.config_revision != source_revision + 1
            or not (task.type_config or {}).get("ai_content_route_v2_enabled")):
        raise ValueError("content_revision_task_scope_invalid")
    base = select(TaskAiContentPolicyBinding).where(
        TaskAiContentPolicyBinding.tenant_id == task.tenant_id,
        TaskAiContentPolicyBinding.task_id == task.id,
        TaskAiContentPolicyBinding.task_lifecycle_epoch == task.task_lifecycle_epoch)
    existing = _one(session, base.where(
        TaskAiContentPolicyBinding.task_config_revision == task.config_revision), lock=lock)
    if existing is not None:
        raise ValueError("content_revision_binding_already_present")
    binding = _one(session, base.where(
        TaskAiContentPolicyBinding.task_config_revision == source_revision), lock=lock)
    if binding is None:
        raise ValueError("content_revision_original_binding_missing")
    policy = _one(session, select(AiContentPolicyVersion).where(
        AiContentPolicyVersion.id == binding.policy_version_id), lock=lock)
    config = task.type_config or {}
    if (policy is None or policy.tenant_id != task.tenant_id or policy.status != "active"
            or config.get("ai_content_policy_version_id") != policy.id
            or list(config.get("ai_content_allowed_routes") or []) != list(binding.allowed_routes or [])
            or (not restore_original_references and
                list(config.get("ai_content_attestation_ids") or []) != list(binding.attestation_ids or []))
            or not config.get("ai_two_stage_enabled")):
        raise ValueError("content_revision_authority_changed")
    return task, binding, policy


def _source_authority(session, task, *, binding, policy, lock=False):
    original = SimpleNamespace(id=task.id, tenant_id=task.tenant_id,
        config_revision=binding.task_config_revision, task_lifecycle_epoch=task.task_lifecycle_epoch)
    spec = _binding_spec(task, binding, attestation_ids=tuple(binding.attestation_ids or []))
    routes = _validate_binding_routes(policy, spec.allowed_routes)
    _validate_provider_routes(session, task, routes)
    if lock:
        session.scalars(select(AdultSubjectAttestation).where(
            AdultSubjectAttestation.id.in_(spec.attestation_ids)).with_for_update()
            .execution_options(populate_existing=True)).all()
    sources = _load_attestations(session, original, policy, spec.attestation_ids)
    _validate_adult_route_coverage(routes, spec.scope_refs, sources)
    values = _binding_values(original, policy, spec, routes, sources)
    if values["evidence_hash"] != binding.evidence_hash:
        raise ValueError("content_revision_original_evidence_mismatch")
    for source in sources:
        _validate_attestation_spec(_spec(source))
    return tuple(sorted(sources, key=lambda item: item.id))


def _binding_spec(task, binding, *, attestation_ids):
    return TaskBindingSpec(task_id=task.id, policy_version_id=binding.policy_version_id,
        allowed_routes=tuple(binding.allowed_routes or []), attestation_ids=attestation_ids,
        scope_refs=_scope_refs(task, task.type_config), approved_by=binding.approved_by,
        style_overlay_id=binding.style_overlay_id)


def _spec(source):
    values = {key: getattr(source, key) for key in AttestationSpec.__dataclass_fields__}
    return AttestationSpec(**{**values, "evidence_codes": tuple(values["evidence_codes"])})


def _successor(task, source):
    spec = replace(_spec(source), task_config_revision=task.config_revision)
    identity = f"content-revision:{task.id}:{task.task_lifecycle_epoch}:{task.config_revision}:{source.id}"
    return str(uuid5(NAMESPACE_URL, identity)), spec


def _append_attestation(session, task, source):
    identity, spec = _successor(task, source)
    if session.get(AdultSubjectAttestation, identity) is not None:
        raise RuntimeError("content_revision_successor_already_present")
    _validate_attestation_spec(spec)
    successor = AdultSubjectAttestation(id=identity, **asdict(spec),
        status="active", evidence_hash=_hash(asdict(spec)), attested_at=source.attested_at)
    session.add(successor)
    session.flush()
    return successor.id


def _one(session, statement, *, lock):
    return session.scalar(statement.with_for_update().execution_options(populate_existing=True)
        if lock else statement)


def _row_values(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}
