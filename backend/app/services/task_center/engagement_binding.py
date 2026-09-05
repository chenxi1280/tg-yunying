from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupMembershipSnapshotSet,
    AccountPool,
    Task,
    TaskAccountGroupBindingSetRevision,
)
from app.services._common import _now
from app.timezone import BEIJING_TZ
from .engagement_membership_snapshot import capture_membership_groups


ENGAGEMENT_TASK_TYPES = frozenset(
    {"group_ai_chat", "channel_comment", "channel_like", "channel_view"}
)
NORMAL_POOL_PURPOSE = "normal"
DEFAULT_GROUP_CONCURRENCY_LIMIT = 5
UNIFIED_ENGAGEMENT_CONTRACT_VERSION = "unified_engagement_v1"
UNIFIED_ENGAGEMENT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class EngagementBindingSpec:
    group_ids: tuple[int, ...]
    group_contracts: tuple[dict, ...]
    concurrency_limit_per_group: int
    binding_set_hash: str


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_group_ids(config: dict) -> tuple[int, ...]:
    if str(config.get("account_selection_mode") or "") != "group":
        raise ValueError("unified_engagement_requires_account_group_binding")
    raw_ids = config.get("account_group_ids") or []
    group_ids = tuple(int(item) for item in raw_ids if int(item) > 0)
    if not group_ids:
        raise ValueError("account_group_ids 至少选择一个账号分组")
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("account_group_ids 不得重复")
    return tuple(sorted(group_ids))


def _normal_pool_contract(pool: AccountPool) -> dict:
    purpose = str(pool.pool_purpose or NORMAL_POOL_PURPOSE)
    system_key = str(pool.system_key or "")
    if not pool.is_enabled:
        raise ValueError(f"account_group_disabled:{pool.id}")
    if purpose != NORMAL_POOL_PURPOSE or system_key not in {"", NORMAL_POOL_PURPOSE}:
        raise ValueError(f"account_group_purpose_mismatch:{pool.id}")
    return {
        "group_id": int(pool.id),
        "pool_purpose": purpose,
        "system_key": system_key,
        "is_system": bool(pool.is_system),
    }


def _load_pools(
    session: Session, tenant_id: int, group_ids: tuple[int, ...]
) -> list[AccountPool]:
    pools = list(
        session.scalars(
            select(AccountPool)
            .where(
                AccountPool.tenant_id == tenant_id,
                AccountPool.id.in_(group_ids),
            )
            .order_by(AccountPool.id.asc())
        )
    )
    if tuple(pool.id for pool in pools) != group_ids:
        raise ValueError("account_group_not_found_or_cross_tenant")
    return pools


def validate_engagement_binding(
    session: Session, tenant_id: int, task_type: str, config: dict
) -> EngagementBindingSpec | None:
    if task_type not in ENGAGEMENT_TASK_TYPES:
        return None
    if config.get("engagement_contract_version") != UNIFIED_ENGAGEMENT_CONTRACT_VERSION:
        return None
    group_ids = _canonical_group_ids(config)
    limit = int(
        config.get("concurrency_limit_per_group")
        or DEFAULT_GROUP_CONCURRENCY_LIMIT
    )
    if limit < 1 or limit > 50:
        raise ValueError("concurrency_limit_per_group 必须在 1-50 之间")
    contracts = tuple(_normal_pool_contract(pool) for pool in _load_pools(session, tenant_id, group_ids))
    binding_hash = _stable_hash({"groups": contracts, "limit": limit})
    return EngagementBindingSpec(
        group_ids=group_ids,
        group_contracts=contracts,
        concurrency_limit_per_group=limit,
        binding_set_hash=binding_hash,
    )


def validate_engagement_timezone(
    task_type: str, config: dict, timezone_name: str
) -> None:
    if task_type not in ENGAGEMENT_TASK_TYPES:
        return
    if config.get("engagement_contract_version") != UNIFIED_ENGAGEMENT_CONTRACT_VERSION:
        return
    if timezone_name != UNIFIED_ENGAGEMENT_TIMEZONE:
        raise ValueError("unified_engagement_timezone_must_be_Asia/Shanghai")


def projected_account_config(base: dict, spec: EngagementBindingSpec | None) -> dict:
    if spec is None:
        return dict(base or {})
    projected = dict(base or {})
    projected.update(
        {
            "selection_mode": "group",
            "account_group_id": spec.group_ids[0] if len(spec.group_ids) == 1 else None,
            "account_group_ids": list(spec.group_ids),
            "max_concurrent": spec.concurrency_limit_per_group,
        }
    )
    projected.pop("account_ids", None)
    return projected


