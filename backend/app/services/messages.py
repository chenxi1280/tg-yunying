from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Campaign,
    FailureType,
    Material,
    GroupAuthStatus,
    MessageTask,
    MessageTaskAttempt,
    OperationTarget,
    SchedulingSetting,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
    VerificationTask,
)
from app.gateways import DeveloperAppCredentials, OutboundSegment
from app.task_queue import get_task_queue

from ._common import _as_utc, _now, audit, gateway, require_tenant
from app.schemas import DirectMessageTaskCreate, MessageSendBatchCreate, MessageSendTarget, MessageSendTaskCreate

from .accounts import find_account_contact
from .developer_apps import credentials_for_account
from .tenants import ensure_task_quota_available
from .verification import create_verification_task
from .content_filters import tenant_keyword_rules


def _validate_message_material(session: Session, tenant_id: int, message_type: str, material_id: int | None) -> Material | None:
    if message_type == "文本":
        if material_id:
            material = session.get(Material, material_id)
            if not material or material.tenant_id != tenant_id:
                raise ValueError("素材不存在")
            return material
        return None
    if not material_id:
        raise ValueError(f"{message_type}消息需要选择素材")
    material = session.get(Material, material_id)
    if not material or material.tenant_id != tenant_id:
        raise ValueError("素材不存在")
    if material.review_status != "已审核":
        raise ValueError("只能使用已审核素材")
    if material.material_type != message_type:
        raise ValueError(f"请选择{message_type}素材")
    return material


def _resolve_operation_target(session: Session, tenant_id: int, target_id: int, expected_type: str) -> OperationTarget:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id or target.target_type != expected_type:
        raise ValueError("目标不存在")
    if not target.can_send:
        raise ValueError("目标当前不可发送")
    if target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        raise ValueError("目标未授权运营")
    return target


def _linked_group_for_operation_target(session: Session, target: OperationTarget) -> TgGroup | None:
    return session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )


def _ensure_account_can_send_group(session: Session, account: TgAccount, group: TgGroup) -> None:
    if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        raise ValueError("群未授权运营")
    if not group.can_send:
        raise ValueError("群当前不可发送")
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account.id,
            TgGroupAccount.can_send.is_(True),
        )
    )
    if not link:
        raise ValueError("该账号不可向此运营目标发送")


def _ensure_account_can_send_operation_target(session: Session, account: TgAccount, target: OperationTarget) -> TgGroup:
    linked_group = _linked_group_for_operation_target(session, target)
    if not linked_group:
        raise ValueError("该账号不可向此运营目标发送")
    _ensure_account_can_send_group(session, account, linked_group)
    return linked_group


def _resolve_send_account(session: Session, account_id: int, tenant_id: int | None) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("请选择在线且未删除的发送账号")
    if tenant_id is not None and account.tenant_id != tenant_id:
        raise ValueError("发送账号不属于当前租户")
    if account.status != AccountStatus.ACTIVE.value:
        raise ValueError("请选择已在线的发送账号")
    return account


def _resolve_message_target(session: Session, account: TgAccount, target: MessageSendTarget | MessageSendTaskCreate) -> tuple[str, str | None, str, int | None]:
    target_type = target.target_type
    target_peer_id = (target.target_peer_id or "").strip() or None
    target_display = target.target_display.strip()
    group_id: int | None = None

    if target_type == "private":
        if not target_peer_id:
            raise ValueError("请选择或输入联系人")
        target_display = target_display or target_peer_id
    elif target_type == "group":
        if target.group_id:
            group = session.get(TgGroup, target.group_id)
            if not group or group.tenant_id != account.tenant_id:
                raise ValueError("群聊不存在")
            _ensure_account_can_send_group(session, account, group)
            group_id = group.id
            target_peer_id = group.tg_peer_id
            target_display = group.title
        elif target.operation_target_id:
            operation_target = _resolve_operation_target(session, account.tenant_id, target.operation_target_id, "group")
            linked_group = _ensure_account_can_send_operation_target(session, account, operation_target)
            group_id = linked_group.id
            target_peer_id = operation_target.tg_peer_id
            target_display = operation_target.title
        elif target_peer_id:
            target_display = target_display or target_peer_id
        else:
            raise ValueError("请选择群聊目标")
    else:
        if target.operation_target_id:
            operation_target = _resolve_operation_target(session, account.tenant_id, target.operation_target_id, "channel")
            _ensure_account_can_send_operation_target(session, account, operation_target)
            target_peer_id = operation_target.tg_peer_id
            target_display = operation_target.title
        elif target_peer_id:
            target_display = target_display or target_peer_id
        else:
            raise ValueError("请选择频道目标")
    return target_type, target_peer_id, target_display, group_id


