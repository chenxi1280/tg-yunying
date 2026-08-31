from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Task,
)
from app.schemas.task_center import (
    GroupCloneConfig,
    GroupCloneTaskConfigUpdate,
    GroupCloneTaskCreate,
)

from .group_mutation_authority import check_and_claim_exclusive_authority, compute_route_hash
from .group_clone_precheck import (
    CloneResolvedConfig,
    precheck_group_clone,
    request_fingerprint,
    resolve_clone_config,
    validate_clone_rule_binding,
)
from .group_clone_start_rows import initialize_start_rows

GROUP_CLONE_CONTRACT = "v2_group_clone"


def create_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    fingerprint = request_fingerprint(payload)
    existing = _idempotent_task(session, user_id, payload.client_request_id)
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("client_request_id 已用于不同的 group_clone 配置")
        return existing, False
    precheck = precheck_group_clone(session, tenant_id, payload)
    if precheck.hard_blocks:
        raise ValueError(f"group_clone 配置不可创建: {precheck.hard_blocks}")
    task = _new_task(tenant_id, user_id, payload=payload, fingerprint=fingerprint)
    session.add(task)
    session.flush()
    return task, True


def create_and_start_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    task, created = create_group_clone_task(session, tenant_id, user_id, payload=payload)
    if not created:
        return task, False
    resolved = _resolved_or_raise(session, tenant_id, payload)
    route_hash = compute_route_hash(
        payload.source.peer_type,
        payload.source.peer_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
    )
    claimed, reason, authority = check_and_claim_exclusive_authority(
        session,
        tenant_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
        writer_kind="group_clone",
        writer_id=task.id,
        route_hash=route_hash,
    )
    if not claimed or authority is None:
        raise ValueError(f"目标群独占写权限申请失败: {reason}")
    initialize_start_rows(session, task, payload=payload, resolved=resolved, route_hash=route_hash)
    task.status = "pending"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "starting"}
    return task, True


def create_cutover_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    fingerprint = request_fingerprint(payload)
    existing = _idempotent_task(session, user_id, payload.client_request_id)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("client_request_id 已用于不同的 group_clone 配置")
        return existing, False
    resolved = _resolved_or_raise(session, tenant_id, payload)
    task = _new_task(tenant_id, user_id, payload=payload, fingerprint=fingerprint)
    session.add(task)
    session.flush()
    route_hash = compute_route_hash(
        payload.source.peer_type,
        payload.source.peer_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
    )
    initialize_start_rows(
        session, task, payload=payload, resolved=resolved, route_hash=route_hash,
    )
    task.status = "pending"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "starting"}
    return task, True


def update_group_clone_config(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    payload: GroupCloneTaskConfigUpdate,
) -> Task:
    task = _tenant_task(session, tenant_id, task_id, for_update=True)
    if task.type != "group_clone":
        raise ValueError("非 group_clone 任务")
    current = dict(task.type_config or {})
    mutable = payload.model_dump(mode="json")
    current_senders = set((current.get("sender_pool") or {}).get("account_ids") or [])
    requested_senders = set((mutable.get("sender_pool") or {}).get("account_ids") or [])
    if requested_senders != current_senders:
        raise ValueError("sender_pool.account_ids 变更必须走受控账号槽交接")
    candidate = GroupCloneConfig.model_validate({**current, **mutable})
    validate_clone_rule_binding(session, tenant_id, candidate.content)
    task.type_config = candidate.model_dump(mode="json")
    task.config_revision = int(task.config_revision or 1) + 1
    return task


def tenant_clone_task(session: Session, tenant_id: int, task_id: str) -> Task:
    task = _tenant_task(session, tenant_id, task_id, for_update=False)
    if task.type != "group_clone":
        raise ValueError("非 group_clone 任务")
    return task


def _new_task(tenant_id, user_id, *, payload, fingerprint) -> Task:
    config = GroupCloneConfig(
        source=payload.source,
        target=payload.target,
        sender_pool=payload.sender_pool,
        pacing=payload.pacing,
        content=payload.content,
        lifecycle=payload.lifecycle,
        retention=payload.retention,
    ).model_dump(mode="json")
    return Task(
        tenant_id=tenant_id,
        name=payload.name,
        type="group_clone",
        status="stopped",
        priority=payload.priority,
        timezone=payload.timezone,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        max_duration_hours=payload.max_duration_hours,
        account_config=payload.account_config.model_dump(mode="json"),
        pacing_config=payload.pacing_config.model_dump(mode="json"),
        failure_policy=payload.failure_policy.model_dump(mode="json"),
        type_config=config,
        fulfillment_contract_version=GROUP_CLONE_CONTRACT,
        created_by_user_id=user_id,
        create_task_type="group_clone",
        client_request_id=payload.client_request_id,
        request_fingerprint=fingerprint,
        idempotency_legacy_unproven=False,
        stats={"clone_start_state": "stopped"},
    )


def _resolved_or_raise(session, tenant_id, payload) -> CloneResolvedConfig:
    blocks: list[str] = []
    resolved = resolve_clone_config(session, tenant_id, payload=payload, blocks=blocks)
    if resolved is None or blocks:
        raise ValueError(f"group_clone start precheck 漂移: {blocks}")
    return resolved


def _idempotent_task(session, user_id, client_request_id) -> Task | None:
    if not client_request_id:
        return None
    return session.scalar(select(Task).where(
        Task.created_by_user_id == user_id,
        Task.create_task_type == "group_clone",
        Task.client_request_id == client_request_id,
    ).with_for_update())


def _tenant_task(session, tenant_id, task_id, *, for_update):
    stmt = select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    task = session.scalar(stmt.with_for_update() if for_update else stmt)
    if task is None:
        raise LookupError("任务不存在")
    return task


__all__ = [
    "create_and_start_group_clone_task",
    "create_cutover_group_clone_task",
    "create_group_clone_task",
    "precheck_group_clone",
    "request_fingerprint",
    "tenant_clone_task",
    "update_group_clone_config",
]