def freeze_initial_binding(
    session: Session, task: Task, spec: EngagementBindingSpec | None
) -> TaskAccountGroupBindingSetRevision | None:
    if spec is None:
        return None
    _lock_binding_spec(session, task.tenant_id, spec)
    _ensure_runtime_policies(session, task.tenant_id, spec)
    revision = TaskAccountGroupBindingSetRevision(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        binding_set_revision=1,
        account_group_ids=list(spec.group_ids),
        concurrency_limit_per_group=spec.concurrency_limit_per_group,
        group_contracts=list(spec.group_contracts),
        binding_set_hash=spec.binding_set_hash,
        effective_from=_now(),
    )
    session.add(revision)
    return revision


def replace_active_binding(
    session: Session,
    task: Task,
    spec: EngagementBindingSpec,
    *,
    effective_from: datetime,
) -> TaskAccountGroupBindingSetRevision:
    _lock_binding_spec(session, task.tenant_id, spec)
    current = session.scalar(
        select(TaskAccountGroupBindingSetRevision)
        .where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active",
        )
        .with_for_update()
    )
    if current is not None and current.binding_set_hash == spec.binding_set_hash:
        return current
    if current is not None:
        current.state = "superseded"
        current.effective_to = effective_from
    successor = _binding_successor(task, spec, current, effective_from)
    session.add(successor)
    return successor


def synchronize_task_binding(
    session: Session, task: Task
) -> TaskAccountGroupBindingSetRevision | None:
    spec = validate_engagement_binding(
        session, task.tenant_id, task.type, task.type_config or {}
    )
    if spec is None:
        return None
    _lock_binding_spec(session, task.tenant_id, spec)
    _ensure_runtime_policies(session, task.tenant_id, spec)
    current = session.scalar(
        select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active",
        )
    )
    if current is not None and current.binding_set_hash != spec.binding_set_hash:
        if task.status not in {"draft", "pending"}:
            successor = _schedule_binding_successor(session, task, spec, current)
            _restore_current_binding_config(task, current)
            return successor
    task.account_config = projected_account_config(task.account_config or {}, spec)
    return replace_active_binding(session, task, spec, effective_from=_now())


def _binding_successor(
    task: Task,
    spec: EngagementBindingSpec,
    current: TaskAccountGroupBindingSetRevision | None,
    effective_from: datetime,
    *,
    revision: int | None = None,
    state: str = "active",
) -> TaskAccountGroupBindingSetRevision:
    return TaskAccountGroupBindingSetRevision(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        binding_set_revision=revision or ((current.binding_set_revision + 1) if current else 1),
        account_group_ids=list(spec.group_ids),
        concurrency_limit_per_group=spec.concurrency_limit_per_group,
        group_contracts=list(spec.group_contracts),
        binding_set_hash=spec.binding_set_hash,
        effective_from=effective_from,
        state=state,
        supersedes_revision_id=current.id if current else None,
    )


def activate_due_binding(
    session: Session, task: Task, *, period_start: datetime
) -> TaskAccountGroupBindingSetRevision | None:
    due = session.scalar(
        select(TaskAccountGroupBindingSetRevision)
        .where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "scheduled",
            TaskAccountGroupBindingSetRevision.effective_from <= period_start,
        )
        .order_by(TaskAccountGroupBindingSetRevision.binding_set_revision.desc())
        .with_for_update()
    )
    if due is None:
        return None
    _lock_binding_spec(session, task.tenant_id, _spec_from_binding(due))
    current = session.scalar(
        select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active",
        )
    )
    if current is not None:
        current.state = "superseded"
        current.effective_to = period_start
    due.state = "active"
    _apply_binding_config(task, due)
    return due


def _schedule_binding_successor(
    session: Session,
    task: Task,
    spec: EngagementBindingSpec,
    current: TaskAccountGroupBindingSetRevision,
) -> TaskAccountGroupBindingSetRevision:
    for row in session.scalars(
        select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "scheduled",
        )
    ):
        row.state = "superseded"
        row.effective_to = _now()
    successor = _binding_successor(
        task,
        spec,
        current,
        _next_beijing_day_start(_now()),
        revision=_next_binding_revision(session, task.id),
        state="scheduled",
    )
    session.add(successor)
    return successor


def _next_binding_revision(session: Session, task_id: str) -> int:
    revisions = session.scalars(
        select(TaskAccountGroupBindingSetRevision.binding_set_revision).where(
            TaskAccountGroupBindingSetRevision.task_id == task_id
        )
    )
    return max((int(item) for item in revisions), default=0) + 1


def _next_beijing_day_start(now_value: datetime) -> datetime:
    aware = (
        now_value.replace(tzinfo=BEIJING_TZ)
        if now_value.tzinfo is None
        else now_value.astimezone(BEIJING_TZ)
    )
    next_date = aware.date() + timedelta(days=1)
    return datetime.combine(next_date, time.min, BEIJING_TZ).astimezone(timezone.utc)