def _message_task(
    account: TgAccount,
    target_type: str,
    target_peer_id: str | None,
    target_display: str,
    group_id: int | None,
    content: str,
    message_type: str,
    material_id: int | None,
    planned_delay_seconds: int,
    scheduled_at: datetime,
) -> MessageTask:
    return MessageTask(
        tenant_id=account.tenant_id,
        campaign_id=None,
        group_id=group_id,
        account_id=account.id,
        preferred_account_id=account.id,
        content=content,
        message_type=message_type,
        material_id=material_id,
        target_type=target_type,
        target_peer_id=target_peer_id,
        target_display=target_display,
        planned_delay_seconds=planned_delay_seconds,
        scheduled_at=scheduled_at,
        status=TaskStatus.QUEUED.value,
        idempotency_key=f"send:{account.id}:{uuid4().hex[:12]}",
    )


def _scheduling_setting(session: Session, tenant_id: int) -> SchedulingSetting:
    setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id))
    if not setting:
        setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None)))
    return setting or SchedulingSetting(tenant_id=tenant_id)


def _naive_utc(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def create_message_send_task(session: Session, payload: MessageSendTaskCreate, actor: str, tenant_id: int | None = None) -> MessageTask:
    account = _resolve_send_account(session, payload.account_id, tenant_id)
    ensure_task_quota_available(session, account.tenant_id)
    content = payload.content.strip()
    if payload.message_type == "文本" and not content:
        raise ValueError("请输入消息内容")
    _validate_message_material(session, account.tenant_id, payload.message_type, payload.material_id)
    target_type, target_peer_id, target_display, group_id = _resolve_message_target(session, account, payload)
    jitter_min = max(payload.jitter_min_seconds, 0)
    jitter_max = max(payload.jitter_max_seconds, jitter_min)
    jitter_seconds = random.randint(jitter_min, jitter_max) if jitter_max else 0
    base_at = _naive_utc(payload.scheduled_at) if payload.scheduled_at else _now()
    scheduled_at = base_at + timedelta(seconds=jitter_seconds)
    planned_delay_seconds = max(0, int((_as_utc(scheduled_at) - _as_utc(_now())).total_seconds()))
    task = _message_task(
        account,
        target_type,
        target_peer_id,
        target_display,
        group_id,
        content,
        payload.message_type,
        payload.material_id,
        planned_delay_seconds,
        scheduled_at,
    )
    session.add(task)
    session.flush()
    get_task_queue().enqueue(task.id)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="创建消息发送任务",
        target_type="message_task",
        target_id=str(task.id),
        detail=f"{target_type}:{target_display}",
    )
    session.commit()
    session.refresh(task)
    return task


def create_message_send_tasks_batch(session: Session, payload: MessageSendBatchCreate, actor: str, tenant_id: int | None = None) -> list[MessageTask]:
    account = _resolve_send_account(session, payload.account_id, tenant_id)
    ensure_task_quota_available(session, account.tenant_id)
    content = payload.content.strip()
    if payload.message_type == "文本" and not content:
        raise ValueError("请输入消息内容")
    _validate_message_material(session, account.tenant_id, payload.message_type, payload.material_id)
    setting = _scheduling_setting(session, account.tenant_id)
    jitter_min = max(int(setting.jitter_min_seconds), 0)
    jitter_max = max(int(setting.jitter_max_seconds), jitter_min)
    batch_interval = max(int(setting.batch_interval_seconds), 0)
    start_at = _naive_utc(payload.scheduled_at) if payload.scheduled_at else _now()
    now_value = _as_utc(_now())
    tasks: list[MessageTask] = []

    for index, target in enumerate(payload.targets):
        target_type, target_peer_id, target_display, group_id = _resolve_message_target(session, account, target)
        jitter_seconds = random.randint(jitter_min, jitter_max) if jitter_max else 0
        scheduled_at = start_at + timedelta(seconds=index * batch_interval + jitter_seconds)
        planned_delay_seconds = max(0, int((_as_utc(scheduled_at) - now_value).total_seconds()))
        task = _message_task(
            account,
            target_type,
            target_peer_id,
            target_display,
            group_id,
            content,
            payload.message_type,
            payload.material_id,
            planned_delay_seconds,
            scheduled_at,
        )
        session.add(task)
        session.flush()
        get_task_queue().enqueue(task.id)
        audit(
            session,
            tenant_id=account.tenant_id,
            actor=actor,
            action="批量创建消息发送任务",
            target_type="message_task",
            target_id=str(task.id),
            detail=f"{target_type}:{target_display}",
        )
        tasks.append(task)

    session.commit()
    for task in tasks:
        session.refresh(task)
    return tasks


