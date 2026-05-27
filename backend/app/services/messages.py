from __future__ import annotations

import json
import math
import random
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.account_capacity import account_capacity_decision, available_accounts_by_capacity
from app.models import (
    AccountStatus,
    Campaign,
    FailureType,
    Material,
    GroupAuthStatus,
    MessageTask,
    MessageTaskAttempt,
    OperationIssue,
    OperationIssueSource,
    OperationTarget,
    SchedulingSetting,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
    VerificationTask,
)
from app.integrations.telegram import DeveloperAppCredentials, OutboundSegment
from app.timezone import beijing_day_bounds
from app.task_queue import get_task_queue

from ._common import _as_utc, _now, audit, gateway, normalize_list_filter, require_tenant
from app.schemas import DirectMessageTaskCreate, MessageSendBatchCreate, MessageSendTarget, MessageSendTaskCreate
from app.schemas.risk_control import RiskPreflightRequest

from .accounts import find_account_contact
from .developer_apps import credentials_for_account
from .tenants import ensure_task_quota_available
from .verification import create_verification_task
from .content_filters import tenant_keyword_rules
from .risk_control import risk_preflight
from .runtime_summary import resolve_message_task_issues_if_recovered, rollup_message_task_failure

MEDIA_MESSAGE_TYPES = {"图片", "表情包", "文件", "组合消息"}
SEGMENT_MEDIA_TYPES = {"图片", "表情包", "文件"}
READY_CACHE_STATUS = "ready"
UNAVAILABLE_CACHE_STATUSES = {"not_cached", "refreshing", "flood_wait", "unrecoverable", "cache_failed"}


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
    if message_type == "组合消息":
        _validate_combination_material(session, tenant_id, material)
    elif message_type in SEGMENT_MEDIA_TYPES:
        _validate_ready_media_material(session, tenant_id, message_type, material.id)
    return material


def _material_send_source(material: Material) -> str:
    if material.tg_cache_peer_id and material.tg_cache_message_id:
        return f"tg-cache://{material.tg_cache_peer_id}/{material.tg_cache_message_id}"
    return material.content


def _material_unavailable_reason(material: Material | None) -> str | None:
    if not material:
        return "cache_not_ready"
    if material.review_status != "已审核":
        return "material_disabled"
    if material.delivery_mode != "download_reupload":
        return "delivery_mode_unsupported"
    if material.material_type == "表情包" and material.emoji_asset_kind == "custom_emoji":
        return None if re.match(r"^custom_emoji:\d+:.+$", material.content or "") else "custom_emoji_unavailable"
    if material.material_type in MEDIA_MESSAGE_TYPES and not (material.tg_cache_peer_id and material.tg_cache_message_id):
        return "cache_not_ready"
    if material.cache_ready_status != READY_CACHE_STATUS:
        if material.cache_ready_status == "flood_wait":
            return "cache_account_flood_wait"
        if material.cache_ready_status in {"not_cached", "refreshing"}:
            return "cache_not_ready"
        if material.cache_ready_status in {"unrecoverable", "cache_failed"}:
            return "material_unrecoverable"
        return material.cache_ready_status or "cache_not_ready"
    return None


def _validate_ready_media_material(session: Session, tenant_id: int, message_type: str, material_id: int | None) -> Material:
    material = session.get(Material, material_id) if material_id else None
    if not material or material.tenant_id != tenant_id:
        raise ValueError("素材不存在")
    if material.material_type != message_type:
        raise ValueError(f"请选择{message_type}素材")
    reason = _material_unavailable_reason(material)
    if reason:
        if reason == "custom_emoji_unavailable":
            raise ValueError("custom emoji 素材格式无效或目标能力不可用")
        raise ValueError(f"素材缓存不可用：{reason}")
    return material


def _validate_combination_material(session: Session, tenant_id: int, material: Material) -> None:
    for item in _combination_items(material):
        if not isinstance(item, dict):
            continue
        segment_type = str(item.get("type") or item.get("segment_type") or "文本")
        if segment_type not in SEGMENT_MEDIA_TYPES:
            continue
        segment_material_id = item.get("material_id")
        if not segment_material_id:
            raise ValueError("组合消息媒体段必须引用已缓存素材")
        _validate_ready_media_material(session, tenant_id, segment_type, int(segment_material_id))


