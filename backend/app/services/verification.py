from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, GroupAuthStatus, OperationTarget, TgAccount, TgGroup, TgGroupAccount, VerificationTask

from ._common import _now, audit, gateway
from .developer_apps import credentials_for_account


__all__ = [
    "confirm_verification_task",
    "create_verification_task",
    "dismiss_verification_task",
    "list_verification_tasks",
    "resolve_group_restriction_task",
]

MANUAL_VERIFICATION_ACTIONS = {"人工处理", "手动处理", "线下处理"}


def list_verification_tasks(session: Session, tenant_id: int, account_id: int | None = None, group_id: int | None = None, limit: int = 100) -> list[VerificationTask]:
    stmt = select(VerificationTask).where(VerificationTask.tenant_id == tenant_id)
    if account_id:
        stmt = stmt.where(VerificationTask.account_id == account_id)
    if group_id:
        stmt = stmt.where(VerificationTask.group_id == group_id)
    tasks = list(session.scalars(stmt.order_by(VerificationTask.id.desc()).limit(limit)))
    for task in tasks:
        _fill_verification_target(session, task)
    return tasks


def _group_target_values(session: Session, tenant_id: int, group_id: int | None) -> tuple[str, str]:
    if not group_id:
        return "", ""
    group = session.get(TgGroup, group_id)
    if not group or group.tenant_id != tenant_id:
        return "", ""
    return group.tg_peer_id or "", group.title or f"群聊 #{group.id}"


def _fill_verification_target(session: Session, task: VerificationTask) -> None:
    if task.target_peer_id and task.target_display:
        return
    group_peer_id, group_display = _group_target_values(session, task.tenant_id, task.group_id)
    if group_peer_id and not task.target_peer_id:
        task.target_peer_id = group_peer_id
    if group_display and not task.target_display:
        task.target_display = group_display


def _mark_group_sendable(session: Session, task: VerificationTask, account: TgAccount | None) -> None:
    if not task.group_id:
        return
    group = session.get(TgGroup, task.group_id)
    if group:
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
        group.can_send = True
    if account:
        link = session.scalar(
            select(TgGroupAccount).where(TgGroupAccount.group_id == task.group_id, TgGroupAccount.account_id == account.id)
        )
        if link:
            link.can_send = True
            link.permission_label = "可发言"


def _sync_group_target(
    session: Session,
    *,
    tenant_id: int,
    group: TgGroup,
) -> None:
    links = list(
        session.scalars(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id == group.id,
            )
        )
    )
    group.can_send = any(link.can_send for link in links)
    if group.can_send:
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
    elif group.auth_status == GroupAuthStatus.AUTHORIZED.value:
        group.auth_status = GroupAuthStatus.READONLY.value
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
    )
    if target:
        target.title = group.title
        target.member_count = group.member_count
        target.can_send = group.can_send
        target.auth_status = group.auth_status
        target.updated_at = _now()


def _apply_snapshot_to_group_link(session: Session, account: TgAccount, group: TgGroup, snapshot) -> TgGroupAccount:
    group.title = snapshot.title or group.title
    group.group_type = snapshot.group_type or group.group_type
    group.member_count = snapshot.member_count
    link = session.scalar(
        select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id)
    )
    if not link:
        link = TgGroupAccount(
            tenant_id=account.tenant_id,
            group_id=group.id,
            account_id=account.id,
        )
        session.add(link)
        session.flush()
    link.can_send = bool(snapshot.can_send)
    link.permission_label = snapshot.permission_label or ("可发言" if snapshot.can_send else "不可发言")
    _sync_group_target(session, tenant_id=account.tenant_id, group=group)
    return link


def create_verification_task(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    group_id: int | None,
    message_task_id: int | None,
    verification_type: str,
    detected_reason: str,
    suggested_action: str,
    target_peer_id: str = "",
    target_display: str = "",
) -> VerificationTask:
    group_peer_id, group_display = _group_target_values(session, tenant_id, group_id)
    target_peer_id = target_peer_id or group_peer_id
    target_display = target_display or group_display
    existing = session.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.tenant_id == tenant_id,
            VerificationTask.account_id == account_id,
            VerificationTask.group_id == group_id,
            VerificationTask.status == "待处理",
            VerificationTask.verification_type == verification_type,
        )
        .order_by(VerificationTask.id.desc())
    )
    if existing:
        _fill_verification_target(session, existing)
        return existing
    task = VerificationTask(
        tenant_id=tenant_id,
        account_id=account_id,
        group_id=group_id,
        message_task_id=message_task_id,
        verification_type=verification_type,
        detected_reason=detected_reason,
        suggested_action=suggested_action,
        target_peer_id=target_peer_id,
        target_display=target_display,
        requires_user_confirm=True,
        status="待处理",
    )
    session.add(task)
    session.flush()
    audit(session, tenant_id=tenant_id, actor="system", action="生成验证辅助任务", target_type="verification_task", target_id=str(task.id), detail=verification_type)
    return task


