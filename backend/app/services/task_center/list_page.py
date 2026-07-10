from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import Task, TaskRuntimeSummary, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem
from app.services._common import normalize_list_filter
from app.services.task_runtime_stage import derive_task_runtime_stage

from .details import build_task_list_payload_context
from .profile_batch_projection import (
    DELETED_PROFILE_BATCH_STATUS,
    PROFILE_BATCH_TASK_PREFIX,
    TASK_TYPE_LABELS,
    TASK_TYPE_SUMMARIES,
    _projection_status,
    _task_type_for_batch,
)


UNKNOWN_TARGET_GROUP = "未关联群聊"
UNKNOWN_CHANNEL = "未关联频道"


@dataclass(frozen=True)
class TaskListIndexRow:
    id: str
    tenant_id: int
    source_kind: str
    stable_id: int | str
    name: str
    task_type: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_error: str
    target_summary: str
    account_scope_summary: str
    target_group_label: str
    associated_channel_label: str
    group_key: str
    search_text: str
    stats: dict[str, Any]
    task: Task | None = None


@dataclass(frozen=True)
class TaskListPageResult:
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    summary: dict[str, int]
    groups: list[dict[str, Any]]


def list_task_page(
    session: Session,
    *,
    tenant_id: int,
    page: int,
    page_size: int,
    task_type: str | None,
    status: str | None,
    q: str,
    group_key: str | None,
) -> TaskListPageResult:
    _validate_page(page, page_size)
    normalized_type = normalize_list_filter(task_type)
    normalized_status = normalize_list_filter(status)
    ordinary_rows = _ordinary_index_rows(session, tenant_id)
    batch_rows = _profile_batch_index_rows(session, tenant_id)
    batch_q_matches = _batch_query_matches(session, tenant_id, q)
    base_rows = _filter_base_rows(
        [*ordinary_rows, *batch_rows],
        normalized_type,
        normalized_status,
        q,
        batch_q_matches,
    )
    sorted_base = _stable_sort(base_rows)
    summary = _summary(sorted_base)
    groups = _groups(sorted_base)
    grouped_rows = _filter_group(sorted_base, group_key)
    page_rows = _slice_page(grouped_rows, page, page_size)
    return TaskListPageResult(
        items=_hydrate_page_items(session, page_rows),
        total=len(grouped_rows),
        page=page,
        page_size=page_size,
        summary=summary,
        groups=groups,
    )


def _validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise ValueError("page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")


def _ordinary_index_rows(session: Session, tenant_id: int) -> list[TaskListIndexRow]:
    tasks = list(
        session.scalars(
            select(Task)
            .where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
        )
    )
    if not tasks:
        return []
    context = build_task_list_payload_context(session, tasks)
    return [_ordinary_index(task, context.target_summary_by_task_id.get(task.id, ""), context.config_search_text_by_task_id.get(task.id, "")) for task in tasks]


def _ordinary_index(task: Task, target_summary: str, config_search_text: str) -> TaskListIndexRow:
    target_group = _target_group_label(task, target_summary)
    channel = _associated_channel_label(task, target_summary)
    search_parts = [task.id, task.name, task.type, task.status, task.last_error, target_summary, config_search_text, _config_text(task.type_config)]
    return TaskListIndexRow(
        id=task.id,
        tenant_id=task.tenant_id,
        source_kind="task",
        stable_id=task.id,
        name=task.name,
        task_type=task.type,
        status=task.status,
        priority=task.priority,
        created_at=task.created_at,
        updated_at=task.updated_at,
        next_run_at=task.next_run_at,
        last_error=task.last_error,
        target_summary=target_summary,
        account_scope_summary=_account_scope_summary(task.account_config),
        target_group_label=target_group,
        associated_channel_label=channel,
        group_key=_group_key(task, target_group, channel),
        search_text=" ".join(str(part) for part in search_parts if part),
        stats=dict(task.stats or {}),
        task=task,
    )


def _profile_batch_index_rows(session: Session, tenant_id: int) -> list[TaskListIndexRow]:
    batches = list(
        session.scalars(
            select(TgAccountSecurityBatch)
            .where(
                TgAccountSecurityBatch.tenant_id == tenant_id,
                TgAccountSecurityBatch.status != DELETED_PROFILE_BATCH_STATUS,
            )
            .order_by(TgAccountSecurityBatch.id.asc())
        )
    )
    stats_by_id = _profile_batch_stats(session, [batch.id for batch in batches])
    return [_profile_batch_index(batch, stats_by_id.get(batch.id, {})) for batch in batches]