def create_direct_message_task(session: Session, account_id: int, payload: DirectMessageTaskCreate, actor: str) -> MessageTask:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    contact = find_account_contact(session, account, payload.target_peer_id)
    if not contact:
        raise ValueError("请先同步联系人或群友，并从列表中选择发送对象")
    return create_message_send_task(
        session,
        MessageSendTaskCreate(
            account_id=account.id,
            target_type="private",
            target_peer_id=f"@{contact.username}" if contact.username else contact.peer_id,
            target_display=payload.target_display or contact.display_name,
            content=payload.content,
            material_id=payload.material_id,
            message_type=payload.message_type,
            dispatch_now=False,
        ),
        actor,
        account.tenant_id,
    )


def create_pool_direct_message_task(session: Session, pool_id: int, payload: DirectMessageTaskCreate, actor: str) -> MessageTask:
    from app.models import AccountPool

    pool = session.get(AccountPool, pool_id)
    if not pool:
        raise ValueError("account pool not found")
    if not payload.account_id:
        raise ValueError("请选择发送账号")
    account = session.get(TgAccount, payload.account_id)
    if not account or account.deleted_at is not None or account.tenant_id != pool.tenant_id or account.pool_id != pool.id:
        raise ValueError("发送账号不属于该账号池")
    if account.status != AccountStatus.ACTIVE.value:
        raise ValueError("请选择已在线的发送账号")
    return create_direct_message_task(session, account.id, payload, actor)


def _utc_day_bounds(value: datetime | None = None) -> tuple[datetime, datetime]:
    current = (value or _now()).replace(tzinfo=UTC) if (value or _now()).tzinfo is None else (value or _now()).astimezone(UTC)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _split_rule_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n,，;；]+", raw) if item.strip()]


def _extract_links(text: str) -> list[str]:
    return re.findall(r"(https?://\S+|www\.\S+)", text, flags=re.IGNORECASE)


def _group_sent_today(session: Session, task: MessageTask) -> int:
    if not task.group_id:
        return 0
    day_start, day_end = _utc_day_bounds()
    return session.scalar(
        select(func.count(MessageTask.id)).where(
            MessageTask.tenant_id == task.tenant_id,
            MessageTask.group_id == task.group_id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
            MessageTask.sent_at >= day_start,
            MessageTask.sent_at < day_end,
        )
    ) or 0


def _group_last_sent_at(session: Session, task: MessageTask) -> datetime | None:
    if not task.group_id:
        return None
    return session.scalar(
        select(func.max(MessageTask.sent_at)).where(
            MessageTask.tenant_id == task.tenant_id,
            MessageTask.group_id == task.group_id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
        )
    )


def build_outbound_segments(session: Session, task: MessageTask) -> list[OutboundSegment]:
    material = session.get(Material, task.material_id) if task.material_id else None
    if task.message_type == "组合消息" and material:
        try:
            raw_segments = json.loads(material.content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"组合消息素材格式无效：{exc}") from exc
        segments: list[OutboundSegment] = []
        for item in raw_segments if isinstance(raw_segments, list) else []:
            if isinstance(item, str):
                segments.append(OutboundSegment(segment_type="文本", content=item))
                continue
            if not isinstance(item, dict):
                continue
            segment_type = str(item.get("type") or item.get("segment_type") or "文本")
            segments.append(
                OutboundSegment(
                    segment_type=segment_type,
                    content=str(item.get("content") or ""),
                    source=item.get("source") or item.get("url"),
                    caption=str(item.get("caption") or ""),
                )
            )
        if task.content.strip():
            segments.insert(0, OutboundSegment(segment_type="文本", content=task.content.strip()))
        return segments or [OutboundSegment(segment_type="文本", content=task.content)]
    if task.message_type in {"图片", "表情包", "文件"} and material:
        return [OutboundSegment(segment_type=task.message_type, content=task.content, source=material.content, caption=task.content)]
    if task.message_type == "链接" and material:
        return [OutboundSegment(segment_type="链接", content=task.content, source=material.content)]
    return [OutboundSegment(segment_type="文本", content=task.content)]