def _combination_items(material: Material) -> list:
    try:
        raw_segments = json.loads(material.content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"组合消息素材格式无效：{exc}") from exc
    if not isinstance(raw_segments, list):
        raise ValueError("组合消息素材必须是数组")
    return raw_segments


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
    media_sent: bool | None = None if message_type == "文本" else False
    return MessageTask(
        tenant_id=account.tenant_id,
        campaign_id=None,
        group_id=group_id,
        account_id=account.id,
        preferred_account_id=account.id,
        content=content,
        message_type=message_type,
        material_id=material_id,
        media_sent=media_sent,
        target_type=target_type,
        target_peer_id=target_peer_id,
        target_display=target_display,
        planned_delay_seconds=planned_delay_seconds,
        scheduled_at=scheduled_at,
        status=TaskStatus.QUEUED.value,
        idempotency_key=f"send:{account.id}:{uuid4().hex[:12]}",
    )


def _apply_task_material_snapshot(task: MessageTask, material: Material | None) -> None:
    if not material:
        task.material_asset_fingerprint = ""
        task.material_cache_ready_status = ""
        task.media_sent = None
        task.media_failure_reason = ""
        return
    task.material_asset_fingerprint = material.asset_fingerprint or ""
    task.material_cache_ready_status = material.cache_ready_status or ""
    if task.message_type in MEDIA_MESSAGE_TYPES:
        task.media_sent = False
        task.media_failure_reason = ""


def _execution_material_failure(session: Session, task: MessageTask) -> str | None:
    if task.message_type not in MEDIA_MESSAGE_TYPES:
        return None
    material = session.get(Material, task.material_id) if task.material_id else None
    if not material:
        return "cache_not_ready"
    _apply_task_material_snapshot(task, material)
    if task.message_type == "组合消息":
        try:
            for item in _combination_items(material):
                if not isinstance(item, dict):
                    continue
                segment_type = str(item.get("type") or item.get("segment_type") or "文本")
                if segment_type not in SEGMENT_MEDIA_TYPES:
                    continue
                segment_material = session.get(Material, int(item.get("material_id") or 0))
                reason = _material_unavailable_reason(segment_material)
                if not segment_material or segment_material.tenant_id != task.tenant_id or segment_material.material_type != segment_type:
                    return "cache_not_ready"
                if reason:
                    return reason
        except (TypeError, ValueError):
            return "cache_not_ready"
        return None
    return _material_unavailable_reason(material)
    return None


def _scheduling_setting(session: Session, tenant_id: int) -> SchedulingSetting:
    setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id))
    if not setting:
        setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None)))
    return setting or SchedulingSetting(tenant_id=tenant_id)


def _attach_operation_issue_status(session: Session, task: MessageTask) -> MessageTask:
    issue = session.scalar(
        select(OperationIssue)
        .join(OperationIssueSource, OperationIssueSource.issue_id == OperationIssue.id)
        .where(
            OperationIssue.tenant_id == task.tenant_id,
            OperationIssueSource.tenant_id == task.tenant_id,
            OperationIssueSource.source_type == "message_task",
            OperationIssueSource.source_id == str(task.id),
        )
        .order_by(OperationIssue.last_seen_at.desc(), OperationIssue.updated_at.desc())
        .limit(1)
    )
    setattr(task, "operation_issue_id", issue.id if issue else "")
    setattr(task, "operation_issue_status", issue.status if issue else "")
    setattr(task, "operation_issue_rolled_up", bool(issue))
    return task


