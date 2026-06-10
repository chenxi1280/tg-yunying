from __future__ import annotations
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram import OperationResult
from app.models import (
    AccountStatus,
    GroupAuthStatus,
    OperationTarget,
    TgAccount,
    TgGroup,
    TgGroupAccount,
    VerificationTask,
)

from ._common import _now, audit, gateway
from .developer_apps import credentials_for_account
from .membership_challenges import (
    auto_resolve_image_verification,
    read_challenge_context,
    read_challenge_context_with_fallback,
)


__all__ = [
    "confirm_verification_task",
    "create_verification_task",
    "dismiss_verification_task",
    "get_verification_challenge_context",
    "list_verification_tasks",
    "refresh_verification_challenge_context",
    "resolve_group_restriction_batch",
    "resolve_group_restriction_task",
    "submit_verification_response",
]

MANUAL_VERIFICATION_ACTIONS = {"人工处理", "手动处理", "线下处理"}
GROUP_RESTRICTION_VERIFICATION_TYPES = ("群发言权限", "群发言不可用")
OPEN_VERIFICATION_STATUSES = ("待处理", "失败", "需人工处理")
ADMIN_APPROVAL_CANDIDATE_LIMIT = 10
VERIFICATION_READER_CANDIDATE_LIMIT = 5
IMAGE_VERIFICATION_MARKERS = ("图片", "图形", "验证码", "captcha", "bot", "机器人")


@dataclass(frozen=True)
class GroupRestrictionBatchResult:
    group_id: int
    target_peer_id: str
    target_display: str
    checked_count: int
    restored_count: int
    blocked_count: int
    failed_count: int
    approval_status: str
    approval_detail: str
    approval_account_id: int | None
    message: str
    tasks: list[VerificationTask]


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
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
    )
    display = target.title if target and target.title else group.title
    return group.tg_peer_id or "", display or f"群聊 #{group.id}"


def _fill_verification_target(session: Session, task: VerificationTask) -> None:
    group_peer_id, group_display = _group_target_values(session, task.tenant_id, task.group_id)
    if group_peer_id and not task.target_peer_id:
        task.target_peer_id = group_peer_id
    if group_display:
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
            VerificationTask.status.in_(("待处理", "失败", "需人工处理")),
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
    task = _group_restriction_task(session, task_id)
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
            result = gateway.probe_target_capabilities(
                account.id,
                group.tg_peer_id,
                "group",
                account.session_ciphertext,
                credentials,
            )
            _apply_group_probe_result(session, task, account, group, result)
        except Exception as exc:  # noqa: BLE001
            task.status = "失败"
            task.failure_detail = str(exc)
            task.handled_at = _now()
    audit(
        session,
        tenant_id=task.tenant_id,
        actor=actor,
        action="解除群限制重查",
        target_type="verification_task",
        target_id=str(task.id),
        detail=f"{task.status}:{task.failure_detail}",
    )
    session.commit()
    session.refresh(task)
    return task


def get_verification_challenge_context(session: Session, task_id: int) -> dict:
    task, account, _group = _group_restriction_task_account_group(session, task_id)
    credentials = credentials_for_account(session, account)
    result = read_challenge_context(session, task, account, credentials)
    session.commit()
    return result


def refresh_verification_challenge_context(session: Session, task_id: int, actor: str) -> dict:
    task, account, group = _group_restriction_task_account_group(session, task_id)
    credentials = credentials_for_account(session, account)
    _retry_membership_before_context(session, task, account, group, credentials)
    if task.status == "已处理":
        context = _sendable_context_payload(task, account)
    elif _should_auto_image_verify(task):
        result = auto_resolve_image_verification(
            session,
            task,
            account,
            credentials,
            reader_candidates=_verification_reader_candidates(session, task, account, group),
        )
        context = _context_from_image_result(session, task, account, group, credentials, result)
    else:
        context = read_challenge_context_with_fallback(
            session,
            task,
            account,
            credentials,
            reader_candidates=_verification_reader_candidates(session, task, account, group),
        ).context
    audit(session, tenant_id=task.tenant_id, actor=actor, action="重新读取验证聊天", target_type="verification_task", target_id=str(task.id), detail=f"{task.status}:{task.failure_detail}")
    session.commit()
    return context


def _retry_membership_before_context(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    group: TgGroup,
    credentials,
) -> None:
    join = gateway.ensure_channel_membership(
        account.id,
        task.target_peer_id,
        account.session_ciphertext,
        credentials,
    )
    if not join.ok:
        task.status = "需人工处理"
        task.failure_detail = join.detail or join.failure_type or "重新加入失败"
        _mark_image_verification_if_needed(task, task.failure_detail)
        return
    probe = gateway.probe_target_capabilities(
        account.id,
        task.target_peer_id,
        "group",
        account.session_ciphertext,
        credentials,
    )
    _apply_group_probe_result(session, task, account, group, probe)
    if not probe.ok:
        _mark_image_verification_if_needed(task, task.failure_detail)