def _profile_batch_index(batch: TgAccountSecurityBatch, aggregate: dict[str, Any]) -> TaskListIndexRow:
    task_type = _task_type_for_batch(batch)
    stats = _profile_batch_stats_payload(batch, aggregate)
    target_summary = f"{TASK_TYPE_SUMMARIES[task_type]} / {batch.total_count} 个账号"
    group_label = "账号安全系统任务"
    channel_label = UNKNOWN_CHANNEL
    return TaskListIndexRow(
        id=f"{PROFILE_BATCH_TASK_PREFIX}{batch.id}",
        tenant_id=batch.tenant_id,
        source_kind="account_security_batch",
        stable_id=batch.id,
        name=f"{TASK_TYPE_LABELS[task_type]} #{batch.id}",
        task_type=task_type,
        status=_projection_status(batch.status),
        priority=3,
        created_at=batch.created_at,
        updated_at=batch.finished_at or batch.started_at or batch.created_at,
        next_run_at=None,
        last_error=str(stats.get("latest_failure_type") or ""),
        target_summary=target_summary,
        account_scope_summary=f"{batch.total_count} 个账号",
        target_group_label=group_label,
        associated_channel_label=channel_label,
        group_key=_label_group_key(group_label, channel_label),
        search_text=" ".join(str(part) for part in [batch.id, batch.reason, batch.trace_id, batch.status, target_summary] if part),
        stats=stats,
    )


