from __future__ import annotations

import json
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_gateway import canonical_ai_model_identity
from app.models import (
    AiContentPolicyVersion,
    AuditLog,
    TaskAiContentPolicyBinding,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)
from app.services._common import _now

from .ai_content_policy import (
    ADULT_ROUTE_SUBJECTS,
    PolicyDraft,
    approve_policy,
    create_policy_draft,
    activate_policy,
)
from .ai_v2_canary_bootstrap_manifest import (
    BootstrapBudget,
    MANIFEST_ID,
    policy_payload,
    required_purposes,
)
from .ai_v2_canary_bootstrap_contract import (
    BootstrapChoices,
    RouteItemChoice,
    parse_choices,
)
from .ai_v2_canary_bootstrap_read import (
    attestation_snapshot as _attestation_snapshot,
    binding_row as _binding_row,
    canonical_hash as _hash,
    choice_snapshot as _choice_snapshot,
    current_policy as _current_policy,
    next_policy_version as _next_policy_version,
    policy_row as _policy_row,
    provider_snapshots as _provider_snapshots,
    route_snapshots as _route_snapshots,
    task_by_id as _task,
    task_row as _task_row,
    task_snapshot as _task_snapshot,
    v2_tasks as _v2_tasks,
    with_fingerprint as _with_fingerprint,
)
from .config_normalization import validated_type_config
from .task_ai_content_activation import activate_task_ai_content_config


SCRIPT_VERSION = "ai_v2_canary_bootstrap_v1"
QUIET_TASK_STATES = frozenset({"draft", "paused"})
AUDIT_ACTION = "应用AI V2单任务受保护bootstrap"


class AiV2BootstrapConflict(RuntimeError):
    pass


def preview_bootstrap(
    session: Session,
    tenant_id: int,
    choices: BootstrapChoices,
    *,
    lock: bool = False,
) -> dict:
    missing = _missing_choices(choices)
    task = (
        _task(session, tenant_id, choices.task_id, lock=lock)
        if choices.task_id
        else None
    )
    next_policy_version = _next_policy_version(session, tenant_id, lock=lock)
    purposes = _purposes(choices)
    snapshot = {
        "version": SCRIPT_VERSION,
        "manifest_id": MANIFEST_ID,
        "tenant_id": tenant_id,
        "choices": _choice_snapshot(choices),
        "next_policy_version": next_policy_version,
        "current_policy": _current_policy(session, tenant_id, lock=lock),
        "task": _task_snapshot(session, task, lock=lock),
        "existing_v2_tasks": _v2_tasks(session, tenant_id, lock=lock),
        "routes": _route_snapshots(session, tenant_id, purposes, lock=lock),
        "providers": _provider_snapshots(session, choices, lock=lock),
        "attestations": _attestation_snapshot(
            session,
            tenant_id,
            choices,
            task=task,
            next_policy_version=next_policy_version,
            lock=lock,
        ),
        "missing_user_choices": missing,
    }
    snapshot["blockers"] = _blockers(snapshot, choices)
    return _with_fingerprint(snapshot)


def apply_bootstrap(
    session: Session,
    tenant_id: int,
    choices: BootstrapChoices,
    *,
    expected_fingerprint: str,
) -> dict:
    prior = _prior_apply(session, tenant_id, choices)
    if prior:
        if prior.get("fingerprint") != expected_fingerprint:
            raise AiV2BootstrapConflict("ai_v2_bootstrap_approval_ref_reused")
        return {
            "applied": True,
            "idempotent": True,
            "readback": readback_bootstrap(session, tenant_id, choices.task_id),
        }
    preview = preview_bootstrap(session, tenant_id, choices, lock=True)
    if preview["fingerprint"] != expected_fingerprint:
        raise AiV2BootstrapConflict("ai_v2_bootstrap_fingerprint_mismatch")
    if preview["missing_user_choices"] or preview["blockers"]:
        raise AiV2BootstrapConflict("ai_v2_bootstrap_not_ready")
    policy = _apply_policy(session, tenant_id, choices=choices, preview=preview)
    _apply_routes(session, tenant_id, choices=choices, preview=preview)
    task, binding = _apply_task(
        session, tenant_id, choices=choices, policy_id=policy.id
    )
    _write_audit(
        session,
        tenant_id,
        choices=choices,
        preview=preview,
        policy=policy,
        binding=binding,
    )
    session.flush()
    return {
        "applied": True,
        "idempotent": False,
        "task_id": task.id,
        "policy_id": policy.id,
    }