def validate_group_task_policy(session: Session, task: MessageTask, group: TgGroup) -> tuple[str | None, str | None]:
    if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群未授权运营"
    if not group.can_send:
        return FailureType.GROUP_PERMISSION_DENIED.value, "群当前不可发送"
    sent_today = _group_sent_today(session, task)
    if sent_today >= group.daily_limit:
        return FailureType.SLOWMODE.value, f"群当日发送已达上限 {group.daily_limit}"
    now_value = _as_utc(_now())
    group_last_sent_at = _group_last_sent_at(session, task)
    if group_last_sent_at and (now_value - _as_utc(group_last_sent_at)).total_seconds() < group.group_cooldown_seconds:
        return FailureType.SLOWMODE.value, f"群冷却中，还需等待 {group.group_cooldown_seconds} 秒"
    segments = build_outbound_segments(session, task)
    text_parts = [task.content]
    for segment in segments:
        text_parts.extend([segment.content, segment.caption, segment.source or ""])
    text = "\n".join(piece for piece in text_parts if piece)
    tenant_hit = next((rule.keyword for rule in tenant_keyword_rules(session, task.tenant_id) if rule.keyword and rule.keyword.lower() in text.lower()), None)
    if tenant_hit:
        return FailureType.CONTENT_REJECTED.value, f"命中租户关键词：{tenant_hit}"
    banned_words = _split_rule_list(group.banned_words)
    hit_words = [word for word in banned_words if word and word in text]
    if hit_words:
        return FailureType.CONTENT_REJECTED.value, f"命中群禁词：{'、'.join(hit_words[:3])}"
    whitelist = _split_rule_list(group.link_whitelist)
    links = _extract_links(text)
    if whitelist and links:
        for link in links:
            normalized = link.lower()
            if not any(rule.lower() in normalized for rule in whitelist):
                return FailureType.CONTENT_REJECTED.value, f"链接不在白名单内：{link}"
    return None, None


def choose_account(session: Session, task: MessageTask) -> tuple[TgAccount | None, str | None, str | None]:
    if task.campaign_id is None and task.target_type in {"private", "group", "channel"} and (task.preferred_account_id or task.account_id):
        fixed_account_id = task.account_id or task.preferred_account_id
        account = session.get(TgAccount, fixed_account_id) if fixed_account_id else None
        if not account or account.deleted_at is not None or account.tenant_id != task.tenant_id or account.status != AccountStatus.ACTIVE.value:
            return None, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用"
        if task.group_id:
            group = session.get(TgGroup, task.group_id)
            if not group:
                return None, FailureType.GROUP_PERMISSION_DENIED.value, "群不存在"
            failure_type, failure_detail = validate_group_task_policy(session, task, group)
            if failure_type:
                return None, failure_type, failure_detail
            link = session.scalar(
                select(TgGroupAccount).where(
                    TgGroupAccount.group_id == task.group_id,
                    TgGroupAccount.account_id == account.id,
                    TgGroupAccount.can_send.is_(True),
                )
            )
            if not link:
                return None, FailureType.ACCOUNT_UNAVAILABLE.value, "该账号不可向此群发送"
        return account, None, None
    group = session.get(TgGroup, task.group_id)
    if not group or group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return None, FailureType.GROUP_PERMISSION_DENIED.value, "群未授权运营"

    failure_type, failure_detail = validate_group_task_policy(session, task, group)
    if failure_type:
        return None, failure_type, failure_detail

    from .campaigns import campaign_selected_accounts  # lazy import to avoid circular dependency

    selected = campaign_selected_accounts(session.get(Campaign, task.campaign_id)) if task.campaign_id else {}
    allowed_account_ids = selected.get(str(task.group_id), [])
    now_value = _as_utc(_now())

    if task.preferred_account_id:
        preferred_allowed = not allowed_account_ids or task.preferred_account_id in allowed_account_ids
        preferred = session.get(TgAccount, task.preferred_account_id) if preferred_allowed else None
        link = (
            session.scalar(
                select(TgGroupAccount).where(
                    TgGroupAccount.group_id == task.group_id,
                    TgGroupAccount.account_id == task.preferred_account_id,
                    TgGroupAccount.can_send.is_(True),
                )
            )
            if preferred
            else None
        )
        if preferred and link and preferred.deleted_at is None and preferred.tenant_id == task.tenant_id and preferred.status == AccountStatus.ACTIVE.value:
            if preferred.developer_app and preferred.developer_app.credentials_version > preferred.developer_app_version:
                preferred.status = AccountStatus.NEED_RELOGIN.value
            elif not link.last_sent_at or (_as_utc(link.last_sent_at) + timedelta(seconds=group.account_cooldown_seconds)) <= now_value:
                return preferred, None, None

    stmt = (
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == task.tenant_id,
            TgGroupAccount.group_id == task.group_id,
            TgGroupAccount.can_send.is_(True),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
        )
        .order_by(TgAccount.health_score.desc())
    )
    if allowed_account_ids:
        stmt = stmt.where(TgAccount.id.in_(allowed_account_ids))
    rows = session.scalars(stmt)
    for account in rows:
        if account.developer_app and account.developer_app.credentials_version > account.developer_app_version:
            account.status = AccountStatus.NEED_RELOGIN.value
            continue
        link = session.scalar(
            select(TgGroupAccount).where(TgGroupAccount.group_id == task.group_id, TgGroupAccount.account_id == account.id)
        )
        if link and link.last_sent_at:
            last_sent = _as_utc(link.last_sent_at)
            if (now_value - last_sent).total_seconds() < group.account_cooldown_seconds:
                continue
        return account, None, None
    return None, FailureType.ACCOUNT_UNAVAILABLE.value, "没有可用于该群的在线账号，或账号仍处于冷却中"


