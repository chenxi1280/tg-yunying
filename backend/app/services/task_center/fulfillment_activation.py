from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    Task,
    TaskContractActivationManifest,
    TaskContractRoute,
    TenantAiSetting,
    FulfillmentRemoteFact,
)
from app.services._common import _now


CURRENT_CONTRACT_VERSION = "fact_first_v3"
CANARY_FACT_KIND_BY_TASK_TYPE = {
    "search_click": "target_click_observed",
    "channel_view": "view_observed",
    "channel_like": "reaction_observed",
    "channel_comment": "remote_message_observed",
    "group_ai_chat": "remote_message_observed",
}


@dataclass(frozen=True)
class ActivationRequest:
    tenant_id: int
    release_train: str
    old_task_ids: tuple[str, ...]
    new_task_ids: tuple[str, ...]
    canary_task_id: str
    expected_old_set_hash: str
    expected_new_config_set_hash: str
    approval_ref: str


@dataclass(frozen=True)
class ActivationPreview:
    old_set_hash: str
    new_config_set_hash: str


def clone_prepared_task(session: Session, old_task: Task, *, actor_id: int | None) -> Task:
    clone = Task(
        id=str(uuid4()),
        tenant_id=old_task.tenant_id,
        name=old_task.name,
        type=old_task.type,
        status="prepared",
        priority=old_task.priority,
        timezone=old_task.timezone,
        scheduled_start=_now(),
        scheduled_end=old_task.scheduled_end,
        max_duration_hours=old_task.max_duration_hours,
        account_config=dict(old_task.account_config or {}),
        pacing_config=dict(old_task.pacing_config or {}),
        failure_policy=dict(old_task.failure_policy or {}),
        type_config=_clone_type_config(session, old_task),
        stats=_fresh_stats(old_task),
        config_revision=1,
        created_by_user_id=actor_id,
        task_lifecycle_epoch=1,
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
        group_ai_prejoin_channel_ids=list(old_task.group_ai_prejoin_channel_ids or []),
    )
    session.add(clone)
    session.flush()
    return clone


def _clone_type_config(session: Session, old_task: Task) -> dict:
    config = dict(old_task.type_config or {})
    if old_task.type != "group_ai_chat":
        return config
    if config.get("topic_participation_rate") is None:
        raise ValueError("topic_participation_rate_required")
    setting = session.scalar(select(TenantAiSetting).where(
        TenantAiSetting.tenant_id == old_task.tenant_id,
    ))
    provider_id = int(setting.default_provider_id or 0) if setting else 0
    if not provider_id:
        raise ValueError("fact_first_ai_provider_required")
    return {**config, "ai_provider_id": provider_id}


def prepare_activation_manifest(
    session: Session,
    request: ActivationRequest,
) -> TaskContractActivationManifest:
    old_tasks = _tasks(session, request.tenant_id, request.old_task_ids)
    new_tasks = _tasks(session, request.tenant_id, request.new_task_ids)
    _validate_activation_request(request, old_tasks, new_tasks)
    manifest = _new_manifest(
        session,
        request,
        old_tasks=old_tasks,
        new_tasks=new_tasks,
    )
    session.add(manifest)
    session.flush()
    _add_routes(
        session,
        manifest,
        old_tasks=old_tasks,
        new_tasks=new_tasks,
    )
    _start_canary_task(session, manifest, new_tasks)
    return manifest


def preview_activation(
    session: Session,
    *,
    tenant_id: int,
    old_task_ids: tuple[str, ...],
    new_task_ids: tuple[str, ...],
) -> ActivationPreview:
    old_tasks = _tasks(session, tenant_id, old_task_ids)
    new_tasks = _tasks(session, tenant_id, new_task_ids)
    return ActivationPreview(
        old_set_hash=_old_task_set_hash(old_tasks),
        new_config_set_hash=_new_task_set_hash(new_tasks),
    )