def _mark_image_verification_if_needed(task: VerificationTask, detail: str | None) -> None:
    text = f"{task.detected_reason or ''} {detail or ''}".lower()
    if any(marker.lower() in text for marker in IMAGE_VERIFICATION_MARKERS):
        task.suggested_action = "识别图形验证码"


def _should_auto_image_verify(task: VerificationTask) -> bool:
    return task.suggested_action == "识别图形验证码"


def _verification_reader_candidates(
    session: Session,
    task: VerificationTask,
    submit_account: TgAccount,
    group: TgGroup,
) -> list[tuple[TgAccount, object]]:
    candidates = _verification_reader_accounts(session, task, submit_account, group)
    readable: list[tuple[TgAccount, object]] = []
    for account in candidates:
        readable.append((account, credentials_for_account(session, account)))
    return readable


def _verification_reader_accounts(
    session: Session,
    task: VerificationTask,
    submit_account: TgAccount,
    group: TgGroup,
) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.id != submit_account.id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
        )
        .order_by(TgAccount.id.asc())
        .limit(VERIFICATION_READER_CANDIDATE_LIMIT)
    )
    return list(session.scalars(stmt))


def _context_from_image_result(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    group: TgGroup,
    credentials,
    result: OperationResult,
) -> dict:
    context = getattr(result, "attempt_context", None) or {}
    if result.ok:
        probe = gateway.probe_target_capabilities(account.id, task.target_peer_id, "group", account.session_ciphertext, credentials)
        _apply_group_probe_result(session, task, account, group, probe)
    else:
        task.status = "需人工处理"
        task.failure_detail = result.detail or result.failure_type or "MiMo 验证处理失败"
        task.handled_at = None
    if context:
        context["failure_detail"] = task.failure_detail
        context["suggested_action"] = task.suggested_action
        return context
    return _manual_context_payload(task, account, task.failure_detail)


def _sendable_context_payload(task: VerificationTask, account: TgAccount) -> dict:
    return _refresh_context_payload(task, account, "sendable", "重新加入后已可发言，无需验证码。")


def _manual_context_payload(task: VerificationTask, account: TgAccount, detail: str) -> dict:
    return _refresh_context_payload(task, account, "manual_required", detail)


def _refresh_context_payload(task: VerificationTask, account: TgAccount, status: str, detail: str) -> dict:
    return {
        "task_id": task.id,
        "account_id": account.id,
        "submit_account_id": account.id,
        "reader_account_id": account.id,
        "target_display": task.target_display,
        "target_peer_id": task.target_peer_id,
        "detected_reason": task.detected_reason,
        "failure_detail": task.failure_detail,
        "suggested_action": task.suggested_action,
        "context_status": status,
        "last_read_at": _now(),
        "message_count": 0,
        "read_failure_detail": detail,
        "messages": [],
    }


def submit_verification_response(session: Session, task_id: int, response_text: str, actor: str) -> VerificationTask:
    task, account, group = _group_restriction_task_account_group(session, task_id)
    credentials = credentials_for_account(session, account)
    result = gateway.submit_verification_response(
        account.id,
        task.target_peer_id,
        response_text.strip(),
        account.session_ciphertext,
        credentials,
    )
    if not result.ok:
        task.status = _verification_send_failure_status(result)
        task.failure_detail = getattr(result, "detail", None) or getattr(result, "failure_type", None) or "验证回复发送失败"
        task.handled_at = None
    elif group:
        probe = gateway.probe_target_capabilities(account.id, task.target_peer_id, "group", account.session_ciphertext, credentials)
        _apply_group_probe_result(session, task, account, group, probe)
        if task.status != "已处理":
            task.failure_detail = f"验证回复已发送；{task.failure_detail}"
    else:
        task.status = "已处理"
        task.failure_detail = result.detail or "验证回复已发送"
        task.handled_at = _now()
    audit(session, tenant_id=task.tenant_id, actor=actor, action="提交验证回复", target_type="verification_task", target_id=str(task.id), detail=f"{task.status}:{task.failure_detail}")
    session.commit()
    session.refresh(task)
    return task


def _verification_send_failure_status(result) -> str:
    return getattr(result, "status", None) or "失败"


def _group_restriction_task_account_group(session: Session, task_id: int) -> tuple[VerificationTask, TgAccount, TgGroup]:
    task, account, group = _verification_task_account_group(session, task_id)
    if not group or task.issue_category != "group_restriction":
        raise ValueError("verification task is not a group restriction")
    return task, account, group