def confirm_verification_task(session: Session, task_id: int, actor: str) -> VerificationTask:
    task = session.get(VerificationTask, task_id)
    if not task:
        raise ValueError("verification task not found")
    if task.status not in {"待处理", "失败"}:
        return task
    _fill_verification_target(session, task)
    account = session.get(TgAccount, task.account_id) if task.account_id else None
    if task.suggested_action.strip() in MANUAL_VERIFICATION_ACTIONS:
        if task.issue_category == "group_restriction":
            task.status = "需人工处理"
            task.handled_at = None
            task.failure_detail = "请先在 Telegram 群内由管理员解除限制，再回到账号详情执行解除群限制重查。"
        else:
            task.status = "已处理"
            task.failure_detail = "已由操作员确认完成人工处理"
    elif not account or account.status != AccountStatus.ACTIVE.value:
        task.status = "失败"
        task.failure_detail = "账号不可用，请先完成登录或健康检查"
    else:
        try:
            credentials = credentials_for_account(session, account)
            result = gateway.resolve_verification_task(account.id, task.suggested_action, task.target_peer_id, account.session_ciphertext, credentials)
            task.status = result.status
            task.failure_detail = result.detail
            if task.status in {"已处理", "已完成"} and task.group_id:
                _mark_group_sendable(session, task, account)
        except Exception as exc:  # noqa: BLE001
            task.status = "失败"
            task.failure_detail = str(exc)
    if task.status != "需人工处理":
        task.handled_at = _now()
    audit(session, tenant_id=task.tenant_id, actor=actor, action="处理验证辅助任务", target_type="verification_task", target_id=str(task.id), detail=f"{task.status}:{task.failure_detail}")
    session.commit()
    session.refresh(task)
    return task


def resolve_group_restriction_task(session: Session, task_id: int, actor: str) -> VerificationTask:
    task = session.get(VerificationTask, task_id)
    if not task:
        raise ValueError("verification task not found")
    if not task.group_id or not task.account_id:
        raise ValueError("verification task is not linked to a group target")
    _fill_verification_target(session, task)
    account = session.get(TgAccount, task.account_id)
    group = session.get(TgGroup, task.group_id)
    if not account or not group:
        raise ValueError("account or group not found")
    if account.status != AccountStatus.ACTIVE.value:
        task.status = "失败"
        task.failure_detail = "账号不可用，请先完成登录或健康检查，再检查群限制是否解除。"
        task.handled_at = _now()
    else:
        try:
            credentials = credentials_for_account(session, account)
            snapshots = gateway.list_groups(account.id, account.session_ciphertext, credentials)
            snapshot = next((item for item in snapshots if item.tg_peer_id == group.tg_peer_id), None)
            if not snapshot:
                task.status = "需人工处理"
                task.failure_detail = "目标能力重查未找到该群，请确认账号仍在群内，或重新同步账号群聊。"
                task.handled_at = None
            else:
                link = _apply_snapshot_to_group_link(session, account, group, snapshot)
                if link.can_send:
                    task.status = "已处理"
                    task.failure_detail = f"目标能力重查通过：{link.permission_label or '可发言'}。"
                    task.handled_at = _now()
                else:
                    task.status = "需人工处理"
                    task.failure_detail = f"目标能力重查未通过：{link.permission_label or '不可发言'}。请继续在群内解除限制后重查。"
                    task.handled_at = None
        except Exception as exc:  # noqa: BLE001
            task.status = "失败"
            task.failure_detail = str(exc)
            task.handled_at = _now()
    audit(session, tenant_id=task.tenant_id, actor=actor, action="解除群限制重查", target_type="verification_task", target_id=str(task.id), detail=f"{task.status}:{task.failure_detail}")
    session.commit()
    session.refresh(task)
    return task


def dismiss_verification_task(session: Session, task_id: int, actor: str) -> VerificationTask:
    task = session.get(VerificationTask, task_id)
    if not task:
        raise ValueError("verification task not found")
    task.status = "已忽略"
    task.handled_at = _now()
    audit(session, tenant_id=task.tenant_id, actor=actor, action="忽略验证辅助任务", target_type="verification_task", target_id=str(task.id))
    session.commit()
    session.refresh(task)
    return task
