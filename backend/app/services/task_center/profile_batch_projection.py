from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Material, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem
from app.services._common import _now, audit

PROFILE_BATCH_TASK_PREFIX = "account_security_batch:"
PROFILE_BATCH_TASK_TYPE = "account_profile_init"
DELETED_PROFILE_BATCH_STATUS = "deleted"
OPEN_ITEM_STATUSES = {"pending", "running", "waiting"}
OPEN_STEP_STATUSES = {"pending", "running", "waiting", "waiting_cache"}


def is_profile_batch_task_id(task_id: str) -> bool:
    return task_id.startswith(PROFILE_BATCH_TASK_PREFIX) and task_id.removeprefix(PROFILE_BATCH_TASK_PREFIX).isdigit()


def list_profile_batch_tasks(session: Session, tenant_id: int, task_type: str | None, status: str | None) -> list[dict[str, Any]]:
    if task_type and task_type != PROFILE_BATCH_TASK_TYPE:
        return []
    batches = list(
        session.scalars(
            select(TgAccountSecurityBatch)
            .where(TgAccountSecurityBatch.tenant_id == tenant_id)
            .where(TgAccountSecurityBatch.status != DELETED_PROFILE_BATCH_STATUS)
            .order_by(TgAccountSecurityBatch.id.desc())
            .limit(50)
        )
    )
    rows = [_projection_payload(session, batch) for batch in batches if _is_profile_batch(batch)]
    return [row for row in rows if not status or row["status"] == status]


def get_profile_batch_task_detail(session: Session, tenant_id: int, task_id: str) -> dict[str, Any]:
    batch_id = int(task_id.removeprefix(PROFILE_BATCH_TASK_PREFIX))
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id or not _is_profile_batch(batch) or batch.status == DELETED_PROFILE_BATCH_STATUS:
        raise ValueError("task not found")
    payload = _projection_payload(session, batch)
    profile_batch = _profile_batch_detail(session, batch)
    return {
        "task": payload,
        "actions": [],
        "stats": payload["stats"],
        "task_runtime_summary": None,
        "operation_plan_links": [],
        "accounts": [],
        "membership_phase": {},
        "membership_accounts": [],
        "message_groups": [],
        "ai_cycles": [],
        "ai_generation_records": [],
        "ai_account_profiles": [],
        "relay_batches": [],
        "recent_relay_sources": [],
        "profile_batch": profile_batch,
    }


def delete_profile_batch_task(session: Session, tenant_id: int, task_id: str, *, actor: str, reason: str = "") -> None:
    batch_id = int(task_id.removeprefix(PROFILE_BATCH_TASK_PREFIX))
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id or not _is_profile_batch(batch) or batch.status == DELETED_PROFILE_BATCH_STATUS:
        raise ValueError("task not found")
    now = _now()
    reason_text = reason.strip() or "任务已删除"
    items = _batch_items(session, batch.id)
    for item in items:
        _skip_open_profile_batch_item(item, reason_text, now)
    batch.status = DELETED_PROFILE_BATCH_STATUS
    batch.skipped_count = sum(1 for item in items if item.status in {"skipped", "manual_required"})
    batch.finished_at = now
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="删除资料初始化批次",
        target_type="account_security_batch",
        target_id=str(batch.id),
        detail=reason_text,
    )
    session.commit()


def _skip_open_profile_batch_item(item: TgAccountSecurityBatchItem, reason: str, finished_at: datetime) -> None:
    if item.status in OPEN_ITEM_STATUSES:
        item.status = "skipped"
        item.skipped_reason = reason
        item.finished_at = finished_at
    for field in ["precheck_status", "cleanup_status", "two_fa_status", "profile_status", "username_status", "avatar_status"]:
        if getattr(item, field) in OPEN_STEP_STATUSES:
            setattr(item, field, "skipped")


def _projection_payload(session: Session, batch: TgAccountSecurityBatch) -> dict[str, Any]:
    items = _batch_items(session, batch.id)
    stats = _projection_stats(batch, items)
    return {
        "id": f"{PROFILE_BATCH_TASK_PREFIX}{batch.id}",
        "tenant_id": batch.tenant_id,
        "name": f"资料初始化批次 #{batch.id}",
        "type": PROFILE_BATCH_TASK_TYPE,
        "status": _projection_status(batch.status),
        "priority": 3,
        "timezone": "Asia/Shanghai",
        "scheduled_start": None,
        "scheduled_end": None,
        "max_duration_hours": None,
        "next_run_at": None,
        "last_error": stats.get("latest_failure_type", ""),
        "account_config": {},
        "pacing_config": {},
        "failure_policy": {},
        "type_config": {"source": "account_security_batch", "batch_id": batch.id},
        "stats": stats,
        "target_summary": f"账号资料初始化 / {batch.total_count} 个账号",
        "search_text": _projection_search_text(session, batch, items),
        "created_at": batch.created_at,
        "updated_at": batch.finished_at or batch.started_at or batch.created_at,
    }


