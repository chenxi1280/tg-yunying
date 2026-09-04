from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import OperationTarget, Task, TaskTargetScopeClaim, TgGroup
from app.services._common import _now


UNIFIED_ADAPTER_TYPES = frozenset({
    "group_ai_chat",
    "channel_comment",
    "channel_like",
    "channel_view",
})
ACTIVE_TASK_STATUSES = frozenset({"pending", "running"})


class TaskTargetScopeConflict(ValueError):
    def __init__(self, holder_task_id: str) -> None:
        self.holder_task_id = holder_task_id
        super().__init__(f"task_target_scope_conflict:{holder_task_id}")


@dataclass(frozen=True)
class TargetScope:
    adapter_type: str
    canonical_peer_id: str
    target_kind: str


def ensure_task_target_scope_claims(
    session: Session,
    task: Task,
) -> tuple[TaskTargetScopeClaim, ...]:
    scopes = task_target_scopes(session, task)
    _release_unexpected_claims(session, task, scopes)
    return tuple(_acquire_scope(session, task, scope) for scope in scopes)


def task_target_scopes(session: Session, task: Task) -> tuple[TargetScope, ...]:
    config = dict(task.type_config or {})
    if (
        task.type not in UNIFIED_ADAPTER_TYPES
        or config.get("engagement_contract_version") != "unified_engagement_v1"
    ):
        return ()
    if task.type == "group_ai_chat":
        peer_id = _group_peer_id(session, task, config)
        return (TargetScope(task.type, peer_id, "group"),)
    target = _operation_target(
        session, task, int(config.get("target_channel_id") or 0), "channel"
    )
    return (TargetScope(task.type, _canonical_peer(target.tg_peer_id), "channel"),)


def release_task_target_scope_claims(
    session: Session,
    task: Task,
    *,
    reason: str,
) -> int:
    claims = _task_active_claims(session, task.id, lock=True)
    for claim in claims:
        _release_claim(claim, reason)
    return len(claims)


def has_current_task_target_scope_claim(session: Session, task: Task) -> bool:
    scopes = task_target_scopes(session, task)
    if not scopes:
        return True
    expected = {(scope.adapter_type, scope.canonical_peer_id) for scope in scopes}
    actual = {
        (claim.adapter_type, claim.canonical_peer_id)
        for claim in _task_active_claims(session, task.id, lock=False)
        if claim.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1)
    }
    return actual == expected


def active_unified_group_ai_owner(
    session: Session,
    *,
    tenant_id: int,
    canonical_peer_id: str,
) -> str | None:
    scope = TargetScope(
        adapter_type="group_ai_chat",
        canonical_peer_id=_canonical_peer(canonical_peer_id),
        target_kind="group",
    )
    claim = _active_scope_claim(session, tenant_id, scope, lock=False)
    if claim is None or _claim_is_stale(session, claim):
        return None
    return claim.task_id


def _acquire_scope(
    session: Session,
    task: Task,
    scope: TargetScope,
) -> TaskTargetScopeClaim:
    current = _active_scope_claim(session, task.tenant_id, scope, lock=True)
    if current is not None and _claim_is_stale(session, current):
        _release_claim(current, "stale_holder")
        session.flush()
        current = None
    if current is not None:
        if _claim_owned_by_task_epoch(current, task):
            return current
        raise TaskTargetScopeConflict(current.task_id)
    return _insert_scope_claim(session, task, scope)


def _insert_scope_claim(
    session: Session,
    task: Task,
    scope: TargetScope,
) -> TaskTargetScopeClaim:
    claim = TaskTargetScopeClaim(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        adapter_type=scope.adapter_type,
        canonical_peer_id=scope.canonical_peer_id,
        target_kind=scope.target_kind,
        acquired_at=_now(),
    )
    try:
        with session.begin_nested():
            session.add(claim)
            session.flush()
    except IntegrityError:
        holder = _active_scope_claim(session, task.tenant_id, scope, lock=False)
        if holder is not None and _claim_owned_by_task_epoch(holder, task):
            return holder
        raise TaskTargetScopeConflict(holder.task_id if holder else "unknown")
    return claim


def _release_unexpected_claims(
    session: Session,
    task: Task,
    scopes: tuple[TargetScope, ...],
) -> None:
    expected = {(scope.adapter_type, scope.canonical_peer_id) for scope in scopes}
    epoch = int(task.task_lifecycle_epoch or 1)
    for claim in _task_active_claims(session, task.id, lock=True):
        identity = (claim.adapter_type, claim.canonical_peer_id)
        if claim.task_lifecycle_epoch != epoch or identity not in expected:
            _release_claim(claim, "scope_or_epoch_superseded")


def _active_scope_claim(
    session: Session,
    tenant_id: int,
    scope: TargetScope,
    *,
    lock: bool,
) -> TaskTargetScopeClaim | None:
    statement = select(TaskTargetScopeClaim).where(
        TaskTargetScopeClaim.tenant_id == tenant_id,
        TaskTargetScopeClaim.adapter_type == scope.adapter_type,
        TaskTargetScopeClaim.canonical_peer_id == scope.canonical_peer_id,
        TaskTargetScopeClaim.state == "active",
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _task_active_claims(
    session: Session,
    task_id: str,
    *,
    lock: bool,
) -> list[TaskTargetScopeClaim]:
    statement = select(TaskTargetScopeClaim).where(
        TaskTargetScopeClaim.task_id == task_id,
        TaskTargetScopeClaim.state == "active",
    )
    if lock and session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    return list(session.scalars(statement))


def _claim_is_stale(session: Session, claim: TaskTargetScopeClaim) -> bool:
    holder = session.get(Task, claim.task_id)
    return (
        holder is None
        or holder.deleted_at is not None
        or holder.status not in ACTIVE_TASK_STATUSES
        or int(holder.task_lifecycle_epoch or 1) != claim.task_lifecycle_epoch
    )


def _claim_owned_by_task_epoch(claim: TaskTargetScopeClaim, task: Task) -> bool:
    return (
        claim.task_id == task.id
        and claim.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1)
    )


def _release_claim(claim: TaskTargetScopeClaim, reason: str) -> None:
    claim.state = "released"
    claim.released_at = _now()
    claim.release_reason = reason


def _group_peer_id(session: Session, task: Task, config: dict) -> str:
    operation_target_id = int(config.get("target_operation_target_id") or 0)
    if operation_target_id:
        target = _operation_target(session, task, operation_target_id, "group")
        return _canonical_peer(target.tg_peer_id)
    group_id = int(config.get("target_group_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    if group is None or group.tenant_id != task.tenant_id:
        raise ValueError("task_target_scope_group_missing")
    return _canonical_peer(group.tg_peer_id)


def _operation_target(
    session: Session,
    task: Task,
    target_id: int,
    target_kind: str,
) -> OperationTarget:
    target = session.get(OperationTarget, target_id) if target_id else None
    if (
        target is None
        or target.tenant_id != task.tenant_id
        or target.target_type != target_kind
    ):
        raise ValueError(f"task_target_scope_{target_kind}_missing")
    return target


def _canonical_peer(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("task_target_scope_peer_missing")
    return normalized


__all__ = [
    "TaskTargetScopeConflict",
    "active_unified_group_ai_owner",
    "ensure_task_target_scope_claims",
    "has_current_task_target_scope_claim",
    "release_task_target_scope_claims",
    "task_target_scopes",
]