def _profile_batch_stats(session: Session, batch_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not batch_ids:
        return {}
    item = TgAccountSecurityBatchItem
    latest_item = aliased(TgAccountSecurityBatchItem)
    latest_failure = (
        select(latest_item.failure_type)
        .where(latest_item.batch_id == TgAccountSecurityBatch.id, latest_item.failure_type != "")
        .order_by(latest_item.id.desc())
        .limit(1)
        .correlate(TgAccountSecurityBatch)
        .scalar_subquery()
    )
    statement = _profile_batch_stats_statement(item, latest_failure, batch_ids)
    return {int(row.batch_id): dict(row._mapping) for row in session.execute(statement)}


def _profile_batch_stats_statement(item, latest_failure, batch_ids: list[int]):
    return (
        select(
            TgAccountSecurityBatch.id.label("batch_id"),
            func.count(item.id).label("total_actions"),
            func.count(item.id).filter(item.status == "succeeded").label("success_count"),
            func.count(item.id).filter(item.status.in_(["failed", "partial_success"])).label("failure_count"),
            func.count(item.id).filter(item.status.in_(["skipped", "manual_required"])).label("skipped_count"),
            func.count(item.id).filter(item.status == "manual_required").label("manual_required_count"),
            func.count(item.id).filter(item.status == "pending").label("pending_count"),
            func.count(item.id).filter(item.avatar_status == "waiting_cache").label("waiting_cache_count"),
            func.count(item.id).filter(item.status == "running").label("running_count"),
            latest_failure.label("latest_failure_type"),
        )
        .outerjoin(item, item.batch_id == TgAccountSecurityBatch.id)
        .where(TgAccountSecurityBatch.id.in_(batch_ids))
        .group_by(TgAccountSecurityBatch.id)
    )


def _profile_batch_stats_payload(batch: TgAccountSecurityBatch, aggregate: dict[str, Any]) -> dict[str, Any]:
    total_actions = int(aggregate.get("total_actions") or 0)
    return {
        "total_actions": total_actions or batch.total_count,
        "success_count": int(aggregate.get("success_count") or 0),
        "failure_count": int(aggregate.get("failure_count") or 0),
        "skipped_count": int(aggregate.get("skipped_count") or 0),
        "manual_required_count": int(aggregate.get("manual_required_count") or 0),
        "pending_count": int(aggregate.get("pending_count") or 0),
        "waiting_cache_count": int(aggregate.get("waiting_cache_count") or 0),
        "running_count": int(aggregate.get("running_count") or 0),
        "batch_status": batch.status,
        "latest_failure_type": str(aggregate.get("latest_failure_type") or ""),
    }


def _batch_query_matches(session: Session, tenant_id: int, q: str) -> set[int]:
    normalized = q.strip().lower()
    if not normalized:
        return set()
    item = TgAccountSecurityBatchItem
    account = TgAccount
    fields = [item.failure_type, item.failure_detail, item.avatar_source, account.display_name, account.username, account.phone_masked]
    match = or_(*(func.lower(func.coalesce(field, "")).contains(normalized) for field in fields))
    rows = session.scalars(
        select(item.batch_id)
        .outerjoin(account, account.id == item.account_id)
        .where(item.tenant_id == tenant_id, match)
        .distinct()
    )
    return {int(batch_id) for batch_id in rows}


def _filter_base_rows(
    rows: list[TaskListIndexRow],
    task_type: str | None,
    status: str | None,
    q: str,
    batch_q_matches: set[int],
) -> list[TaskListIndexRow]:
    normalized_q = q.strip().casefold()
    result: list[TaskListIndexRow] = []
    for row in rows:
        if task_type and row.task_type != task_type:
            continue
        if status and row.status != status:
            continue
        batch_match = isinstance(row.stable_id, int) and row.stable_id in batch_q_matches
        if normalized_q and normalized_q not in row.search_text.casefold() and not batch_match:
            continue
        result.append(row)
    return result


def _stable_sort(rows: list[TaskListIndexRow]) -> list[TaskListIndexRow]:
    ordered = sorted(rows, key=_stable_id_key, reverse=True)
    ordered = sorted(ordered, key=lambda row: 0 if row.source_kind == "task" else 1)
    ordered = sorted(ordered, key=lambda row: _utc_datetime(row.created_at), reverse=True)
    return sorted(ordered, key=lambda row: row.priority)


def _stable_id_key(row: TaskListIndexRow) -> tuple[int, int, str]:
    if isinstance(row.stable_id, int):
        return (1, row.stable_id, "")
    return (0, 0, row.stable_id)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _summary(rows: list[TaskListIndexRow]) -> dict[str, int]:
    return {
        "total": len(rows),
        "running": sum(row.status == "running" for row in rows),
        "failed": sum(row.status == "failed" for row in rows),
    }


def _groups(rows: list[TaskListIndexRow]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            row.group_key,
            {
                "key": row.group_key,
                "target_group_label": row.target_group_label,
                "associated_channel_label": row.associated_channel_label,
                "task_count": 0,
                "running_count": 0,
                "failed_count": 0,
            },
        )
        group["task_count"] += 1
        group["running_count"] += int(row.status == "running")
        group["failed_count"] += int(row.status == "failed")
    return list(groups.values())


def _filter_group(rows: list[TaskListIndexRow], group_key: str | None) -> list[TaskListIndexRow]:
    normalized = normalize_list_filter(group_key)
    if not normalized:
        return rows
    return [row for row in rows if row.group_key == normalized]


def _slice_page(rows: list[TaskListIndexRow], page: int, page_size: int) -> list[TaskListIndexRow]:
    offset = (page - 1) * page_size
    return rows[offset : offset + page_size]


def _hydrate_page_items(session: Session, rows: list[TaskListIndexRow]) -> list[dict[str, Any]]:
    task_ids = [row.id for row in rows if row.task]
    summaries = _runtime_summaries(session, task_ids)
    return [_list_item_payload(row, summaries.get(row.id)) for row in rows]


def _runtime_summaries(session: Session, task_ids: list[str]) -> dict[str, TaskRuntimeSummary]:
    if not task_ids:
        return {}
    rows = session.scalars(select(TaskRuntimeSummary).where(TaskRuntimeSummary.task_id.in_(task_ids)))
    return {row.task_id: row for row in rows}


def _list_item_payload(row: TaskListIndexRow, summary: TaskRuntimeSummary | None) -> dict[str, Any]:
    stats = _ordinary_stats(row.stats, summary) if row.task else row.stats
    runtime_stage = derive_task_runtime_stage(row.task, summary=summary) if row.task else {}
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "source_kind": row.source_kind,
        "name": row.name,
        "type": row.task_type,
        "status": row.status,
        "priority": row.priority,
        "next_run_at": row.next_run_at,
        "last_error": row.last_error,
        "stats": stats,
        "runtime_stage": runtime_stage,
        "target_summary": row.target_summary,
        "account_scope_summary": row.account_scope_summary,
        "target_group_label": row.target_group_label,
        "associated_channel_label": row.associated_channel_label,
        "group_key": row.group_key,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _ordinary_stats(stats: dict[str, Any], summary: TaskRuntimeSummary | None) -> dict[str, Any]:
    result = dict(stats)
    if not summary:
        return result
    result.update(
        {
            "total_actions": summary.planned_count,
            "success_count": summary.success_count,
            "failure_count": summary.failed_count,
            "pending_count": summary.pending_count,
            "oldest_pending_at": summary.oldest_pending_at,
            "latest_failure_type": summary.latest_failure_type,
            "runtime_summary_updated_at": summary.updated_at,
        }
    )
    return result


def _target_group_label(task: Task, target_summary: str) -> str:
    config = task.type_config or {}
    if task.type in {"group_ai_chat", "group_relay"}:
        return _first_text([target_summary, config.get("target_group_name"), config.get("target_group_id")]) or UNKNOWN_TARGET_GROUP
    if task.type == "search_join_group":
        return _first_text([target_summary, config.get("target_title"), config.get("target_group_name"), config.get("target_group_id")]) or UNKNOWN_TARGET_GROUP
    if task.type == "search_rank_deboost":
        return _first_text([target_summary, config.get("target_group_name"), config.get("target_group_ids")]) or UNKNOWN_TARGET_GROUP
    return _first_text([config.get("linked_group_name"), config.get("discussion_group_name"), config.get("target_group_name"), config.get("linked_group_id"), config.get("discussion_group_id")]) or UNKNOWN_TARGET_GROUP


def _associated_channel_label(task: Task, target_summary: str) -> str:
    config = task.type_config or {}
    if task.type.startswith("channel_"):
        return _first_text([target_summary, config.get("target_channel_name"), config.get("channel_title"), config.get("target_channel_id")]) or UNKNOWN_CHANNEL
    channels = [
        *_text_list(config.get("required_channels")),
        *_text_list(config.get("required_channel_refs")),
        *_text_list(config.get("linked_channels")),
        *_text_list(config.get("associated_channels")),
        _first_text([config.get("linked_channel_name"), config.get("required_channel_name"), config.get("target_channel_name")]),
    ]
    return "、".join(dict.fromkeys(item for item in channels if item)) or UNKNOWN_CHANNEL


def _group_key(task: Task, target_label: str, channel_label: str) -> str:
    config = task.type_config or {}
    target_key = _key_part([config.get("target_operation_target_id"), config.get("target_group_id"), config.get("linked_group_id"), config.get("discussion_group_id"), target_label])
    channel_key = _key_part([config.get("target_channel_id"), channel_label])
    return f"task-group:{target_key}:{channel_key}"


def _label_group_key(target_label: str, channel_label: str) -> str:
    return f"task-group:{_key_part([target_label])}:{_key_part([channel_label])}"


def _key_part(values: list[Any]) -> str:
    return re.sub(r"\s+", "-", _first_text(values).lower())


def _first_text(values: list[Any]) -> str:
    return next((text for text in (_primitive_text(value) for value in values) if text), "")


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for text in (_primitive_text(item) for item in value) if text]
    text = _primitive_text(value)
    return [text] if text else []


def _primitive_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return _first_text([value.get("title"), value.get("name"), value.get("username"), value.get("label"), value.get("id")])
    if isinstance(value, list):
        return "、".join(_text_list(value))
    return ""


def _config_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_config_text(item) for item in value.values() if item is not None)
    if isinstance(value, list):
        return " ".join(_config_text(item) for item in value)
    return str(value or "")


def _account_scope_summary(config: dict[str, Any] | None) -> str:
    values = config or {}
    account_ids = values.get("account_ids") or []
    if account_ids:
        return f"{len(account_ids)} 个账号"
    pool_ids = values.get("account_pool_ids") or values.get("pool_ids") or []
    if pool_ids:
        return f"{len(pool_ids)} 个账号池"
    return str(values.get("selection_mode") or "")


__all__ = ["TaskListIndexRow", "TaskListPageResult", "list_task_page"]