def _verification_task_account_group(session: Session, task_id: int) -> tuple[VerificationTask, TgAccount, TgGroup | None]:
    task = session.get(VerificationTask, task_id)
    if not task:
        raise ValueError("verification task not found")
    _fill_verification_target(session, task)
    account = session.get(TgAccount, task.account_id) if task.account_id else None
    if not account:
        raise ValueError("verification task is not linked to an account")
    if account.status != AccountStatus.ACTIVE.value:
        raise ValueError("账号不可用，请先完成登录或健康检查")
    if not task.target_peer_id:
        raise ValueError("verification task has no target peer")
    group = session.get(TgGroup, task.group_id) if task.group_id else None
    return task, account, group


def resolve_group_restriction_batch(session: Session, task_id: int, actor: str) -> GroupRestrictionBatchResult:
    from app.schemas import OperationTargetAdmissionRetryRequest
    from .operations import retry_operation_target_admission

    base_task = _group_restriction_task(session, task_id)
    group = session.get(TgGroup, base_task.group_id)
    if not group:
        raise ValueError("group not found")
    target = _group_operation_target(session, base_task, group)
    accounts = _group_restricted_accounts(session, base_task, group)
    if not accounts:
        approval = ("未执行", "当前目标没有待重查的受限账号", None)
        return _group_restriction_batch_result(base_task, group, [base_task], approval)
    detail = retry_operation_target_admission(
        session,
        base_task.tenant_id,
        target.id,
        OperationTargetAdmissionRetryRequest(reason="验证待处理页批量重查群限制", account_ids=[account.id for account in accounts]),
        actor,
    )
    retry = detail.get("admission_retry") or {}
    queued = int(retry.get("queued_action_count") or 0)
    retried = int(retry.get("retried_account_count") or len(accounts))
    return _queued_group_restriction_batch_result(base_task, group, queued=queued, retried=retried)


def _group_operation_target(session: Session, task: VerificationTask, group: TgGroup) -> OperationTarget:
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == task.tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
    )
    if not target:
        raise ValueError("operation target not found")
    return target


def _queued_group_restriction_batch_result(
    base_task: VerificationTask,
    group: TgGroup,
    *,
    queued: int,
    retried: int,
) -> GroupRestrictionBatchResult:
    target_display = base_task.target_display or group.title or f"群聊 #{group.id}"
    detail = f"已提交后台目标准入重查 {queued} 个动作，覆盖 {retried} 个账号"
    return GroupRestrictionBatchResult(
        group_id=group.id,
        target_peer_id=base_task.target_peer_id or group.tg_peer_id or "",
        target_display=target_display,
        checked_count=retried,
        restored_count=0,
        blocked_count=retried,
        failed_count=0,
        approval_status="已转后台重查",
        approval_detail=detail,
        approval_account_id=None,
        message=f"{target_display} {detail}；后台会逐个重查账号准入并写入审计记录。",
        tasks=[base_task],
    )


def _group_restriction_task(session: Session, task_id: int) -> VerificationTask:
    task = session.get(VerificationTask, task_id)
    if not task:
        raise ValueError("verification task not found")
    if not task.group_id or not task.account_id:
        raise ValueError("verification task is not linked to a group target")
    return task


def _ensure_group_restriction_batch_tasks(
    session: Session,
    base_task: VerificationTask,
    group: TgGroup,
) -> list[int]:
    task_ids = set()
    for account in _group_restricted_accounts(session, base_task, group):
        task = _open_group_restriction_task(session, base_task, account.id)
        if not task:
            task = _create_group_restriction_recheck_task(session, base_task, account.id)
        _fill_verification_target(session, task)
        task_ids.add(task.id)
    if not task_ids:
        task_ids.add(base_task.id)
    session.commit()
    return sorted(task_ids)


def _attempt_group_verification_admin_approval(
    session: Session,
    task: VerificationTask,
    group: TgGroup,
) -> tuple[str, str, int | None]:
    candidates = _admin_approval_candidates(session, task, group)
    if not candidates:
        return "未执行", "未找到可用于群验证放行的候选账号", None
    last_detail = ""
    for account in candidates:
        try:
            credentials = credentials_for_account(session, account)
            result = gateway.approve_group_verification_messages(
                account.id,
                group.tg_peer_id,
                account.session_ciphertext,
                credentials,
            )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc)
            continue
        if result.ok:
            return "已执行", result.detail or "已执行管理员通过按钮", account.id
        last_detail = result.detail or result.failure_type or result.status
        if result.failure_type != "缺少管理员权限":
            return result.status or "需人工处理", last_detail, account.id
    return "需人工处理", last_detail or "未找到可执行群验证放行的管理员账号", None