def _restore_current_binding_config(
    task: Task, current: TaskAccountGroupBindingSetRevision
) -> None:
    _apply_binding_config(task, current)


def _apply_binding_config(
    task: Task, binding: TaskAccountGroupBindingSetRevision
) -> None:
    config = dict(task.type_config or {})
    config.update(
        {
            "account_selection_mode": "group",
            "account_group_ids": list(binding.account_group_ids),
            "concurrency_limit_per_group": binding.concurrency_limit_per_group,
        }
    )
    task.type_config = config
    task.account_config = projected_account_config(
        task.account_config or {},
        _spec_from_binding(binding),
    )


def _spec_from_binding(binding):
    return EngagementBindingSpec(
        group_ids=tuple(binding.account_group_ids),
        group_contracts=tuple(binding.group_contracts),
        concurrency_limit_per_group=binding.concurrency_limit_per_group,
        binding_set_hash=binding.binding_set_hash,
    )


def validate_task_membership_foundation(session, task):
    config = task.type_config or {}
    if task.type not in ENGAGEMENT_TASK_TYPES:
        return
    if config.get("engagement_contract_version") != UNIFIED_ENGAGEMENT_CONTRACT_VERSION:
        return
    binding = _active_binding(session, task)
    capture_membership_groups(session, task.tenant_id, tuple(binding.account_group_ids))


def freeze_membership_snapshot(
    session: Session,
    task: Task,
    *,
    participation_unit: str,
) -> AccountGroupMembershipSnapshotSet:
    existing = session.scalar(
        select(AccountGroupMembershipSnapshotSet).where(
            AccountGroupMembershipSnapshotSet.task_id == task.id,
            AccountGroupMembershipSnapshotSet.task_lifecycle_epoch
            == task.task_lifecycle_epoch,
            AccountGroupMembershipSnapshotSet.participation_unit
            == participation_unit,
        )
    )
    if existing is not None:
        return existing
    binding = _active_binding(session, task)
    group_ids = tuple(int(item) for item in binding.account_group_ids)
    memberships = capture_membership_groups(session, task.tenant_id, group_ids)
    rows = sorted((account_id, group["group_id"]) for group in memberships
        for account_id in group["member_account_ids"])
    members = tuple(account_id for account_id, _ in rows)
    origins = {str(account_id): group_id for account_id, group_id in rows}
    snapshot = AccountGroupMembershipSnapshotSet(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        binding_set_revision_id=binding.id,
        participation_unit=participation_unit,
        group_memberships=memberships,
        member_account_ids=list(members),
        account_origin_groups=origins,
        member_union_hash=_stable_hash(
            {"groups": memberships, "members": members, "origins": origins}
        ),
    )
    session.add(snapshot)
    return snapshot


def _active_binding(
    session: Session, task: Task
) -> TaskAccountGroupBindingSetRevision:
    binding = session.scalar(
        select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.tenant_id == task.tenant_id,
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active",
        )
    )
    if binding is None:
        raise ValueError("engagement_binding_missing")
    return binding


def _ensure_runtime_policies(
    session: Session, tenant_id: int, spec: EngagementBindingSpec
) -> None:
    from .engagement_runtime_policy import ensure_engagement_runtime_policies

    ensure_engagement_runtime_policies(
        session,
        tenant_id=tenant_id,
        binding=spec,
    )


def _lock_binding_spec(session, tenant_id, spec):
    groups = capture_membership_groups(session, tenant_id, spec.group_ids)
    contracts = tuple(_binding_contract_from_snapshot(group) for group in groups)
    if _stable_hash({"groups": contracts, "limit": spec.concurrency_limit_per_group}) != spec.binding_set_hash:
        raise ValueError("engagement_binding_group_state_changed")


def _binding_contract_from_snapshot(group):
    state = group["group_state"]
    if not state["is_enabled"]:
        raise ValueError(f"account_group_disabled:{group['group_id']}")
    return {"group_id": group["group_id"],
        "pool_purpose": str(state["pool_purpose"] or NORMAL_POOL_PURPOSE),
        "system_key": str(state["system_key"] or ""), "is_system": bool(state["is_system"])}


__all__ = [
    "ENGAGEMENT_TASK_TYPES",
    "EngagementBindingSpec",
    "UNIFIED_ENGAGEMENT_CONTRACT_VERSION",
    "UNIFIED_ENGAGEMENT_TIMEZONE",
    "activate_due_binding",
    "freeze_initial_binding",
    "freeze_membership_snapshot",
    "projected_account_config",
    "replace_active_binding",
    "synchronize_task_binding",
    "validate_engagement_binding",
    "validate_engagement_timezone",
    "validate_task_membership_foundation",
]