def readback_bootstrap(session: Session, tenant_id: int, task_id: str) -> dict:
    task = _task(session, tenant_id, task_id, lock=False)
    if task is None:
        raise AiV2BootstrapConflict("ai_v2_bootstrap_task_missing")
    binding = session.scalar(
        select(TaskAiContentPolicyBinding).where(
            TaskAiContentPolicyBinding.task_id == task.id,
            TaskAiContentPolicyBinding.task_lifecycle_epoch
            == task.task_lifecycle_epoch,
            TaskAiContentPolicyBinding.task_config_revision == task.config_revision,
        )
    )
    policy = (
        session.get(AiContentPolicyVersion, binding.policy_version_id)
        if binding
        else None
    )
    purposes = required_purposes(
        tuple((task.type_config or {}).get("ai_content_allowed_routes") or ())
    )
    audit_row = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AUDIT_ACTION,
            AuditLog.target_id == task.id,
        )
        .order_by(AuditLog.id.desc())
    )
    return {
        "version": SCRIPT_VERSION,
        "task": _task_row(task),
        "binding": _binding_row(binding),
        "policy": _policy_row(policy),
        "routes": _route_snapshots(session, tenant_id, purposes, lock=False),
        "audit_id": audit_row.id if audit_row else None,
        "production_effect": "persisted_config_only",
        "production_fixed": False,
    }


def _missing_choices(choices: BootstrapChoices) -> list[str]:
    fields = {
        "deployed_sha": bool(choices.deployed_sha),
        "task_id": bool(choices.task_id),
        "expected_task_revision": choices.expected_task_revision > 0,
        "allowed_routes": bool(choices.allowed_routes),
        "max_cost_per_slot": choices.max_cost_per_slot > 0,
        "daily_ai_budget": choices.daily_ai_budget > 0,
        "sampling_manifest_hash": len(choices.sampling_manifest_hash) == 64,
        "requester": bool(choices.requester),
        "approver": bool(choices.approver),
        "approval_ref": bool(choices.approval_ref),
    }
    missing = [field for field, present in fields.items() if not present]
    if choices.allowed_routes and set(choices.routes) != set(_purposes(choices)):
        missing.append("route_items_for_every_required_purpose")
    if (
        any(route != "general" for route in choices.allowed_routes)
        and not choices.attestation_ids
    ):
        missing.append("adult_attestation_ids")
    return missing


def _blockers(snapshot: dict, choices: BootstrapChoices) -> list[str]:
    blockers = []
    task = snapshot["task"]
    if task and task["type"] != "group_ai_chat":
        blockers.append("task_type_not_group_ai_chat")
    if task and task["status"] not in QUIET_TASK_STATES:
        blockers.append("task_not_quiet")
    if task and task["config_revision"] != choices.expected_task_revision:
        blockers.append("task_revision_mismatch")
    if task and task["open_work"]["total"]:
        blockers.append("task_open_work_present")
    if task and task["voice"]["missing_count"]:
        blockers.append("voice_profile_coverage_incomplete")
    if task and not task["voice"]["account_count"]:
        blockers.append("voice_account_scope_empty")
    if snapshot["existing_v2_tasks"]:
        blockers.append("existing_v2_task_present")
    blockers.extend(_route_choice_blockers(choices, snapshot["providers"]))
    blockers.extend(_attestation_blockers(snapshot, choices))
    if choices.requester and choices.requester == choices.approver:
        blockers.append("requester_approver_must_differ")
    if choices.deployed_sha and not _release_sha_valid(choices.deployed_sha):
        blockers.append("deployed_sha_invalid")
    if choices.daily_ai_budget and choices.max_cost_per_slot > choices.daily_ai_budget:
        blockers.append("daily_ai_budget_below_slot_budget")
    return sorted(set(blockers))