def activate_manifest(
    session: Session,
    manifest_id: str,
    *,
    expected_version: int,
) -> TaskContractActivationManifest:
    manifest = session.get(TaskContractActivationManifest, manifest_id)
    if manifest is None:
        raise ValueError("activation_manifest_not_found")
    _require_canary_remote_fact(session, manifest)
    changed = session.execute(
        update(TaskContractActivationManifest)
        .where(
            TaskContractActivationManifest.id == manifest_id,
            TaskContractActivationManifest.state == "canary",
            TaskContractActivationManifest.version == expected_version,
        )
        .values(state="active", version=expected_version + 1, activated_at=_now())
    ).rowcount
    if changed != 1:
        raise ValueError("activation_manifest_version_conflict")
    manifest = session.get(TaskContractActivationManifest, manifest_id)
    if manifest is None:
        raise RuntimeError("activation_manifest_missing_after_cas")
    _project_active_task_states(session, manifest)
    return manifest


def gateway_task_allowed(session: Session, task: Task) -> bool:
    routes = list(session.execute(
        select(TaskContractRoute.role, TaskContractActivationManifest)
        .join(
            TaskContractActivationManifest,
            TaskContractActivationManifest.id == TaskContractRoute.manifest_id,
        )
        .where(TaskContractRoute.task_id == task.id)
        .order_by(TaskContractActivationManifest.route_epoch.desc())
    ))
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return not any(role == "old" and manifest.state == "active" for role, manifest in routes)
    if not routes:
        return not bool(dict(task.stats or {}).get("requires_activation_manifest"))
    return any(_new_route_allowed(task.id, role, manifest) for role, manifest in routes)


def _new_route_allowed(
    task_id: str,
    role: str,
    manifest: TaskContractActivationManifest,
) -> bool:
    if role != "new":
        return False
    if manifest.state == "active":
        return True
    return manifest.state == "canary" and manifest.canary_task_id == task_id


def _tasks(session: Session, tenant_id: int, task_ids: tuple[str, ...]) -> list[Task]:
    rows = list(session.scalars(select(Task).where(
        Task.tenant_id == tenant_id,
        Task.id.in_(task_ids),
    )))
    if {row.id for row in rows} != set(task_ids):
        raise ValueError("activation_task_set_mismatch")
    return rows


def _validate_activation_request(
    request: ActivationRequest,
    old_tasks: list[Task],
    new_tasks: list[Task],
) -> None:
    if not old_tasks or not new_tasks or request.canary_task_id not in request.new_task_ids:
        raise ValueError("activation_task_set_invalid")
    if set(request.old_task_ids) & set(request.new_task_ids):
        raise ValueError("activation_task_sets_overlap")
    if any(task.status != "prepared" for task in new_tasks):
        raise ValueError("activation_new_task_not_prepared")
    if any(task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION for task in new_tasks):
        raise ValueError("activation_new_task_contract_mismatch")
    preview = ActivationPreview(
        old_set_hash=_old_task_set_hash(old_tasks),
        new_config_set_hash=_new_task_set_hash(new_tasks),
    )
    if request.expected_old_set_hash != preview.old_set_hash:
        raise ValueError("activation_old_task_set_changed")
    if request.expected_new_config_set_hash != preview.new_config_set_hash:
        raise ValueError("activation_new_task_config_changed")
    if not request.approval_ref.strip():
        raise ValueError("activation_approval_ref_required")


def _new_manifest(
    session: Session,
    request: ActivationRequest,
    *,
    old_tasks: list[Task],
    new_tasks: list[Task],
) -> TaskContractActivationManifest:
    route_epoch = int(session.scalar(select(func.max(
        TaskContractActivationManifest.route_epoch
    )).where(
        TaskContractActivationManifest.tenant_id == request.tenant_id,
        TaskContractActivationManifest.release_train == request.release_train,
    )) or 0) + 1
    return TaskContractActivationManifest(
        tenant_id=request.tenant_id,
        release_train=request.release_train,
        old_task_ids=list(request.old_task_ids),
        new_task_ids=list(request.new_task_ids),
        canary_task_id=request.canary_task_id,
        old_set_hash=request.expected_old_set_hash,
        new_config_set_hash=request.expected_new_config_set_hash,
        route_epoch=route_epoch,
        state="canary",
        approval_ref=request.approval_ref,
    )