def dispatch_task(session_factory, task_id: int) -> MessageTask:
    with session_factory() as session:
        task = session.get(MessageTask, task_id)
        if not task:
            raise ValueError("task not found")
        if task.status == TaskStatus.SENT.value:
            return task
        scheduled_at = _as_utc(task.scheduled_at)
        if scheduled_at > _as_utc(_now()):
            return task

        account, selection_failure_type, selection_failure_detail = choose_account(session, task)
        if not account:
            task.status = TaskStatus.FAILED.value
            task.failure_type = selection_failure_type or FailureType.ACCOUNT_UNAVAILABLE.value
            task.failure_detail = selection_failure_detail or ("私发任务账号不可用" if task.target_type == "private" else "没有可用于该群的在线账号")
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            if task.group_id:
                create_verification_task(
                    session,
                    tenant_id=task.tenant_id,
                    account_id=task.preferred_account_id,
                    group_id=task.group_id,
                    message_task_id=task.id,
                    verification_type="群发言不可用",
                    detected_reason=task.failure_detail,
                    suggested_action="人工处理",
                )
            session.commit()
            session.refresh(task)
            return task

        task.account_id = account.id
        task.status = TaskStatus.SENDING.value
        try:
            credentials = credentials_for_account(session, account)
        except ValueError as exc:
            task.status = TaskStatus.FAILED.value
            task.failure_type = "账号不可用"
            task.failure_detail = str(exc)
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, account_id=account.id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            session.commit()
            session.refresh(task)
            return task
        session.commit()
        content = task.content
        outbound_segments = build_outbound_segments(session, task)
        account_id = account.id
        group_id = task.group_id or 0
        account_session = account.session_ciphertext
        developer_credentials = credentials
        peer_id = task.target_peer_id
        if not peer_id:
            group = session.get(TgGroup, group_id)
            peer_id = group.tg_peer_id if group else None

    with session_factory() as session:
        account = session.get(TgAccount, account_id)
        task = session.get(MessageTask, task_id)
        if not task:
            raise ValueError("task not found")
        if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
            task.status = TaskStatus.FAILED.value
            task.failure_type = FailureType.ACCOUNT_UNAVAILABLE.value
            task.failure_detail = "账号不可用"
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, account_id=account_id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            audit(session, tenant_id=task.tenant_id, actor="tg-worker", action="执行消息发送", target_type="message_task", target_id=str(task.id), detail=task.failure_detail)
            session.commit()
            session.refresh(task)
            return task

    result = gateway.send_message(
        account_id,
        group_id,
        content,
        outbound_segments,
        account_session,
        peer_id,
        developer_credentials,
    )

    with session_factory() as session:
        task = session.get(MessageTask, task_id)
        if not task:
            raise ValueError("task not found")
        account = session.get(TgAccount, account_id)
        if result.ok and (not account or account.deleted_at is not None):
            task.status = TaskStatus.FAILED.value
            task.failure_type = FailureType.ACCOUNT_UNAVAILABLE.value
            task.failure_detail = "账号已删除" if account else "账号不可用"
            detail = task.failure_detail
        elif result.ok:
            task.status = TaskStatus.SENT.value
            task.sent_at = _now()
            task.failure_type = None
            task.failure_detail = None
            detail = f"remote_message_id={result.remote_message_id}"
            link = session.scalar(
                select(TgGroupAccount).where(TgGroupAccount.group_id == task.group_id, TgGroupAccount.account_id == account_id)
            ) if task.group_id else None
            if link:
                link.last_sent_at = task.sent_at
            if account:
                account.last_active_at = task.sent_at
            material = session.get(Material, task.material_id) if task.material_id else None
            if material:
                material.usage_count += 1
                material.last_used_at = task.sent_at
        else:
            task.status = TaskStatus.FAILED.value
            task.failure_type = result.failure_type
            task.failure_detail = result.detail
            detail = result.detail or ""
            if task.group_id and task.failure_type in {"群无权限", "群慢速模式", "目标无效", "未知错误"}:
                verification_type = "慢速模式" if task.failure_type == "群慢速模式" else "需验证或关注"
                suggested_action = "人工处理"
                if "关注" in detail:
                    suggested_action = "关注频道"
                elif "按钮" in detail or "点击" in detail:
                    suggested_action = "点击按钮"
                create_verification_task(
                    session,
                    tenant_id=task.tenant_id,
                    account_id=account_id,
                    group_id=task.group_id,
                    message_task_id=task.id,
                    verification_type=verification_type,
                    detected_reason=detail or task.failure_type or "发送失败，需要检查群验证",
                    suggested_action=suggested_action,
                )
            if result.failure_type == "账号受限":
                account = session.get(TgAccount, account_id)
                if account:
                    account.status = AccountStatus.LIMITED.value
                    account.health_score = min(account.health_score, 55)

        session.add(
            MessageTaskAttempt(
                tenant_id=task.tenant_id,
                task_id=task.id,
                account_id=account_id,
                status=task.status,
                failure_type=task.failure_type,
                detail=detail,
            )
        )
        audit(session, tenant_id=task.tenant_id, actor="tg-worker", action="执行消息发送", target_type="message_task", target_id=str(task.id), detail=detail)
        session.commit()
        session.refresh(task)
        return task