def _attach_operation_issue_statuses(session: Session, tasks: list[MessageTask]) -> list[MessageTask]:
    if not tasks:
        return tasks
    task_ids = [str(task.id) for task in tasks]
    rows = list(
        session.execute(
            select(OperationIssueSource.source_id, OperationIssue)
            .join(OperationIssue, OperationIssue.id == OperationIssueSource.issue_id)
            .where(
                OperationIssueSource.source_type == "message_task",
                OperationIssueSource.source_id.in_(task_ids),
            )
            .order_by(OperationIssue.last_seen_at.desc(), OperationIssue.updated_at.desc())
        )
    )
    issue_by_source_id: dict[str, OperationIssue] = {}
    for source_id, issue in rows:
        issue_by_source_id.setdefault(str(source_id), issue)
    for task in tasks:
        issue = issue_by_source_id.get(str(task.id))
        setattr(task, "operation_issue_id", issue.id if issue else "")
        setattr(task, "operation_issue_status", issue.status if issue else "")
        setattr(task, "operation_issue_rolled_up", bool(issue))
    return tasks


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def create_message_send_task(session: Session, payload: MessageSendTaskCreate, actor: str, tenant_id: int | None = None) -> MessageTask:
    account = _resolve_send_account(session, payload.account_id, tenant_id)
    ensure_task_quota_available(session, account.tenant_id)
    content = payload.content.strip()
    if payload.message_type == "文本" and not content:
        raise ValueError("请输入消息内容")
    material = _validate_message_material(session, account.tenant_id, payload.message_type, payload.material_id)
    _ensure_risk_preflight_passed(session, account, [payload], content, payload.scheduled_at)
    target_type, target_peer_id, target_display, group_id = _resolve_message_target(session, account, payload)
    jitter_min = max(payload.jitter_min_seconds, 0)
    jitter_max = max(payload.jitter_max_seconds, jitter_min)
    jitter_seconds = random.randint(jitter_min, jitter_max) if jitter_max else 0
    base_at = _naive_utc(payload.scheduled_at) if payload.scheduled_at else _now()
    scheduled_at = base_at + timedelta(seconds=jitter_seconds)
    planned_delay_seconds = _planned_delay_seconds(scheduled_at)
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
    _apply_task_material_snapshot(task, material)
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
    material = _validate_message_material(session, account.tenant_id, payload.message_type, payload.material_id)
    _ensure_risk_preflight_passed(session, account, payload.targets, content, payload.scheduled_at)
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
        planned_delay_seconds = _planned_delay_seconds(scheduled_at, now_value)
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
        _apply_task_material_snapshot(task, material)
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


def _ensure_risk_preflight_passed(
    session: Session,
    account: TgAccount,
    targets: list[MessageSendTarget | MessageSendTaskCreate],
    content: str,
    scheduled_at: datetime | None,
) -> None:
    result = risk_preflight(
        session,
        account.tenant_id,
        RiskPreflightRequest(
            scenario="message_send",
            account_ids=[account.id],
            proxy_ids=[account.proxy_id] if account.proxy_id else [],
            target_ids=[int(target.operation_target_id) for target in targets if target.operation_target_id],
            content_preview=content,
            task_type="message_send",
            scheduled_at=scheduled_at,
        ),
    )
    if result["decision"] == "block":
        reasons = [*result.get("suggested_actions", []), *result.get("decision_reasons", [])]
        raise ValueError("风控预检未通过：" + "；".join(str(reason) for reason in reasons if reason))


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


def _split_rule_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\n,，;；]+", raw) if item.strip()]


def _extract_links(text: str) -> list[str]:
    return re.findall(r"(https?://\S+|www\.\S+)", text, flags=re.IGNORECASE)


