from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiAccountVoiceProfile,
    AiContentPolicyVersion,
    AdultSubjectAttestation,
    AiProvider,
    AiProviderHealthStatus,
    ExecutionAttempt,
    GenerationJob,
    Task,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TgAccount,
)
from app.services._common import _now

from .account_scope import _scope_account_ids
from .ai_v2_canary_bootstrap_contract import BootstrapChoices


OPEN_ACTION_STATES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)
OPEN_JOB_STATES = ("pending", "generating", "unknown")


def task_by_id(
    session: Session, tenant_id: int, task_id: str, *, lock: bool
) -> Task | None:
    stmt = select(Task).where(
        Task.id == task_id,
        Task.tenant_id == tenant_id,
        Task.deleted_at.is_(None),
    )
    return session.scalar(stmt.with_for_update() if lock else stmt)


def next_policy_version(session: Session, tenant_id: int, *, lock: bool) -> int:
    if lock:
        list(
            session.scalars(
                select(AiContentPolicyVersion)
                .where(
                    AiContentPolicyVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        )
    return (
        int(
            session.scalar(
                select(func.max(AiContentPolicyVersion.version)).where(
                    AiContentPolicyVersion.tenant_id == tenant_id,
                )
            )
            or 0
        )
        + 1
    )


def current_policy(session: Session, tenant_id: int, *, lock: bool) -> dict | None:
    stmt = select(AiContentPolicyVersion).where(
        AiContentPolicyVersion.tenant_id == tenant_id,
        AiContentPolicyVersion.status == "active",
    )
    return policy_row(session.scalar(stmt.with_for_update() if lock else stmt))


def v2_tasks(session: Session, tenant_id: int, *, lock: bool) -> list[dict]:
    stmt = select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    rows = list(session.scalars(stmt.with_for_update() if lock else stmt))
    return [
        task_row(item)
        for item in rows
        if (item.type_config or {}).get("ai_content_route_v2_enabled")
    ]


def task_snapshot(session: Session, task: Task | None, *, lock: bool) -> dict | None:
    if task is None:
        return None
    return {
        **task_row(task),
        "config_hash": canonical_hash(task.type_config or {}),
        "open_work": _open_work(session, task, lock=lock),
        "voice": _voice_snapshot(session, task, lock=lock),
    }


def provider_snapshots(
    session: Session, choices: BootstrapChoices, *, lock: bool
) -> list[dict]:
    ids = sorted(
        {item.provider_id for items in choices.routes.values() for item in items}
    )
    stmt = select(AiProvider).where(AiProvider.id.in_(ids))
    if lock:
        stmt = stmt.with_for_update()
    providers = {item.id: item for item in session.scalars(stmt)} if ids else {}
    return [
        provider_row(providers.get(provider_id), provider_id) for provider_id in ids
    ]


def _open_work(session: Session, task: Task, *, lock: bool) -> dict:
    action_stmt = select(Action).where(
        Action.task_id == task.id, Action.status.in_(OPEN_ACTION_STATES)
    )
    job_stmt = select(GenerationJob).where(
        GenerationJob.task_id == task.id, GenerationJob.state.in_(OPEN_JOB_STATES)
    )
    if lock:
        action_stmt = action_stmt.with_for_update()
        job_stmt = job_stmt.with_for_update()
    actions = list(session.scalars(action_stmt))
    jobs = list(session.scalars(job_stmt))
    attempts = _gateway_attempts(session, actions, lock=lock)
    return {
        "action_count": len(actions),
        "job_count": len(jobs),
        "gateway_started_count": len(attempts),
        "unknown_count": sum(item.status == "unknown_after_send" for item in actions),
        "total": len(actions) + len(jobs) + len(attempts),
        "state_hash": canonical_hash(
            {
                "actions": [_action_row(item) for item in actions],
                "jobs": [_job_row(item) for item in jobs],
            }
        ),
    }


def _gateway_attempts(session: Session, actions: list[Action], *, lock: bool) -> list:
    action_ids = [item.id for item in actions]
    if not action_ids:
        return []
    stmt = select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )
    return list(session.scalars(stmt.with_for_update() if lock else stmt))


def _voice_snapshot(session: Session, task: Task, *, lock: bool) -> dict:
    account_ids = _scope_account_ids(session, task)
    if lock and account_ids:
        list(
            session.scalars(
                select(TgAccount).where(TgAccount.id.in_(account_ids)).with_for_update()
            )
        )
        account_ids = _scope_account_ids(session, task)
    profiles = _voice_profiles(session, task, account_ids, lock=lock)
    ready = {
        item.account_id
        for item in profiles
        if str(item.short_prompt_summary or "").strip()
    }
    missing = sorted(set(account_ids) - ready)
    return {
        "account_count": len(set(account_ids)),
        "ready_count": len(ready),
        "missing_count": len(missing),
        "missing_ids_hash": canonical_hash(missing),
    }


def _voice_profiles(
    session: Session, task: Task, account_ids: list[int], *, lock: bool
) -> list[AiAccountVoiceProfile]:
    if not account_ids:
        return []
    stmt = select(AiAccountVoiceProfile).where(
        AiAccountVoiceProfile.tenant_id == task.tenant_id,
        AiAccountVoiceProfile.account_id.in_(account_ids),
        AiAccountVoiceProfile.status == "active",
        AiAccountVoiceProfile.quality_status == "active",
    )
    return list(session.scalars(stmt.with_for_update() if lock else stmt))


def _action_row(item: Action) -> dict:
    return {
        "id": item.id,
        "status": item.status,
        "action_version": item.action_version,
        "candidate_hash": item.candidate_hash,
    }


def _job_row(item: GenerationJob) -> dict:
    return {
        "id": item.id,
        "state": item.state,
        "job_version": item.job_version,
        "candidate_hash": item.candidate_hash,
    }


def route_snapshots(
    session: Session,
    tenant_id: int,
    purposes: tuple[str, ...],
    *,
    lock: bool,
) -> list[dict]:
    return [
        _active_route_row(session, tenant_id, purpose, lock=lock)
        for purpose in purposes
    ]


def attestation_snapshot(
    session: Session,
    tenant_id: int,
    choices: BootstrapChoices,
    *,
    task: Task | None,
    next_policy_version: int,
    lock: bool,
) -> list[dict]:
    stmt = select(AdultSubjectAttestation).where(
        AdultSubjectAttestation.id.in_(choices.attestation_ids)
    )
    if lock:
        stmt = stmt.with_for_update()
    rows = list(session.scalars(stmt)) if choices.attestation_ids else []
    return [
        _attestation_row(
            item,
            tenant_id,
            choices,
            task=task,
            next_policy_version=next_policy_version,
        )
        for item in rows
    ]


def _attestation_row(
    item: AdultSubjectAttestation,
    tenant_id: int,
    choices: BootstrapChoices,
    *,
    task: Task | None,
    next_policy_version: int,
) -> dict:
    target_scope = (
        str((task.type_config or {}).get("target_group_id") or "") if task else ""
    )
    current = bool(
        item.status == "active"
        and item.tenant_id == tenant_id
        and item.task_config_revision == choices.expected_task_revision + 1
        and item.policy_version == next_policy_version
        and item.scope_type == "task_group"
        and item.scope_id == target_scope
        and item.expires_at > _now_like(item.expires_at)
    )
    return {
        "id": item.id,
        "subject_class": item.subject_class,
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "current": current,
    }


def _now_like(value: datetime) -> datetime:
    current = _now()
    if value.tzinfo is None:
        return current.replace(tzinfo=None)
    if current.tzinfo is None:
        return current.replace(tzinfo=value.tzinfo)
    return current.astimezone(value.tzinfo)


def _active_route_row(
    session: Session, tenant_id: int, purpose: str, *, lock: bool
) -> dict:
    stmt = (
        select(TenantAiProviderRouteSet)
        .where(
            TenantAiProviderRouteSet.tenant_id == tenant_id,
            TenantAiProviderRouteSet.purpose == purpose,
        )
        .order_by(TenantAiProviderRouteSet.revision)
    )
    routes = list(session.scalars(stmt.with_for_update() if lock else stmt))
    route = next((item for item in routes if item.status == "active"), None)
    max_revision = max((item.revision for item in routes), default=0)
    if route is None:
        return {
            "purpose": purpose,
            "id": None,
            "revision": 0,
            "max_revision": max_revision,
            "content_hash": "",
            "items": [],
        }
    return {
        **_route_row(session, purpose, route, lock=lock),
        "max_revision": max_revision,
    }


def _route_row(session: Session, purpose: str, route, *, lock: bool) -> dict:  # noqa: ANN001
    stmt = (
        select(TenantAiProviderRouteItem)
        .where(
            TenantAiProviderRouteItem.route_set_id == route.id,
        )
        .order_by(TenantAiProviderRouteItem.priority)
    )
    items = list(session.scalars(stmt.with_for_update() if lock else stmt))
    return {
        "purpose": purpose,
        "id": route.id,
        "revision": route.revision,
        "content_hash": route.content_hash,
        "items": [route_item_row(item) for item in items],
    }


def choice_snapshot(choices: BootstrapChoices) -> dict:
    payload = asdict(choices)
    payload["route_items"] = {
        purpose: [asdict(item) for item in items]
        for purpose, items in choices.route_items
    }
    return payload


def task_row(task: Task) -> dict:
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "task_lifecycle_epoch": task.task_lifecycle_epoch,
        "config_revision": task.config_revision,
        "route_v2_enabled": bool(
            (task.type_config or {}).get("ai_content_route_v2_enabled")
        ),
    }