def _admin_approval_candidates(session: Session, task: VerificationTask, group: TgGroup) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgGroupAccount.group_id == group.id,
            (TgGroupAccount.can_send.is_(True)) | (TgGroupAccount.is_listener.is_(True)),
        )
        .order_by(TgGroupAccount.is_listener.desc(), TgGroupAccount.can_send.desc(), TgAccount.id.asc())
        .limit(ADMIN_APPROVAL_CANDIDATE_LIMIT)
    )
    return list(session.scalars(stmt))


def _group_restricted_accounts(session: Session, task: VerificationTask, group: TgGroup) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(False),
        )
        .order_by(TgAccount.id.asc())
    )
    return list(session.scalars(stmt))


def _open_group_restriction_task(
    session: Session,
    base_task: VerificationTask,
    account_id: int,
) -> VerificationTask | None:
    return session.scalar(
        select(VerificationTask)
        .where(
            VerificationTask.tenant_id == base_task.tenant_id,
            VerificationTask.account_id == account_id,
            VerificationTask.group_id == base_task.group_id,
            VerificationTask.status.in_(OPEN_VERIFICATION_STATUSES),
            VerificationTask.verification_type.in_(GROUP_RESTRICTION_VERIFICATION_TYPES),
        )
        .order_by(VerificationTask.id.desc())
    )


def _create_group_restriction_recheck_task(
    session: Session,
    base_task: VerificationTask,
    account_id: int,
) -> VerificationTask:
    task = VerificationTask(
        tenant_id=base_task.tenant_id,
        account_id=account_id,
        group_id=base_task.group_id,
        verification_type="群发言权限",
        detected_reason="批量重查发现账号仍未获群发言权限",
        suggested_action="人工处理",
        target_peer_id=base_task.target_peer_id,
        target_display=base_task.target_display,
        requires_user_confirm=True,
        status="需人工处理",
    )
    session.add(task)
    session.flush()
    return task


def _group_restriction_batch_result(
    base_task: VerificationTask,
    group: TgGroup,
    tasks: list[VerificationTask],
    approval: tuple[str, str, int | None],
) -> GroupRestrictionBatchResult:
    restored = sum(1 for task in tasks if task.status == "已处理")
    failed = sum(1 for task in tasks if task.status == "失败")
    blocked = len(tasks) - restored - failed
    target_display = base_task.target_display or group.title
    approval_status, approval_detail, approval_account_id = approval
    return GroupRestrictionBatchResult(
        group_id=group.id,
        target_peer_id=base_task.target_peer_id or group.tg_peer_id or "",
        target_display=target_display,
        checked_count=len(tasks),
        restored_count=restored,
        blocked_count=blocked,
        failed_count=failed,
        approval_status=approval_status,
        approval_detail=approval_detail,
        approval_account_id=approval_account_id,
        message=(
            f"{target_display} 管理员放行：{approval_status}（{approval_detail}）；"
            f"已重查 {len(tasks)} 个账号：恢复 {restored}，仍需处理 {blocked}，失败 {failed}。"
        ),
        tasks=tasks,
    )


def _apply_batch_approval_detail(
    tasks: list[VerificationTask],
    approval: tuple[str, str, int | None],
) -> None:
    status, detail, _account_id = approval
    prefix = f"管理员放行：{status}（{detail}）；"
    for task in tasks:
        if task.status not in {"需人工处理", "失败"}:
            continue
        task.failure_detail = f"{prefix}{_strip_batch_approval_detail(task.failure_detail)}"


def _strip_batch_approval_detail(detail: str | None) -> str:
    if not detail:
        return ""
    if detail.startswith("管理员放行：") and "；" in detail:
        return detail.split("；", 1)[1]
    return detail


def _apply_group_probe_result(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    group: TgGroup,
    result: OperationResult,
) -> None:
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account.id,
        )
    )
    if result.ok:
        _mark_group_sendable(session, task, account)
        _sync_group_target(session, tenant_id=task.tenant_id, group=group)
        task.status = "已处理"
        task.failure_detail = "目标能力重查通过：可发言。"
        task.handled_at = _now()
        return
    if link:
        link.can_send = False
        link.permission_label = (result.detail or result.failure_type or "不可发言")[:80]
    _sync_group_target(session, tenant_id=task.tenant_id, group=group)
    task.status = "需人工处理"
    task.failure_detail = _group_probe_failure_detail(result)
    task.handled_at = None


def _group_probe_failure_detail(result: OperationResult) -> str:
    reason = result.detail or result.failure_type or "不可发言"
    return f"目标能力重查未通过：{reason}。请在群内完成图形验证码或由管理员通过后重查。"


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