def _group_sent_today(session: Session, task: MessageTask) -> int:
    if not task.group_id:
        return 0
    day_start, day_end = beijing_day_bounds()
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
        segments: list[OutboundSegment] = []
        for item in _combination_items(material):
            if isinstance(item, str):
                segments.append(OutboundSegment(segment_type="文本", content=item))
                continue
            if not isinstance(item, dict):
                continue
            segment_type = str(item.get("type") or item.get("segment_type") or "文本")
            if segment_type in SEGMENT_MEDIA_TYPES:
                segment_material = _validate_ready_media_material(session, task.tenant_id, segment_type, int(item.get("material_id") or 0))
                segments.append(
                    OutboundSegment(
                        segment_type=segment_type,
                        content=str(item.get("content") or ""),
                        source=_material_send_source(segment_material),
                        caption=str(item.get("caption") or segment_material.caption or ""),
                    )
                )
                continue
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
        source = _material_send_source(material)
        caption = task.content or material.caption
        return [OutboundSegment(segment_type=task.message_type, content=task.content, source=source, caption=caption)]
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
            return _attach_operation_issue_status(session, task)
        scheduled_at = _as_utc(task.scheduled_at)
        if scheduled_at > _as_utc(_now()):
            return _attach_operation_issue_status(session, task)

        account, selection_failure_type, selection_failure_detail = choose_account(session, task)
        if not account:
            task.status = TaskStatus.FAILED.value
            task.failure_type = selection_failure_type or FailureType.ACCOUNT_UNAVAILABLE.value
            task.failure_detail = selection_failure_detail or ("私发任务账号不可用" if task.target_type == "private" else "没有可用于该群的在线账号")
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = False
                task.media_failure_reason = task.failure_type or "account_unavailable"
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            rollup_message_task_failure(session, task)
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
            return _attach_operation_issue_status(session, task)

        capacity_decision = account_capacity_decision(
            session,
            tenant_id=task.tenant_id,
            account_id=account.id,
            scheduled_at=task.scheduled_at,
            exclude_message_task_id=task.id,
        )
        if not capacity_decision.available:
            replacement = _replacement_message_account(session, task, account)
            if replacement:
                original_account_id = account.id
                account = replacement
                task.account_id = replacement.id
                session.add(
                    MessageTaskAttempt(
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        account_id=replacement.id,
                        status=TaskStatus.QUEUED.value,
                        failure_type=None,
                        detail=f"账号达到全局上限，已从 {original_account_id} 转派到 {replacement.id}",
                    )
                )
                audit(
                    session,
                    tenant_id=task.tenant_id,
                    actor="tg-worker",
                    action="消息任务账号转派",
                    target_type="message_task",
                    target_id=str(task.id),
                    detail=f"{original_account_id}->{replacement.id}; {capacity_decision.reason}",
                )
            else:
                return _defer_message_task_for_capacity(session, task, capacity_decision)

        material_failure = _execution_material_failure(session, task)
        if material_failure:
            task.status = TaskStatus.FAILED.value
            task.failure_type = material_failure
            task.failure_detail = f"素材媒体不可发送：{material_failure}"
            task.media_sent = False
            task.media_failure_reason = material_failure
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, account_id=account.id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            rollup_message_task_failure(session, task)
            audit(session, tenant_id=task.tenant_id, actor="tg-worker", action="执行消息发送", target_type="message_task", target_id=str(task.id), detail=task.failure_detail)
            session.commit()
            session.refresh(task)
            return _attach_operation_issue_status(session, task)

        task.account_id = account.id
        task.status = TaskStatus.SENDING.value
        try:
            credentials = credentials_for_account(session, account)
        except ValueError as exc:
            task.status = TaskStatus.FAILED.value
            task.failure_type = "账号不可用"
            task.failure_detail = str(exc)
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = False
                task.media_failure_reason = task.failure_type
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, account_id=account.id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            rollup_message_task_failure(session, task)
            session.commit()
            session.refresh(task)
            return _attach_operation_issue_status(session, task)
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
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = False
                task.media_failure_reason = task.failure_type
            session.add(MessageTaskAttempt(tenant_id=task.tenant_id, task_id=task.id, account_id=account_id, status=task.status, failure_type=task.failure_type, detail=task.failure_detail))
            rollup_message_task_failure(session, task)
            audit(session, tenant_id=task.tenant_id, actor="tg-worker", action="执行消息发送", target_type="message_task", target_id=str(task.id), detail=task.failure_detail)
            session.commit()
            session.refresh(task)
            return _attach_operation_issue_status(session, task)

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
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = False
                task.media_failure_reason = task.failure_type
            detail = task.failure_detail
        elif result.ok:
            task.status = TaskStatus.SENT.value
            task.sent_at = _now()
            task.failure_type = None
            task.failure_detail = None
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = True
                task.media_failure_reason = ""
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
            if task.message_type in MEDIA_MESSAGE_TYPES:
                task.media_sent = False
                task.media_failure_reason = result.failure_type or "send_failed"
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
        if task.status == TaskStatus.FAILED.value:
            rollup_message_task_failure(session, task)
        elif task.status == TaskStatus.SENT.value:
            resolve_message_task_issues_if_recovered(session, task)
        audit(session, tenant_id=task.tenant_id, actor="tg-worker", action="执行消息发送", target_type="message_task", target_id=str(task.id), detail=detail)
        session.commit()
        session.refresh(task)
        return _attach_operation_issue_status(session, task)


def _replacement_message_account(session: Session, task: MessageTask, current_account: TgAccount) -> TgAccount | None:
    if task.target_type == "private":
        return None
    stmt = (
        select(TgAccount)
        .where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.id != current_account.id,
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    group = session.get(TgGroup, task.group_id) if task.group_id else _linked_group_for_peer(session, task)
    if group:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
        )
    candidates = list(session.scalars(stmt.limit(50)))
    available = available_accounts_by_capacity(
        session,
        tenant_id=task.tenant_id,
        accounts=candidates,
        scheduled_at=task.scheduled_at,
        limit=1,
        exclude_message_task_id=task.id,
    )
    return available[0] if available else None