def _route_choice_blockers(
    choices: BootstrapChoices, providers: list[dict]
) -> list[str]:
    blockers = []
    by_id = {item["id"]: item for item in providers}
    for purpose, items in choices.routes.items():
        priorities = [item.priority for item in items]
        if not items or priorities != list(range(1, len(items) + 1)):
            blockers.append(f"route_priority_invalid:{purpose}")
        identities = [_item_identity(item) for item in items]
        if len(identities) != len(set(identities)):
            blockers.append(f"route_identity_duplicate:{purpose}")
        for item in items:
            provider = by_id.get(item.provider_id, {})
            if not item.model_name or not 1 <= item.timeout_ms <= 30_000:
                blockers.append(f"route_item_invalid:{purpose}")
            if not item.rate_policy or not item.concurrency_policy:
                blockers.append(f"route_policy_missing:{purpose}")
            if not provider.get("ready"):
                blockers.append(f"provider_not_ready:{item.provider_id}")
            if provider.get("is_billable") and not provider.get("pricing_ready"):
                blockers.append(f"provider_pricing_missing:{item.provider_id}")
    blockers.extend(_reviewer_identity_blockers(choices))
    return blockers


def _reviewer_identity_blockers(choices: BootstrapChoices) -> list[str]:
    purposes = _purposes(choices)
    if not purposes:
        return []
    reviewer_purpose = purposes[-1]
    reviewer = {
        _item_identity(item) for item in choices.routes.get(reviewer_purpose, ())
    }
    generators = {
        _item_identity(item)
        for purpose, items in choices.routes.items()
        if purpose != reviewer_purpose
        for item in items
    }
    return ["semantic_reviewer_identity_overlap"] if reviewer & generators else []


def _attestation_blockers(snapshot: dict, choices: BootstrapChoices) -> list[str]:
    if len(snapshot["attestations"]) != len(choices.attestation_ids):
        return ["adult_attestation_missing"]
    adult_routes = set(choices.allowed_routes) & set(ADULT_ROUTE_SUBJECTS)
    if not adult_routes:
        return []
    rows = snapshot["attestations"]
    subjects = {item["subject_class"] for item in rows if item["current"]}
    required = {ADULT_ROUTE_SUBJECTS[route] for route in adult_routes}
    return [] if required <= subjects else ["adult_attestation_not_current"]


def _apply_policy(
    session: Session,
    tenant_id: int,
    *,
    choices: BootstrapChoices,
    preview: dict,
):
    payload = policy_payload(
        BootstrapBudget(choices.max_cost_per_slot, choices.daily_ai_budget)
    )
    policy = create_policy_draft(
        session,
        PolicyDraft(
            tenant_id=tenant_id,
            version=preview["next_policy_version"],
            route_rules={**payload["route_rules"], "manifest_id": MANIFEST_ID},
            prompt_registry=payload["prompt_registry"],
            gate_config=payload["gate_config"],
            example_set=payload["example_set"],
        ),
    )
    approve_policy(session, policy.id, approved_by=choices.approver)
    return activate_policy(session, policy.id)


def _apply_routes(
    session: Session,
    tenant_id: int,
    *,
    choices: BootstrapChoices,
    preview: dict,
) -> None:
    current = {item["purpose"]: item for item in preview["routes"]}
    for purpose, items in choices.routes.items():
        active_id = current.get(purpose, {}).get("id")
        active = session.get(TenantAiProviderRouteSet, active_id) if active_id else None
        if active:
            active.status = "retired"
            session.flush()
        revision = int(current.get(purpose, {}).get("max_revision") or 0) + 1
        route = TenantAiProviderRouteSet(
            tenant_id=tenant_id,
            purpose=purpose,
            revision=revision,
            status="active",
            content_hash=_hash([asdict(item) for item in items]),
            approved_by=choices.approver,
            approved_at=_now(),
        )
        session.add(route)
        session.flush()
        session.add_all(
            TenantAiProviderRouteItem(route_set_id=route.id, **asdict(item))
            for item in items
        )
    session.flush()


