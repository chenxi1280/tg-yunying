from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, Task, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center.payloads import InviteGroupBotPayload


GROUP_RESCUE_FAILURE_THRESHOLD = 3
RESCUE_STATUS_UNCONFIGURED = "unconfigured"
RESCUE_STATUS_PENDING = "pending"
RESCUE_STATUS_INVITE_SUCCESS = "invite_success"
RESCUE_STATUS_INVITE_FAILED = "invite_failed"


@dataclass(frozen=True)
class GroupRescueResult:
    status: str
    detail: str
    action: Action | None = None


def permission_failure_count_for_send_action(session: Session, action: Action) -> int:
    group_id = _payload_int(action.payload, "group_id")
    if not group_id or not action.account_id:
        return 0
    rows = list(session.scalars(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.action_type == "send_message",
            Action.account_id == action.account_id,
        )
    ))
    current_key = _action_sort_key(action)
    count = 0
    for row in sorted(rows, key=_action_sort_key, reverse=True):
        if _action_sort_key(row) > current_key:
            continue
        if _payload_int(row.payload if isinstance(row.payload, dict) else {}, "group_id") != group_id:
            continue
        if _is_group_permission_failure(row, group_id):
            count += 1
            continue
        if row.status in {"success", "failed", "skipped"}:
            break
    return count


def trigger_group_rescue(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    trigger_account_id: int,
    trigger_reason: str,
    operation_target_id: int | None = None,
) -> GroupRescueResult:
    tenant = session.get(Tenant, task.tenant_id)
    config_error = _rescue_config_error(session, tenant)
    if config_error:
        return GroupRescueResult(RESCUE_STATUS_UNCONFIGURED, config_error)
    existing = _existing_rescue_action(session, task, group, trigger_account_id)
    if existing:
        return GroupRescueResult(_action_rescue_status(existing), _action_rescue_detail(existing), existing)
    action = _create_rescue_action(session, tenant, task, group, trigger_account_id, trigger_reason, operation_target_id)
    return GroupRescueResult(RESCUE_STATUS_PENDING, "已创建群聊救援动作", action)


def rescue_action_snapshot(action: Action | None) -> tuple[str, str]:
    if not action:
        return "", ""
    return _action_rescue_status(action), _action_rescue_detail(action)


def refresh_group_rescue_action(
    session: Session,
    task: Task,
    group: TgGroup,
    action: Action,
    *,
    trigger_account_id: int,
    trigger_reason: str,
    operation_target_id: int | None,
) -> GroupRescueResult:
    tenant = session.get(Tenant, task.tenant_id)
    config_error = _rescue_config_error(session, tenant)
    if config_error:
        return GroupRescueResult(RESCUE_STATUS_UNCONFIGURED, config_error)
    payload = _rescue_payload(tenant, task, group, trigger_account_id, trigger_reason, operation_target_id)
    action.account_id = tenant.group_rescue_admin_account_id
    action.payload = payload.model_dump(mode="json")
    action.status = "pending"
    action.scheduled_at = _now()
    action.executed_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {"rescue_status": RESCUE_STATUS_PENDING}
    return GroupRescueResult(RESCUE_STATUS_PENDING, "已按最新群聊救援配置重新排队", action)


def _create_rescue_action(
    session: Session,
    tenant: Tenant,
    task: Task,
    group: TgGroup,
    trigger_account_id: int,
    trigger_reason: str,
    operation_target_id: int | None,
) -> Action:
    payload = _rescue_payload(tenant, task, group, trigger_account_id, trigger_reason, operation_target_id)
    action = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="invite_group_bot",
        account_id=tenant.group_rescue_admin_account_id,
        scheduled_at=_now(),
        status="pending",
        payload=payload.model_dump(mode="json"),
        result={"rescue_status": RESCUE_STATUS_PENDING},
    )
    session.add(action)
    session.flush()
    return action


def _rescue_payload(
    tenant: Tenant,
    task: Task,
    group: TgGroup,
    trigger_account_id: int,
    trigger_reason: str,
    operation_target_id: int | None,
) -> InviteGroupBotPayload:
    return InviteGroupBotPayload(
        group_id=group.id,
        operation_target_id=operation_target_id,
        group_peer_id=group.tg_peer_id,
        bot_username=tenant.group_rescue_bot_username,
        trigger_account_id=trigger_account_id,
        trigger_task_id=task.id,
        trigger_reason=trigger_reason,
    )


def _existing_rescue_action(session: Session, task: Task, group: TgGroup, trigger_account_id: int) -> Action | None:
    rows = session.scalars(
        select(Action).where(Action.tenant_id == task.tenant_id, Action.task_id == task.id, Action.action_type == "invite_group_bot")
    )
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if _payload_int(payload, "group_id") == group.id and _payload_int(payload, "trigger_account_id") == trigger_account_id:
            return row
    return None


def _rescue_config_error(session: Session, tenant: Tenant | None) -> str:
    if not tenant or not tenant.group_rescue_enabled:
        return "救援配置缺失：未启用群聊救援"
    if not tenant.group_rescue_admin_account_id:
        return "救援配置缺失：未选择救援管理员账号"
    if not (tenant.group_rescue_bot_username or "").strip():
        return "救援配置缺失：未配置救援机器人 username"
    account = session.get(TgAccount, tenant.group_rescue_admin_account_id)
    if not account or account.tenant_id != tenant.id or account.deleted_at is not None:
        return "救援配置缺失：救援管理员账号不存在"
    if account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
        return "救援配置缺失：救援管理员账号不可用"
    return ""


def _is_group_permission_failure(action: Action, group_id: int) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    result = action.result if isinstance(action.result, dict) else {}
    error_code = str(result.get("error_code") or "")
    return (
        _payload_int(payload, "group_id") == group_id
        and action.status in {"failed", "skipped"}
        and (error_code in {"群无权限", "membership_permission_denied"} or result.get("membership_status") == "permission_denied")
    )


def _payload_int(payload: dict | None, key: str) -> int:
    try:
        return int((payload or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _action_sort_key(action: Action) -> tuple[datetime, str]:
    return (action.executed_at or action.scheduled_at or _now(), action.id or "")


def _action_rescue_status(action: Action) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    if result.get("rescue_status"):
        return str(result["rescue_status"])
    if action.status == "success":
        return RESCUE_STATUS_INVITE_SUCCESS
    if action.status == "failed":
        return RESCUE_STATUS_INVITE_FAILED
    return RESCUE_STATUS_PENDING


def _action_rescue_detail(action: Action) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    return str(result.get("error_message") or result.get("detail") or result.get("rescue_detail") or "")