def _linked_group_for_peer(session: Session, task: MessageTask) -> TgGroup | None:
    if not task.target_peer_id:
        return None
    return session.scalar(select(TgGroup).where(TgGroup.tenant_id == task.tenant_id, TgGroup.tg_peer_id == task.target_peer_id))


def _defer_message_task_for_capacity(session: Session, task: MessageTask, decision) -> MessageTask:
    defer_until = decision.defer_until or (_now() + timedelta(seconds=60))
    task.status = TaskStatus.QUEUED.value
    task.scheduled_at = defer_until
    task.planned_delay_seconds = _planned_delay_seconds(defer_until)
    task.failure_type = decision.reason_code or "账号全局限额"
    task.failure_detail = decision.reason or "账号全局限额或冷却中，已延后执行"
    session.add(
        MessageTaskAttempt(
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=task.account_id,
            status=TaskStatus.QUEUED.value,
            failure_type=task.failure_type,
            detail=f"{task.failure_detail}；下次尝试 {defer_until:%Y-%m-%d %H:%M:%S}",
        )
    )
    audit(
        session,
        tenant_id=task.tenant_id,
        actor="tg-worker",
        action="消息任务账号限额延后",
        target_type="message_task",
        target_id=str(task.id),
        detail=task.failure_detail,
    )
    session.commit()
    get_task_queue().enqueue(task.id)
    session.refresh(task)
    return _attach_operation_issue_status(session, task)


def _planned_delay_seconds(scheduled_at: datetime, now_value: datetime | None = None) -> int:
    return max(0, int(math.ceil((_as_utc(scheduled_at) - (now_value or _as_utc(_now()))).total_seconds())))


def retry_task(session_factory, task_id: int, actor: str, dispatch_now: bool) -> MessageTask:
    with session_factory() as session:
        task = session.get(MessageTask, task_id)
        if not task:
            raise ValueError("task not found")
        if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            return _attach_operation_issue_status(session, task)
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
        return _attach_operation_issue_status(session, task)


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
    return _attach_operation_issue_status(session, task)


def filter_tasks(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None) -> list[MessageTask]:
    require_tenant(session, tenant_id)
    status_filter = normalize_list_filter(status)
    stmt = select(MessageTask).where(MessageTask.tenant_id == tenant_id)
    if search:
        stmt = stmt.where(MessageTask.content.like(f"%{search}%"))
    if status_filter:
        stmt = stmt.where(MessageTask.status == status_filter)
    tasks = list(session.scalars(stmt.order_by(MessageTask.id.desc()).offset((page - 1) * page_size).limit(page_size)))
    return _attach_operation_issue_statuses(session, tasks)


def get_message_task(session: Session, tenant_id: int, task_id: int) -> MessageTask:
    task = session.get(MessageTask, task_id)
    if not task or task.tenant_id != tenant_id:
        raise ValueError("task not found")
    return _attach_operation_issue_status(session, task)


def precheck_message_task(session: Session, tenant_id: int, task_id: int) -> dict:
    task = get_message_task(session, tenant_id, task_id)
    target_ids: list[int] = []
    target_peer_id = task.target_peer_id
    if not target_peer_id and task.group_id:
        group = session.get(TgGroup, task.group_id)
        target_peer_id = group.tg_peer_id if group else None
    if target_peer_id:
        target = session.scalar(
            select(OperationTarget.id).where(
                OperationTarget.tenant_id == tenant_id,
                OperationTarget.target_type == task.target_type,
                OperationTarget.tg_peer_id == target_peer_id,
            )
        )
        if target:
            target_ids.append(int(target))
    account_ids = [int(task.account_id)] if task.account_id else []
    account = session.get(TgAccount, task.account_id) if task.account_id else None
    return risk_preflight(
        session,
        tenant_id,
        RiskPreflightRequest(
            scenario="message_send_task_precheck",
            account_ids=account_ids,
            proxy_ids=[account.proxy_id] if account and account.proxy_id else [],
            target_ids=target_ids,
            content_preview=task.content,
            task_type="message_send",
            scheduled_at=task.scheduled_at,
        ),
    )


__all__ = [
    "create_message_send_task",
    "create_message_send_tasks_batch",
    "create_direct_message_task",
    "create_pool_direct_message_task",
    "dispatch_task",
    "retry_task",
    "cancel_message_task",
    "filter_tasks",
    "get_message_task",
    "precheck_message_task",
]