def provider_row(provider: AiProvider | None, provider_id: int) -> dict:
    if provider is None:
        return {
            "id": provider_id,
            "missing": True,
            "ready": False,
            "pricing_ready": False,
        }
    return {
        "id": provider.id,
        "credential_enabled": provider.credential_enabled,
        "is_active": provider.is_active,
        "health_status": provider.health_status,
        "is_billable": provider.is_billable,
        "input_price_per_1k": provider.input_price_per_1k,
        "output_price_per_1k": provider.output_price_per_1k,
        "currency": provider.currency,
        "pricing_ready": provider.input_price_per_1k > 0
        and provider.output_price_per_1k > 0,
        "ready": bool(
            provider.credential_enabled
            and provider.is_active
            and provider.health_status == AiProviderHealthStatus.HEALTHY.value
        ),
    }


def binding_row(binding) -> dict | None:  # noqa: ANN001
    if binding is None:
        return None
    return {
        "id": binding.id,
        "task_config_revision": binding.task_config_revision,
        "policy_version_id": binding.policy_version_id,
        "allowed_routes": binding.allowed_routes,
        "evidence_hash": binding.evidence_hash,
    }


def policy_row(policy) -> dict | None:  # noqa: ANN001
    if policy is None:
        return None
    return {
        "id": policy.id,
        "version": policy.version,
        "status": policy.status,
        "policy_hash": policy.policy_hash,
        "manifest_id": (policy.route_rules or {}).get("manifest_id", ""),
    }


def route_item_row(item: TenantAiProviderRouteItem) -> dict:
    return {
        "priority": item.priority,
        "provider_id": item.provider_id,
        "model_name": item.model_name,
        "enabled": item.enabled,
        "timeout_ms": item.timeout_ms,
        "rate_policy": item.rate_policy,
        "concurrency_policy": item.concurrency_policy,
    }


def with_fingerprint(body: dict) -> dict:
    return {**body, "fingerprint": canonical_hash(body)}


def canonical_hash(value) -> str:  # noqa: ANN001
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "attestation_snapshot",
    "binding_row",
    "canonical_hash",
    "choice_snapshot",
    "current_policy",
    "next_policy_version",
    "policy_row",
    "provider_row",
    "provider_snapshots",
    "route_snapshots",
    "task_by_id",
    "task_row",
    "task_snapshot",
    "v2_tasks",
    "with_fingerprint",
]
