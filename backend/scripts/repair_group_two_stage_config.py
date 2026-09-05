"""Restore group generation settings from the original immutable policy binding."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import (
    AdultSubjectAttestation, AiContentPolicyVersion, AuditLog, Task,
    TaskAiContentPolicyBinding,
)
from app.services._common import _now
from app.services.task_center.ai_content_policy import (
    AttestationSpec, TaskBindingSpec, _binding_values, _hash, _load_attestations,
    _validate_adult_route_coverage, _validate_attestation_spec, _validate_binding_routes,
    create_adult_attestation,
)
from app.services.task_center.task_ai_content_activation import (
    _scope_refs, _validate_provider_routes, activate_task_ai_content_config,
)


TENANT_ID = 1
CONFIG_KEYS = ("ai_two_stage_enabled", "ai_content_allowed_routes", "ai_content_attestation_ids")


@dataclass(frozen=True, kw_only=True)
class RepairOptions:
    deployed_sha: str
    task_ids: tuple[str, ...]
    actor: str
    approval_ref: str
    apply: bool = False
    expected_hash: str = ""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
        separators=(",", ":")).encode()).hexdigest()


def row_hash(row):
    return digest({column.key: getattr(row, column.key) for column in row.__table__.columns})


def _binding(session, task, *, lock=False):
    query = select(TaskAiContentPolicyBinding).where(
        TaskAiContentPolicyBinding.tenant_id == task.tenant_id,
        TaskAiContentPolicyBinding.task_id == task.id,
        TaskAiContentPolicyBinding.task_lifecycle_epoch == task.task_lifecycle_epoch,
        TaskAiContentPolicyBinding.task_config_revision == task.config_revision)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise RuntimeError("group_two_stage_original_binding_missing")
    return row


def _original_contract(session, task, *, binding, policy):
    if policy.tenant_id != task.tenant_id or policy.status != "active":
        raise RuntimeError("group_two_stage_original_policy_invalid")
    config = {**task.type_config, "ai_two_stage_enabled": True,
        "ai_content_allowed_routes": list(binding.allowed_routes),
        "ai_content_attestation_ids": list(binding.attestation_ids)}
    routes = _validate_binding_routes(policy, tuple(binding.allowed_routes))
    _validate_provider_routes(session, task, routes)
    spec = TaskBindingSpec(task_id=task.id, policy_version_id=policy.id,
        allowed_routes=routes, attestation_ids=tuple(binding.attestation_ids),
        scope_refs=_scope_refs(task, config), approved_by=policy.approved_by,
        style_overlay_id=binding.style_overlay_id)
    attestations = _load_attestations(session, task, policy, spec.attestation_ids)
    _validate_adult_route_coverage(routes, spec.scope_refs, attestations)
    values = _binding_values(task=task, policy=policy, spec=spec,
        routes=routes, attestations=attestations)
    return config, spec, values["evidence_hash"] == binding.evidence_hash


def _successor_spec(source, revision):
    values = {field: getattr(source, field) for field in AttestationSpec.__dataclass_fields__}
    values.update(evidence_codes=tuple(source.evidence_codes), task_config_revision=revision)
    spec = AttestationSpec(**values)
    _validate_attestation_spec(spec)
    return spec


def _attestation_plan(session, task, *, binding, lock):
    query = select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(binding.attestation_ids)).order_by(AdultSubjectAttestation.id)
    sources = list(session.scalars(query.with_for_update() if lock else query))
    if {item.id for item in sources} != set(binding.attestation_ids):
        raise RuntimeError("group_two_stage_attestation_missing")
    planned, projected = [], []
    for source in sources:
        spec = _successor_spec(source, task.config_revision + 1)
        identity = f"group-generation-repair:{task.id}:{task.task_lifecycle_epoch}:{spec.task_config_revision}:{source.id}"
        successor_id = str(uuid5(NAMESPACE_URL, identity))
        evidence_hash = _hash(asdict(spec))
        projected.append(SimpleNamespace(id=successor_id, evidence_hash=evidence_hash))
        planned.append({"source_id": source.id, "source_hash": row_hash(source),
            "successor_id": successor_id, "evidence_hash": evidence_hash,
            "attested_at": source.attested_at, "expires_at": source.expires_at})
    return planned, projected


def _proposal(session, task, *, binding, lock):
    policy_query = select(AiContentPolicyVersion).where(AiContentPolicyVersion.id == binding.policy_version_id)
    policy = session.scalar(policy_query.with_for_update() if lock else policy_query)
    planned, projected = _attestation_plan(session, task, binding=binding, lock=lock)
    config, spec, integrity = _original_contract(session, task, binding=binding, policy=policy)
    config["ai_content_attestation_ids"] = [item.id for item in projected]
    prospective = SimpleNamespace(id=task.id, tenant_id=task.tenant_id,
        task_lifecycle_epoch=task.task_lifecycle_epoch, config_revision=task.config_revision + 1)
    values = _binding_values(task=prospective, policy=policy,
        spec=replace(spec, attestation_ids=tuple(config["ai_content_attestation_ids"])),
        routes=tuple(binding.allowed_routes), attestations=tuple(projected))
    return config, planned, values["evidence_hash"], row_hash(policy), integrity


def _task_snapshot(session, task, *, lock):
    config = dict(task.type_config or {})
    if (task.tenant_id != TENANT_ID or task.type != "group_ai_chat" or task.status != "running"
            or task.deleted_at is not None or not config.get("ai_content_route_v2_enabled")):
        raise RuntimeError("group_two_stage_task_scope_changed")
    binding = _binding(session, task, lock=lock)
    if binding.policy_version_id != config.get("ai_content_policy_version_id"):
        raise RuntimeError("group_two_stage_policy_owner_mismatch")
    if (config.get("ai_two_stage_enabled") is True
            and config.get("ai_content_allowed_routes") == binding.allowed_routes
            and config.get("ai_content_attestation_ids") == binding.attestation_ids):
        raise RuntimeError("group_two_stage_repair_not_required")
    next_config, attestations, evidence_hash, policy_hash, integrity = _proposal(
        session, task, binding=binding, lock=lock)
    changes = {key: {"old": config.get(key), "new": next_config[key]}
        for key in CONFIG_KEYS if config.get(key) != next_config[key]}
    if not changes:
        raise RuntimeError("group_two_stage_repair_not_required")
    return {"id": task.id, "tenant_id": task.tenant_id, "type": task.type,
        "status": task.status, "epoch": task.task_lifecycle_epoch,
        "config_revision": task.config_revision, "config_hash": digest(config),
        "account_config_hash": digest(task.account_config),
        "policy_id": binding.policy_version_id, "policy_hash": policy_hash,
        "old_binding_id": binding.id, "old_binding_hash": row_hash(binding),
        "original_binding_evidence_matches_fields": integrity,
        "next_config_revision": task.config_revision + 1,
        "next_binding_evidence_hash": evidence_hash, "attestations": attestations,
        "changes": changes, "next_config_hash": digest(next_config)}


def snapshot(session, options):
    if os.environ.get("RELEASE_SHA") != options.deployed_sha:
        raise RuntimeError("group_two_stage_release_changed")
    if not options.task_ids or len(set(options.task_ids)) != len(options.task_ids):
        raise RuntimeError("group_two_stage_exact_tasks_required")
    query = select(Task).where(Task.id.in_(options.task_ids)).order_by(Task.id)
    tasks = list(session.scalars(query.with_for_update() if options.apply else query))
    if {task.id for task in tasks} != set(options.task_ids):
        raise RuntimeError("group_two_stage_task_missing")
    return {"deployed_sha": options.deployed_sha,
        "tasks": [_task_snapshot(session, task, lock=options.apply) for task in tasks]}


def _apply_task(session, old):
    task = session.get(Task, old["id"])
    for planned in old["attestations"]:
        source = session.get(AdultSubjectAttestation, planned["source_id"])
        successor = create_adult_attestation(session, _successor_spec(source, old["next_config_revision"]))
        successor.id, successor.attested_at = planned["successor_id"], source.attested_at
        if successor.evidence_hash != planned["evidence_hash"]:
            raise RuntimeError("group_two_stage_attestation_projection_mismatch")
    task.type_config = {**task.type_config, **{key: value["new"] for key, value in old["changes"].items()}}
    task.config_revision, task.updated_at = old["next_config_revision"], _now()
    session.flush()
    activate_task_ai_content_config(session, task)
    binding = _binding(session, task)
    if binding.evidence_hash != old["next_binding_evidence_hash"] or digest(task.type_config) != old["next_config_hash"]:
        raise RuntimeError("group_two_stage_new_binding_mismatch")
    return {"id": task.id, "config_revision": task.config_revision,
        "binding_id": binding.id, "binding_evidence_hash": binding.evidence_hash}


def apply_repair(session, options):
    if not options.apply or not options.actor.strip() or not options.approval_ref.strip():
        raise RuntimeError("group_two_stage_approval_required")
    before = snapshot(session, options)
    fingerprint = digest(before)
    if fingerprint != options.expected_hash:
        raise RuntimeError("group_two_stage_preview_drift")
    after = [_apply_task(session, old) for old in before["tasks"]]
    audit = AuditLog(tenant_id=TENANT_ID, actor=options.actor, action="repair_group_two_stage_config",
        target_type="task", target_id="group_two_stage_config",
        detail=json.dumps({"approval_ref": options.approval_ref,
            "before_hash": fingerprint, "before": before, "after": after,
            "job_mutations": 0, "daily_target_mutations": 0, "telegram_calls": 0}, sort_keys=True, default=str))
    session.add(audit)
    session.flush()
    return {"applied": True, "audit_id": audit.id, "after": after}


def run(options, session_factory=SessionLocal):
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            if not options.apply:
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL lock_timeout='2s'"))
            session.execute(text("SET LOCAL statement_timeout='12s'"))
        if not options.apply:
            before = snapshot(session, options)
            return {"mode": "preview", "fingerprint": digest(before), **before}
        result = apply_repair(session, options)
        session.commit()
        return result


def main():
    parser = argparse.ArgumentParser()
    for name in ("deployed-sha", "actor", "approval-ref"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-hash", default="")
    values = vars(parser.parse_args())
    values["task_ids"] = tuple(values.pop("task_id"))
    print(json.dumps(run(RepairOptions(**values)), ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