def retry_task(session_factory, task_id: int, actor: str, dispatch_now: bool) -> MessageTask:
    with session_factory() as session:
        task = session.get(MessageTask, task_id)
        if not task:
            raise ValueError("task not found")
        if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            return task
        task.status = TaskStatus.QUEUED.value
        task.failure_type = None
        task.failure_detail = None
        task.account_id = None
        task.scheduled_at = _now()
        task.planned_delay_seconds = 0
        audit(session, tenant_id=task.tenant_id, actor=actor, action="重试消息任务", target_type="message_task", target_id=str(task.id))
        session.commit()
        session.refresh(task)
        queued_id = task.id

    if dispatch_now:
        return dispatch_task(session_factory, queued_id)
    with session_factory() as session:
        task = session.get(MessageTask, queued_id)
        if not task:
            raise ValueError("task not found")
        return task


def cancel_message_task(session: Session, task_id: int, actor: str) -> MessageTask:
    task = session.get(MessageTask, task_id)
    if not task:
        raise ValueError("task not found")
    if task.status in {TaskStatus.SENT.value, TaskStatus.SENDING.value}:
        raise ValueError("only unsent tasks can be cancelled")
    task.status = TaskStatus.CANCELLED.value
    task.failure_type = None
    task.failure_detail = None
    audit(session, tenant_id=task.tenant_id, actor=actor, action="取消消息任务", target_type="message_task", target_id=str(task.id))
    session.commit()
    session.refresh(task)
    return task


def filter_tasks(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None) -> list[MessageTask]:
    require_tenant(session, tenant_id)
    stmt = select(MessageTask).where(MessageTask.tenant_id == tenant_id)
    if search:
        stmt = stmt.where(MessageTask.content.like(f"%{search}%"))
    if status:
        stmt = stmt.where(MessageTask.status == status)
    return list(session.scalars(stmt.order_by(MessageTask.id.desc()).offset((page - 1) * page_size).limit(page_size)))


__all__ = [
    "create_message_send_task",
    "create_message_send_tasks_batch",
    "create_direct_message_task",
    "create_pool_direct_message_task",
    "dispatch_task",
    "retry_task",
    "cancel_message_task",
    "filter_tasks",
]
