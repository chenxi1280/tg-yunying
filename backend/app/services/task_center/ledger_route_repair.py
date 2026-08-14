from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupBotAdmission,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    TgGroup,
)
from app.services._common import _now

from .ledger_route_repair_actions import (
    cancel_pending_route_actions,
    current_route_action_disposition,
)


PUBLIC_LINK_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "telegram.me/",
)


def preview_group_ai_ledger_route_repair(
    session: Session,
    *,
    task_id: str,
    ledger_id: str,
) -> dict:
    task = _repair_task(session, task_id)
    ledger = _ledger_for_task(session, task, ledger_id)
    ledger_target = _single_ledger_target(session, task, ledger)
    ledger_group, ledger_operation_target = _ledger_route(
        session,
        task,
        ledger,
        ledger_target,
    )
    current_group, current_operation_target = _current_task_route(session, task)
    orphan_target = _orphan_target(session, task, ledger, current_group.id)
    action_disposition = _assert_repairable(
        session,
        task,
        ledger,
        ledger_target,
        ledger_group,
        ledger_operation_target,
        current_group,
        current_operation_target,
        orphan_target,
    )
    return _preview_manifest(
        session,
        task,
        ledger,
        ledger_target,
        ledger_group,
        ledger_operation_target,
        current_group,
        current_operation_target,
        orphan_target,
        action_disposition,
    )


def group_ai_ledger_route_repair_hash(preview: dict) -> str:
    encoded = json.dumps(preview, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_group_ai_ledger_route_repair(
    session: Session,
    *,
    task_id: str,
    ledger_id: str,
    expected_manifest_hash: str,
    approval_ref: str,
    actor: str,
) -> dict:
    if not approval_ref.strip():
        raise ValueError("approval_ref_required")
    preview, manifest_hash = _verified_repair_preview(
        session, task_id, ledger_id, expected_manifest_hash,
    )
    task = _repair_task(session, task_id)
    route = preview["ledger_route"]
    cancelled_action_ids = cancel_pending_route_actions(
        session,
        preview["current_route_action_disposition"],
        approval_ref,
    )
    _restore_task_route(task, route)
    _record_route_repair_audit(
        session, task, ledger_id, manifest_hash, preview, approval_ref, actor, cancelled_action_ids,
    )
    session.flush()
    return _repair_result(task, ledger_id, manifest_hash, route, cancelled_action_ids)


def _verified_repair_preview(
    session: Session,
    task_id: str,
    ledger_id: str,
    expected_manifest_hash: str,
) -> tuple[dict, str]:
    preview = preview_group_ai_ledger_route_repair(
        session,
        task_id=task_id,
        ledger_id=ledger_id,
    )
    actual_hash = group_ai_ledger_route_repair_hash(preview)
    if actual_hash != expected_manifest_hash:
        raise ValueError("repair_manifest_hash_mismatch")
    return preview, actual_hash


def _restore_task_route(task: Task, route: dict) -> None:
    task.type_config = {
        **(task.type_config or {}),
        "target_group_id": route["group_id"],
        "target_operation_target_id": route["operation_target_id"],
        "target_reference_revision": route["target_reference_revision"],
        "target_group_name": route["target_title"],
    }
    if task.last_error == "daily_group_target_ledger_missing":
        task.last_error = ""
        task.next_run_at = _now()
    task.updated_at = _now()


def _record_route_repair_audit(
    session: Session,
    task: Task,
    ledger_id: str,
    manifest_hash: str,
    preview: dict,
    approval_ref: str,
    actor: str,
    cancelled_action_ids: list[str],
) -> None:
    route = preview["ledger_route"]
    session.add(AuditLog(
        tenant_id=task.tenant_id,
        actor=actor,
        action="repair_group_ai_ledger_route",
        target_type="task",
        target_id=task.id,
        detail=json.dumps({
            "approval_ref": approval_ref,
            "ledger_id": ledger_id,
            "manifest_hash": manifest_hash,
            "restored_group_id": route["group_id"],
            "restored_operation_target_id": route["operation_target_id"],
            "safe_terminal_action_count": len(
                preview["current_route_action_disposition"]["safe_terminal_actions"]
            ),
            "cancelled_pending_action_ids": cancelled_action_ids,
        }, ensure_ascii=True, sort_keys=True),
    ))


def _repair_result(
    task: Task,
    ledger_id: str,
    manifest_hash: str,
    route: dict,
    cancelled_action_ids: list[str],
) -> dict:
    return {
        "manifest_hash": manifest_hash,
        "task_id": task.id,
        "ledger_id": ledger_id,
        "restored_group_id": route["group_id"],
        "restored_operation_target_id": route["operation_target_id"],
        "cancelled_pending_action_ids": cancelled_action_ids,
        "readback_route": {
            "target_group_id": task.type_config["target_group_id"],
            "target_operation_target_id": task.type_config["target_operation_target_id"],
            "target_reference_revision": task.type_config["target_reference_revision"],
        },
    }


def _repair_task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task or task.deleted_at is not None or task.type != "group_ai_chat":
        raise ValueError("group_ai_task_not_found")
    return task


def _ledger_for_task(session: Session, task: Task, ledger_id: str) -> TaskDayLedger:
    ledger = session.get(TaskDayLedger, ledger_id)
    if not ledger or ledger.task_id != task.id or ledger.lifecycle_status != "open":
        raise ValueError("open_task_day_ledger_not_found")
    return ledger


def _single_ledger_target(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
) -> TaskGroupDailyTarget:
    rows = list(session.scalars(select(TaskGroupDailyTarget).where(
        TaskGroupDailyTarget.task_id == task.id,
        TaskGroupDailyTarget.task_day_ledger_id == ledger.id,
    )))
    if len(rows) != 1:
        raise ValueError("ledger_target_count_invalid")
    return rows[0]


def _ledger_route(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    ledger_target: TaskGroupDailyTarget,
) -> tuple[TgGroup, OperationTarget]:
    group = session.get(TgGroup, ledger_target.group_id)
    target_ids = list(session.scalars(select(
        TaskGroupDailyMessageSlot.target_operation_target_id
    ).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger.id,
    ).distinct()))
    if not group or group.tenant_id != task.tenant_id or len(target_ids) != 1:
        raise ValueError("ledger_route_invalid")
    target = session.get(OperationTarget, target_ids[0])
    if not target or target.tenant_id != task.tenant_id or target.tg_peer_id != group.tg_peer_id:
        raise ValueError("ledger_route_identity_invalid")
    return group, target