def _add_routes(
    session: Session,
    manifest: TaskContractActivationManifest,
    *,
    old_tasks: list[Task],
    new_tasks: list[Task],
) -> None:
    for role, tasks in (("old", old_tasks), ("new", new_tasks)):
        for task in tasks:
            session.add(TaskContractRoute(
                manifest_id=manifest.id,
                task_id=task.id,
                role=role,
                config_hash=_task_hash(task),
                expected_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
            ))
    session.flush()


def _start_canary_task(
    session: Session,
    manifest: TaskContractActivationManifest,
    new_tasks: list[Task],
) -> None:
    for task in new_tasks:
        if task.id == manifest.canary_task_id:
            task.status = "running"
            task.next_run_at = _now()


def _project_active_task_states(
    session: Session,
    manifest: TaskContractActivationManifest,
) -> None:
    routes = list(session.scalars(select(TaskContractRoute).where(
        TaskContractRoute.manifest_id == manifest.id,
    )))
    for route in routes:
        values = (
            {"status": "running", "next_run_at": _now(), "last_error": ""}
            if route.role == "new"
            else {"status": "stopped", "next_run_at": None}
        )
        changed = session.execute(
            update(Task)
            .where(
                Task.id == route.task_id,
                Task.task_lifecycle_epoch == route.expected_lifecycle_epoch,
            )
            .values(**values)
        ).rowcount
        if changed != 1:
            raise ValueError("activation_task_lifecycle_epoch_conflict")


def _fresh_stats(old_task: Task) -> dict:
    return {
        "fulfillment_contract_version": CURRENT_CONTRACT_VERSION,
        "same_day_recreate_resets_progress": True,
        "recreated_from_task_id": old_task.id,
        "requires_activation_manifest": True,
        "created_at": _now().isoformat(),
    }


def _require_canary_remote_fact(
    session: Session,
    manifest: TaskContractActivationManifest,
) -> None:
    canary = session.get(Task, manifest.canary_task_id)
    if canary is None:
        raise ValueError("activation_canary_task_missing")
    expected_kind = CANARY_FACT_KIND_BY_TASK_TYPE.get(canary.type)
    if expected_kind is None:
        raise ValueError("activation_canary_task_type_unsupported")
    fact_id = session.scalar(
        select(FulfillmentRemoteFact.fact_id)
        .join(Action, Action.id == FulfillmentRemoteFact.action_id)
        .join(ExecutionAttempt, ExecutionAttempt.id == FulfillmentRemoteFact.attempt_id)
        .where(
            FulfillmentRemoteFact.task_id == manifest.canary_task_id,
            Action.task_id == manifest.canary_task_id,
            Action.obligation_type == FulfillmentRemoteFact.obligation_type,
            Action.obligation_id == FulfillmentRemoteFact.obligation_id,
            ExecutionAttempt.action_id == Action.id,
            FulfillmentRemoteFact.fact_kind == expected_kind,
        )
        .limit(1)
    )
    if fact_id is None:
        raise ValueError("activation_canary_remote_fact_required")


def _old_task_set_hash(tasks: list[Task]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(
                (
                    task.id,
                    int(task.task_lifecycle_epoch or 1),
                    int(task.config_revision or 1),
                    task.fulfillment_contract_version,
                    _task_hash(task),
                )
                for task in tasks
            ),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _new_task_set_hash(tasks: list[Task]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted((task.id, _task_hash(task)) for task in tasks),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _task_hash(task: Task) -> str:
    payload = {
        "type": task.type,
        "priority": task.priority,
        "scheduled_end": task.scheduled_end.isoformat() if task.scheduled_end else None,
        "max_duration_hours": task.max_duration_hours,
        "account_config": task.account_config or {},
        "pacing_config": task.pacing_config or {},
        "failure_policy": task.failure_policy or {},
        "type_config": task.type_config or {},
        "timezone": task.timezone,
        "group_ai_prejoin_channel_ids": task.group_ai_prejoin_channel_ids or [],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "ActivationRequest",
    "ActivationPreview",
    "CURRENT_CONTRACT_VERSION",
    "activate_manifest",
    "clone_prepared_task",
    "gateway_task_allowed",
    "prepare_activation_manifest",
    "preview_activation",
]