def _apply_task(
    session: Session,
    tenant_id: int,
    *,
    choices: BootstrapChoices,
    policy_id: str,
):
    task = _task(session, tenant_id, choices.task_id, lock=True)
    if task is None:
        raise AiV2BootstrapConflict("ai_v2_bootstrap_task_missing")
    config = dict(task.type_config or {})
    first_realizer = choices.routes[required_purposes(choices.allowed_routes)[1]][0]
    first_reviewer = choices.routes[required_purposes(choices.allowed_routes)[-1]][0]
    config.update(
        {
            "ai_two_stage_enabled": True,
            "ai_model": first_realizer.model_name,
            "ai_semantic_reviewer_model": first_reviewer.model_name,
            "ai_content_route_v2_enabled": True,
            "ai_content_policy_version_id": policy_id,
            "ai_content_allowed_routes": list(choices.allowed_routes),
            "ai_content_attestation_ids": list(choices.attestation_ids),
            "ai_content_policy_manifest_id": MANIFEST_ID,
            "ai_content_sampling_manifest_hash": choices.sampling_manifest_hash,
            "ai_content_max_cost_per_slot": choices.max_cost_per_slot,
            "ai_content_daily_budget": choices.daily_ai_budget,
        }
    )
    config.pop("ai_provider_id", None)
    task.config_revision += 1
    task.type_config = validated_type_config(task.type, config)
    task.updated_at = _now()
    activate_task_ai_content_config(session, task)
    binding = session.scalar(
        select(TaskAiContentPolicyBinding).where(
            TaskAiContentPolicyBinding.task_id == task.id,
            TaskAiContentPolicyBinding.task_config_revision == task.config_revision,
        )
    )
    if binding is None:
        raise AiV2BootstrapConflict("ai_v2_bootstrap_binding_missing")
    return task, binding


def _write_audit(  # noqa: ANN001
    session,
    tenant_id,
    *,
    choices,
    preview,
    policy,
    binding,
) -> None:
    detail = {
        "approval_ref": choices.approval_ref,
        "requester": choices.requester,
        "approver": choices.approver,
        "fingerprint": preview["fingerprint"],
        "deployed_sha": choices.deployed_sha,
        "old_policy_hash": (preview["current_policy"] or {}).get("policy_hash"),
        "new_policy_hash": policy.policy_hash,
        "binding_hash": binding.evidence_hash,
        "removed_legacy_ai_provider_id": int(
            (preview["task"] or {}).get("legacy_ai_provider_id") or 0
        ),
    }
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=choices.approver,
            action=AUDIT_ACTION,
            target_type="task",
            target_id=choices.task_id,
            detail=json.dumps(detail, sort_keys=True, separators=(",", ":")),
        )
    )


def _prior_apply(
    session: Session, tenant_id: int, choices: BootstrapChoices
) -> dict | None:
    if not choices.approval_ref or not choices.task_id:
        return None
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AUDIT_ACTION,
            AuditLog.target_id == choices.task_id,
        )
        .order_by(AuditLog.id.desc())
    )
    for row in rows:
        try:
            detail = json.loads(row.detail)
        except (TypeError, json.JSONDecodeError):
            continue
        if detail.get("approval_ref") == choices.approval_ref:
            return detail
    return None


def _purposes(choices: BootstrapChoices) -> tuple[str, ...]:
    try:
        return (
            required_purposes(choices.allowed_routes) if choices.allowed_routes else ()
        )
    except KeyError:
        return ()


def _item_identity(item: RouteItemChoice) -> str:
    return f"{item.provider_id}:{canonical_ai_model_identity(item.model_name)}"


def _release_sha_valid(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


__all__ = [
    "AiV2BootstrapConflict",
    "BootstrapChoices",
    "RouteItemChoice",
    "apply_bootstrap",
    "parse_choices",
    "preview_bootstrap",
    "readback_bootstrap",
]