def _current_task_route(session: Session, task: Task) -> tuple[TgGroup, OperationTarget]:
    config = task.type_config or {}
    group_id = int(config.get("target_group_id") or 0)
    target_id = int(config.get("target_operation_target_id") or 0)
    group = session.get(TgGroup, group_id) if group_id else None
    target = session.get(OperationTarget, target_id) if target_id else None
    if not group or not target or group.tenant_id != task.tenant_id or target.tenant_id != task.tenant_id:
        raise ValueError("current_task_route_invalid")
    if group.tg_peer_id != target.tg_peer_id:
        raise ValueError("current_task_route_identity_invalid")
    return group, target


def _orphan_target(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    group_id: int,
) -> TaskGroupDailyTarget:
    rows = list(session.scalars(select(TaskGroupDailyTarget).where(
        TaskGroupDailyTarget.task_id == task.id,
        TaskGroupDailyTarget.group_id == group_id,
        TaskGroupDailyTarget.target_date == ledger.obligation_local_date,
        TaskGroupDailyTarget.task_day_ledger_id.is_(None),
    )))
    if len(rows) != 1:
        raise ValueError("unlinked_current_route_target_count_invalid")
    return rows[0]


def _assert_repairable(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    ledger_target: TaskGroupDailyTarget,
    ledger_group: TgGroup,
    ledger_operation_target: OperationTarget,
    current_group: TgGroup,
    current_operation_target: OperationTarget,
    orphan_target: TaskGroupDailyTarget,
) -> dict:
    if task.status != "running" or task.config_revision != ledger.timezone_revision:
        raise ValueError("task_revision_or_status_drift")
    if ledger_target.target_date != ledger.obligation_local_date:
        raise ValueError("ledger_target_date_invalid")
    if current_group.id == ledger_group.id or current_operation_target.id == ledger_operation_target.id:
        raise ValueError("task_route_already_matches_ledger")
    if not _same_public_identity(ledger_operation_target, current_operation_target):
        raise ValueError("ledger_and_current_route_alias_unproven")
    if _new_route_admission_count(session, task, current_group):
        raise ValueError("current_route_has_admissions")
    return current_route_action_disposition(
        session,
        task,
        orphan_target,
        current_group,
        current_operation_target,
    )


def _same_public_identity(ledger_target: OperationTarget, current_target: OperationTarget) -> bool:
    legacy = _public_username(ledger_target.tg_peer_id) or _public_username(ledger_target.username)
    current = _public_username(current_target.username) or _public_username(current_target.tg_peer_id)
    return bool(legacy and legacy == current)


def _public_username(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    else:
        for prefix in PUBLIC_LINK_PREFIXES:
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].split("?", 1)[0].strip("/")
                break
    return raw.lower() if raw and "/" not in raw else ""


def _new_route_admission_count(session: Session, task: Task, group: TgGroup) -> int:
    return int(session.scalar(select(func.count(TaskGroupBotAdmission.id)).where(
        TaskGroupBotAdmission.task_id == task.id,
        TaskGroupBotAdmission.target_group_id == group.id,
    )) or 0)


def _preview_manifest(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    ledger_target: TaskGroupDailyTarget,
    ledger_group: TgGroup,
    ledger_operation_target: OperationTarget,
    current_group: TgGroup,
    current_operation_target: OperationTarget,
    orphan_target: TaskGroupDailyTarget,
    action_disposition: dict,
) -> dict:
    coverage_count = int(session.scalar(select(func.count(TaskAccountDailyCoverage.id)).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.group_id == current_group.id,
        TaskAccountDailyCoverage.coverage_date == ledger.obligation_local_date,
        TaskAccountDailyCoverage.task_day_ledger_id.is_(None),
    )) or 0)
    return {
        "version": 1,
        "task_id": task.id,
        "ledger_id": ledger.id,
        "ledger_date": ledger.obligation_local_date.isoformat(),
        "ledger_route": _route_manifest(ledger_group, ledger_operation_target, ledger_target.id),
        "current_route": _route_manifest(current_group, current_operation_target, orphan_target.id),
        "task_config_revision": int(task.config_revision),
        "ledger_timezone_revision": int(ledger.timezone_revision),
        "current_route_action_disposition": action_disposition,
        "unlinked_current_coverage_count": coverage_count,
        "orphan_target_created_at": _timestamp(orphan_target.created_at),
    }


def _route_manifest(group: TgGroup, target: OperationTarget, daily_target_id: str) -> dict:
    return {
        "group_id": group.id,
        "operation_target_id": target.id,
        "target_reference_revision": int(target.reference_revision or 1),
        "target_title": target.title,
        "daily_target_id": daily_target_id,
    }


def _timestamp(value: datetime | None) -> str:
    return value.isoformat() if value else ""


__all__ = [
    "apply_group_ai_ledger_route_repair",
    "group_ai_ledger_route_repair_hash",
    "preview_group_ai_ledger_route_repair",
]