def _profile_batch_detail(session: Session, batch: TgAccountSecurityBatch) -> dict[str, Any]:
    items = _batch_items(session, batch.id)
    return {
        "batch_id": batch.id,
        "action_types": _json_list(batch.action_types),
        "batch_status": batch.status,
        "avatar_cache": _avatar_cache_summary(session, items),
        "items": [_profile_batch_item(session, item) for item in items],
    }


def _projection_stats(batch: TgAccountSecurityBatch, items: list[TgAccountSecurityBatchItem]) -> dict[str, Any]:
    latest_failure = next((item.failure_type for item in reversed(items) if item.failure_type), "")
    return {
        "total_actions": len(items),
        "success_count": sum(1 for item in items if item.status == "succeeded"),
        "failure_count": sum(1 for item in items if item.status in {"failed", "partial_success"}),
        "skipped_count": sum(1 for item in items if item.status in {"skipped", "manual_required"}),
        "pending_count": sum(1 for item in items if item.status == "pending"),
        "waiting_cache_count": sum(1 for item in items if item.avatar_status == "waiting_cache"),
        "running_count": sum(1 for item in items if item.status == "running"),
        "batch_status": batch.status,
        "latest_failure_type": latest_failure,
    }


def _profile_batch_item(session: Session, item: TgAccountSecurityBatchItem) -> dict[str, Any]:
    account = session.get(TgAccount, item.account_id)
    cache_status = _avatar_cache_status(session, item.avatar_source)
    return {
        "account_id": item.account_id,
        "display_name": item.generated_display_name or (account.display_name if account else ""),
        "phone_number": account.phone_number if account else "",
        "status": item.status,
        "profile_status": item.profile_status,
        "username_status": item.username_status,
        "avatar_status": item.avatar_status,
        "avatar_source": item.avatar_source,
        "avatar_cache_status": cache_status,
        "avatar_preview_url": account.avatar_preview_url if account and account.avatar_object_key else "",
        "failure_type": item.failure_type,
        "failure_detail": item.failure_detail,
    }


def _avatar_cache_summary(session: Session, items: list[TgAccountSecurityBatchItem]) -> dict[str, int]:
    statuses = [_avatar_cache_status(session, item.avatar_source) for item in items if item.avatar_source]
    return {
        "total": len(statuses),
        "ready": statuses.count("ready"),
        "waiting": sum(1 for status in statuses if status in {"not_cached", "refreshing"}),
        "failed": statuses.count("cache_failed"),
        "flood_wait": statuses.count("flood_wait"),
    }


def _avatar_cache_status(session: Session, source: str) -> str:
    material = _material_for_source(session, source)
    if not material:
        return ""
    return material.cache_ready_status or "not_cached"


def _projection_status(batch_status: str) -> str:
    if batch_status in {"running", "ready"}:
        return "running"
    if batch_status == "succeeded":
        return "completed"
    if batch_status == "failed":
        return "failed"
    if batch_status in {"partial_success", "cancelled"}:
        return "stopped"
    return "running"


def _projection_search_text(session: Session, batch: TgAccountSecurityBatch, items: list[TgAccountSecurityBatchItem]) -> str:
    parts: list[str] = [str(batch.id), batch.reason, batch.trace_id]
    for item in items:
        account = session.get(TgAccount, item.account_id)
        material = _material_for_source(session, item.avatar_source)
        parts.extend([item.failure_type, item.failure_detail, item.avatar_source])
        if account:
            parts.extend([account.display_name, account.username, account.phone_number, account.phone_masked])
        if material:
            parts.extend([material.title, material.cache_ready_status])
    return " ".join(str(part) for part in parts if part)


def _batch_items(session: Session, batch_id: int) -> list[TgAccountSecurityBatchItem]:
    return list(
        session.scalars(
            select(TgAccountSecurityBatchItem)
            .where(TgAccountSecurityBatchItem.batch_id == batch_id)
            .order_by(TgAccountSecurityBatchItem.id.asc())
        )
    )


def _is_profile_batch(batch: TgAccountSecurityBatch) -> bool:
    return bool(set(_json_list(batch.action_types)) & {"update_profile", "update_username", "update_avatar"})


def _material_for_source(session: Session, source: str) -> Material | None:
    value = (source or "").strip().removeprefix("avatar:")
    if not (value.startswith("material:") or value.isdigit()):
        return None
    return session.get(Material, int(value.removeprefix("material:")))


def _json_list(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except ValueError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
