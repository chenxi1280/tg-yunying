from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TgAccountSecurityBatch


TASK_INDEX_CONFIG_KEYS = (
    "associated_channels",
    "channel_title",
    "discussion_group_id",
    "discussion_group_name",
    "linked_channel_name",
    "linked_channels",
    "linked_group_id",
    "linked_group_name",
    "message_ids",
    "required_channel_name",
    "required_channel_refs",
    "required_channels",
    "source_groups",
    "target_channel_id",
    "target_channel_name",
    "target_group_id",
    "target_group_ids",
    "target_group_name",
    "target_operation_target_id",
    "target_operation_target_ids",
    "target_title",
)


@dataclass(frozen=True)
class TaskIndexProjection:
    id: str
    tenant_id: int
    name: str
    type: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_error: str
    type_config: dict[str, Any]


@dataclass(frozen=True)
class ProfileBatchIndexProjection:
    id: int
    tenant_id: int
    action_types: str
    status: str
    total_count: int
    reason: str
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def list_task_index_projections(session: Session, tenant_id: int) -> list[TaskIndexProjection]:
    config_columns = [Task.type_config[key].label(f"config_{key}") for key in TASK_INDEX_CONFIG_KEYS]
    statement = select(
        Task.id,
        Task.tenant_id,
        Task.name,
        Task.type,
        Task.status,
        Task.priority,
        Task.created_at,
        Task.updated_at,
        Task.next_run_at,
        Task.last_error,
        *config_columns,
    ).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    return [_task_projection(row._mapping) for row in session.execute(statement)]


def list_profile_batch_index_projections(
    session: Session,
    tenant_id: int,
    deleted_status: str,
) -> list[ProfileBatchIndexProjection]:
    statement = select(
        TgAccountSecurityBatch.id,
        TgAccountSecurityBatch.tenant_id,
        TgAccountSecurityBatch.action_types,
        TgAccountSecurityBatch.status,
        TgAccountSecurityBatch.total_count,
        TgAccountSecurityBatch.reason,
        TgAccountSecurityBatch.trace_id,
        TgAccountSecurityBatch.created_at,
        TgAccountSecurityBatch.started_at,
        TgAccountSecurityBatch.finished_at,
    ).where(
        TgAccountSecurityBatch.tenant_id == tenant_id,
        TgAccountSecurityBatch.status != deleted_status,
    )
    return [ProfileBatchIndexProjection(**dict(row._mapping)) for row in session.execute(statement)]


def _task_projection(values: dict[str, Any]) -> TaskIndexProjection:
    config = {
        key: values[f"config_{key}"]
        for key in TASK_INDEX_CONFIG_KEYS
        if values[f"config_{key}"] is not None
    }
    return TaskIndexProjection(
        id=values["id"],
        tenant_id=values["tenant_id"],
        name=values["name"],
        type=values["type"],
        status=values["status"],
        priority=values["priority"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        next_run_at=values["next_run_at"],
        last_error=values["last_error"],
        type_config=config,
    )


__all__ = [
    "ProfileBatchIndexProjection",
    "TaskIndexProjection",
    "list_profile_batch_index_projections",
    "list_task_index_projections",
]
