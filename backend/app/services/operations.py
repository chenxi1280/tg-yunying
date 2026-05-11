from __future__ import annotations

from datetime import datetime, timedelta
import random
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    AccountStatus,
    ChannelMessage,
    FailureType,
    GroupAuthStatus,
    ManualOperationRecord,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import (
    ChannelMessageCreate,
    ManualSendRequest,
    OperationTargetCreate,
    OperationTargetUpdate,
    OperationTaskCreate,
)

from ._common import _now, ai_gateway, audit, gateway
from .ai_config import ai_provider_credentials, get_tenant_ai_setting
from .developer_apps import credentials_for_account
from .group_listeners import collect_group_context, recent_context_messages
from .notifications import notify_ai_failure


def _account_id_csv(values: list[int] | str | None) -> str:
    if isinstance(values, str):
        return values
    return ",".join(str(value) for value in values or [])


def _parse_account_ids(raw: str) -> list[int]:
    result: list[int] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


def resolve_actual_quantity(quantity: int, jitter_ratio: float | int = 0.15) -> int:
    quantity = max(1, min(500, int(quantity)))
    if quantity == 1:
        return 1
    ratio = max(0.0, min(1.0, float(jitter_ratio)))
    spread = max(1, round(quantity * ratio)) if ratio else 0
    return max(1, min(500, random.randint(quantity - spread, quantity + spread)))


def _jitter_ratio_percent(value: float) -> int:
    return max(0, min(100, round(float(value) * 100)))


def _attempt_schedule(task: OperationTask, index: int) -> tuple[datetime, int]:
    if task.interval_seconds > 0:
        delay = index * task.interval_seconds
    else:
        delay = 0
    return _now() + timedelta(seconds=delay), delay


def _pick_ai_provider(session: Session, tenant_id: int) -> AiProvider | None:
    setting = get_tenant_ai_setting(session, tenant_id)
    if not setting.ai_enabled:
        raise RuntimeError("客户 AI 配置关闭")
    if setting.default_provider_id:
        provider = session.get(AiProvider, setting.default_provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
            return provider
    return session.scalar(
        select(AiProvider)
        .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
        .order_by(AiProvider.id.asc())
    )


def _generate_operation_contents(
    session: Session,
    task: OperationTask,
    *,
    count: int,
    target_label: str,
) -> list[str]:
    provider = _pick_ai_provider(session, task.tenant_id)
    if not provider:
        raise RuntimeError("没有健康 AI 供应商")
    setting = get_tenant_ai_setting(session, task.tenant_id)
    if task.task_type == "CHANNEL_REPLY":
        purpose = "频道消息评论区回复"
        output_hint = "每条像不同真实账号写出的短回复，不要编号，不要暴露 AI 或运营任务。"
    else:
        purpose = "群聊或频道消息发送"
        output_hint = "每条都要自然差异化，不要刷屏，不要暴露 AI 或运营任务。"
    prompt = (
        f"请为 Telegram {purpose}生成 {count} 条内容。\n"
        f"目标：{target_label}\n"
        f"任务标题：{task.title}\n"
        f"用户要求/主题：{task.content}\n"
        f"{output_hint}\n"
        '只输出 JSON：{"drafts":[{"persona":"账号人设","content":"内容","risk_level":"低"}]}'
    )
    result = ai_gateway.generate_drafts(
        ai_provider_credentials(provider),
        prompt,
        count=count,
        topic=task.content or task.title,
        tone="自然、口语化、不同账号表达不重复",
        persona_set=["老用户", "新用户", "活跃成员", "路人"],
        temperature=setting.temperature,
        max_tokens=max(setting.max_tokens, 1024),
        selected_account_ids=_parse_account_ids(task.account_ids),
    )
    contents = [candidate.content.strip() for candidate in result.candidates if candidate.content.strip()]
    if len(contents) < count:
        raise RuntimeError(f"AI 生成数量不足：{len(contents)}/{count}")
    return contents[:count]


def _failure_attempt(
    session: Session,
    task: OperationTask,
    failure_detail: str,
    *,
    failure_type: str = FailureType.UNKNOWN.value,
    notify_ai: bool = False,
) -> None:
    task.status = TaskStatus.FAILED.value
    task.failure_type = failure_type
    task.failure_detail = failure_detail
    task.executed_at = _now()
    session.add(
        OperationTaskAttempt(
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=None,
            action_type=task.task_type,
            status=TaskStatus.FAILED.value,
            failure_type=failure_type,
            failure_detail=failure_detail,
            idempotency_key=f"op:{task.id}:ai-failed:{uuid4().hex[:8]}",
            scheduled_at=_now(),
            executed_at=_now(),
        )
    )
    if notify_ai:
        notify_ai_failure(
            session,
            tenant_id=task.tenant_id,
            title="AI 运营任务失败",
            detail=failure_detail,
            target_type="operation_task",
            target_id=str(task.id),
        )


def _planned_accounts(accounts: list[TgAccount], count: int) -> list[TgAccount]:
    shuffled = list(accounts)
    random.shuffle(shuffled)
    return [shuffled[index % len(shuffled)] for index in range(count)] if shuffled else []


def _target_label_for_task(session: Session, task: OperationTask) -> str:
    if task.task_type == "MESSAGE_SEND":
        target = session.get(OperationTarget, task.target_id) if task.target_id else None
        return f"{target.target_type}:{target.title}" if target else "unknown target"
    message = session.get(ChannelMessage, task.channel_message_id) if task.channel_message_id else None
    channel = session.get(OperationTarget, message.channel_target_id) if message else None
    channel_label = channel.title if channel else (message.channel_target_id if message else "unknown channel")
    message_id = message.message_id if message else "unknown"
    return f"channel:{channel_label} message:{message_id}"


def _content_plan_for_task(session: Session, task: OperationTask) -> list[str]:
    if task.content_mode == "ai":
        return _generate_operation_contents(
            session,
            task,
            count=task.actual_quantity,
            target_label=_target_label_for_task(session, task),
        )
    return [task.content] * task.actual_quantity


def _create_attempt_plan(session: Session, task: OperationTask, contents: list[str]) -> None:
    accounts = _task_accounts(session, task)
    if not accounts:
        _failure_attempt(
            session,
            task,
            "没有可用在线账号",
            failure_type=FailureType.ACCOUNT_UNAVAILABLE.value,
        )
        return
    planned_accounts = _planned_accounts(accounts, task.actual_quantity)
    for index in range(task.actual_quantity):
        scheduled_at, delay = _attempt_schedule(task, index)
        session.add(
            OperationTaskAttempt(
                tenant_id=task.tenant_id,
                task_id=task.id,
                account_id=planned_accounts[index].id,
                action_type=task.task_type,
                content=contents[index],
                reaction=task.reaction,
                status=TaskStatus.QUEUED.value,
                idempotency_key=f"op:{task.id}:{index + 1}:{uuid4().hex[:10]}",
                planned_delay_seconds=delay,
                scheduled_at=scheduled_at,
            )
        )


def filter_operation_targets(
    session: Session,
    tenant_id: int = 1,
    target_type: str | None = None,
    account_id: int | None = None,
) -> list[dict]:
    if account_id:
        account = session.get(TgAccount, account_id)
        if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
            raise ValueError("account not found")

    stmt = select(OperationTarget).where(OperationTarget.tenant_id == tenant_id)
    if target_type:
        stmt = stmt.where(OperationTarget.target_type == target_type)
    targets = list(session.scalars(stmt.order_by(OperationTarget.id.desc())))
    linked_groups = {
        group.tg_peer_id: group
        for group in session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id))
    }
    group_ids = [group.id for group in linked_groups.values()]
    links_by_group: dict[int, list[TgGroupAccount]] = {group_id: [] for group_id in group_ids}
    if group_ids:
        for link in session.scalars(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id.in_(group_ids),
            )
        ):
            links_by_group.setdefault(link.group_id, []).append(link)
    if account_id:
        visible_group_ids = {
            link.group_id
            for links in links_by_group.values()
            for link in links
            if link.account_id == account_id and link.can_send
        }
        targets = [
            target
            for target in targets
            if target.tg_peer_id in linked_groups and linked_groups[target.tg_peer_id].id in visible_group_ids
        ]
    return [_operation_target_list_payload(target, linked_groups.get(target.tg_peer_id), links_by_group) for target in targets]


def create_operation_target(session: Session, payload: OperationTargetCreate, actor: str) -> OperationTarget:
    existing = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == payload.tenant_id,
            OperationTarget.tg_peer_id == payload.tg_peer_id,
        )
    )
    if existing:
        raise ValueError("target already exists")
    target = OperationTarget(**payload.model_dump())
    session.add(target)
    session.flush()
    audit(session, tenant_id=target.tenant_id, actor=actor, action="创建运营目标", target_type="operation_target", target_id=str(target.id), detail=target.title)
    session.commit()
    session.refresh(target)
    return target


def update_operation_target(session: Session, target_id: int, payload: OperationTargetUpdate, actor: str) -> OperationTarget:
    target = session.get(OperationTarget, target_id)
    if not target:
        raise ValueError("target not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(target, key, value)
    target.updated_at = _now()
    audit(session, tenant_id=target.tenant_id, actor=actor, action="更新运营目标", target_type="operation_target", target_id=str(target.id), detail=target.title)
    session.commit()
    session.refresh(target)
    return target


def ensure_operation_targets_from_legacy_groups(session: Session, tenant_id: int = 1) -> int:
    inserted = 0
    for group in session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id)):
        existing = session.scalar(
            select(OperationTarget).where(
                OperationTarget.tenant_id == tenant_id,
                OperationTarget.tg_peer_id == group.tg_peer_id,
            )
        )
        if existing:
            continue
        session.add(
            OperationTarget(
                tenant_id=tenant_id,
                target_type="channel" if group.group_type == "channel" else "group",
                tg_peer_id=group.tg_peer_id,
                title=group.title,
                member_count=group.member_count,
                can_send=group.can_send,
                auth_status=group.auth_status,
                last_sync_at=_now(),
            )
        )
        inserted += 1
    if inserted:
        session.commit()
    return inserted


def sync_account_targets(session: Session, account_id: int, actor: str) -> list[OperationTarget]:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    credentials = credentials_for_account(session, account)
    snapshots = gateway.list_groups(account.id, account.session_ciphertext, credentials)
    targets: list[OperationTarget] = []
    for snapshot in snapshots:
        target_type = "channel" if snapshot.group_type == "channel" else "group"
        target = session.scalar(
            select(OperationTarget).where(
                OperationTarget.tenant_id == account.tenant_id,
                OperationTarget.tg_peer_id == snapshot.tg_peer_id,
            )
        )
        if target is None:
            target = OperationTarget(tenant_id=account.tenant_id, tg_peer_id=snapshot.tg_peer_id, title=snapshot.title)
            session.add(target)
        target.target_type = target_type
        target.title = snapshot.title
        target.username = snapshot.username or ""
        target.member_count = snapshot.member_count
        target.can_send = snapshot.can_send
        target.auth_status = "已授权运营" if snapshot.can_send else "只读归档"
        target.last_sync_at = _now()
        target.updated_at = _now()
        targets.append(target)
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步群频道目标", target_type="tg_account", target_id=str(account.id), detail=f"targets={len(targets)}")
    session.commit()
    return filter_operation_targets(session, account.tenant_id)


def create_channel_message(session: Session, payload: ChannelMessageCreate, actor: str) -> ChannelMessage:
    channel = session.get(OperationTarget, payload.channel_target_id)
    if not channel or channel.target_type != "channel":
        raise ValueError("channel target not found")
    existing = session.scalar(
        select(ChannelMessage).where(
            ChannelMessage.tenant_id == payload.tenant_id,
            ChannelMessage.channel_target_id == payload.channel_target_id,
            ChannelMessage.message_id == payload.message_id,
        )
    )
    if existing:
        return existing
    message = ChannelMessage(**payload.model_dump())
    session.add(message)
    session.flush()
    audit(session, tenant_id=message.tenant_id, actor=actor, action="登记频道消息", target_type="channel_message", target_id=str(message.id), detail=message.message_url)
    session.commit()
    session.refresh(message)
    return message


def filter_channel_messages(session: Session, tenant_id: int = 1, channel_target_id: int | None = None) -> list[ChannelMessage]:
    stmt = select(ChannelMessage).where(ChannelMessage.tenant_id == tenant_id)
    if channel_target_id:
        stmt = stmt.where(ChannelMessage.channel_target_id == channel_target_id)
    return list(session.scalars(stmt.order_by(ChannelMessage.id.desc())))


def _operation_target_for_tenant(session: Session, tenant_id: int, target_id: int) -> OperationTarget:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id:
        raise ValueError("target not found")
    return target


def _linked_group_for_target(session: Session, target: OperationTarget) -> TgGroup | None:
    return session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )


def _operation_target_list_payload(
    target: OperationTarget,
    linked_group: TgGroup | None,
    links_by_group: dict[int, list[TgGroupAccount]],
) -> dict:
    send_links = [link for link in links_by_group.get(linked_group.id if linked_group else 0, []) if link.can_send]
    listener_links = [link for link in links_by_group.get(linked_group.id if linked_group else 0, []) if link.is_listener]
    return {
        "id": target.id,
        "tenant_id": target.tenant_id,
        "target_type": target.target_type,
        "tg_peer_id": target.tg_peer_id,
        "title": target.title,
        "username": target.username,
        "member_count": target.member_count,
        "can_send": target.can_send,
        "auth_status": target.auth_status,
        "linked_group_id": linked_group.id if linked_group else None,
        "can_listen": bool(linked_group and (linked_group.listener_enabled or listener_links)),
        "can_archive": target.target_type == "group" and target.auth_status == GroupAuthStatus.AUTHORIZED.value,
        "available_send_account_count": len(send_links),
        "listener_account_count": len(listener_links),
        "last_sync_at": target.last_sync_at,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


def _group_accounts_for_detail(session: Session, group: TgGroup) -> list[dict]:
    links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(TgGroupAccount.tenant_id == group.tenant_id, TgGroupAccount.group_id == group.id)
            .order_by(TgGroupAccount.id.asc())
        )
    )
    accounts: list[dict] = []
    for link in links:
        account = session.get(TgAccount, link.account_id)
        if not account or account.deleted_at is not None:
            continue
        accounts.append(
            {
                "id": account.id,
                "display_name": account.display_name,
                "username": account.username,
                "status": account.status,
                "health_score": account.health_score,
                "permission_label": link.permission_label,
                "can_send": link.can_send,
                "is_listener": link.is_listener,
                "last_sent_at": link.last_sent_at,
            }
        )
    return accounts


def _group_messages_for_detail(session: Session, group: TgGroup) -> list[dict]:
    return [
        {
            "id": item.id,
            "listener_account_id": item.listener_account_id,
            "sender_name": item.sender_name,
            "content": item.content,
            "message_type": item.message_type,
            "sent_at": item.sent_at,
            "used_for_ai": item.used_for_ai,
        }
        for item in recent_context_messages(session, group, group.listener_context_limit)
    ]


def operation_target_detail(session: Session, tenant_id: int, target_id: int, *, sync_error: str = "") -> dict:
    target = _operation_target_for_tenant(session, tenant_id, target_id)
    linked_group = _linked_group_for_target(session, target) if target.target_type == "group" else None
    accounts = _group_accounts_for_detail(session, linked_group) if linked_group else []
    group_messages = _group_messages_for_detail(session, linked_group) if linked_group else []
    channel_messages = filter_channel_messages(session, tenant_id, target.id) if target.target_type == "channel" else []
    return {
        "target": target,
        "linked_group": (
            {
                "id": linked_group.id,
                "title": linked_group.title,
                "group_type": linked_group.group_type,
                "member_count": linked_group.member_count,
                "auth_status": linked_group.auth_status,
                "can_send": linked_group.can_send,
                "listener_enabled": linked_group.listener_enabled,
                "listener_context_limit": linked_group.listener_context_limit,
                "listener_last_error": linked_group.listener_last_error,
            }
            if linked_group
            else None
        ),
        "accounts": accounts,
        "group_messages": group_messages,
        "channel_messages": channel_messages,
        "sync_error": sync_error,
        "stats": {
            "available_accounts": sum(1 for item in accounts if item["can_send"] and item["status"] == AccountStatus.ACTIVE.value),
            "listener_accounts": sum(1 for item in accounts if item["is_listener"]),
            "group_messages": len(group_messages),
            "channel_messages": len(channel_messages),
        },
    }


def _channel_message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    if channel.tg_peer_id.startswith("-100") and channel.tg_peer_id[4:].isdigit():
        return f"https://t.me/c/{channel.tg_peer_id[4:]}/{message_id}"
    return ""


def _normalize_snapshot_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _channel_sync_account(session: Session, tenant_id: int) -> TgAccount | None:
    return session.scalar(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        .limit(1)
    )


def _sync_channel_target_messages(session: Session, target: OperationTarget, *, limit: int = 50) -> int:
    account = _channel_sync_account(session, target.tenant_id)
    if not account:
        raise ValueError("没有可用于采集频道消息的在线账号")
    snapshots = gateway.fetch_channel_messages(
        account.id,
        target.tg_peer_id,
        account.session_ciphertext,
        credentials_for_account(session, account),
        limit=limit,
    )
    inserted = 0
    for snapshot in snapshots:
        message_id = int(snapshot.message_id or 0)
        if message_id <= 0:
            continue
        existing = session.scalar(
            select(ChannelMessage).where(
                ChannelMessage.tenant_id == target.tenant_id,
                ChannelMessage.channel_target_id == target.id,
                ChannelMessage.message_id == message_id,
            )
        )
        published_at = _normalize_snapshot_datetime(snapshot.published_at)
        if existing:
            existing.content_preview = snapshot.content_preview or existing.content_preview
            existing.message_url = snapshot.message_url or existing.message_url or _channel_message_url(target, message_id)
            existing.published_at = published_at or existing.published_at
            continue
        session.add(
            ChannelMessage(
                tenant_id=target.tenant_id,
                channel_target_id=target.id,
                message_id=message_id,
                message_url=snapshot.message_url or _channel_message_url(target, message_id),
                content_preview=snapshot.content_preview,
                published_at=published_at,
            )
        )
        inserted += 1
    target.last_sync_at = _now()
    target.updated_at = _now()
    session.flush()
    return inserted


def sync_operation_target_messages(session: Session, tenant_id: int, target_id: int, actor: str) -> dict:
    target = _operation_target_for_tenant(session, tenant_id, target_id)
    inserted = 0
    sync_error = ""
    try:
        if target.target_type == "channel":
            inserted = _sync_channel_target_messages(session, target)
        else:
            group = _linked_group_for_target(session, target)
            if not group:
                raise ValueError("未找到关联群聊资产")
            inserted = collect_group_context(session, group)
            target.last_sync_at = _now()
            target.updated_at = _now()
            session.flush()
        audit(session, tenant_id=target.tenant_id, actor=actor, action="同步目标消息", target_type="operation_target", target_id=str(target.id), detail=f"inserted={inserted}")
        session.commit()
    except Exception as exc:  # noqa: BLE001 - sync should report and preserve cached detail.
        session.rollback()
        sync_error = str(exc)
    return {"inserted": inserted, "detail": operation_target_detail(session, tenant_id, target_id, sync_error=sync_error)}


def create_operation_task(session: Session, payload: OperationTaskCreate, actor: str) -> OperationTask:
    if payload.task_type == "MESSAGE_SEND":
        target = session.get(OperationTarget, payload.target_id)
        if not target:
            raise ValueError("message send task requires target_id")
        if target.target_type == "channel" and not target.can_send:
            raise ValueError("频道无发帖权限")
        if not payload.content.strip():
            raise ValueError("message send task requires content or AI prompt")
    else:
        channel_message = session.get(ChannelMessage, payload.channel_message_id)
        if not channel_message:
            raise ValueError("channel task requires channel_message_id")
        if payload.task_type == "CHANNEL_REACTION" and not payload.reaction.strip():
            raise ValueError("channel reaction task requires reaction")
        if payload.task_type == "CHANNEL_REPLY" and not payload.content.strip():
            raise ValueError("channel reply task requires AI prompt")
    actual_quantity = resolve_actual_quantity(payload.quantity, payload.quantity_jitter_ratio)
    if payload.task_type == "CHANNEL_REPLY":
        content_mode = "ai"
    elif payload.task_type == "MESSAGE_SEND":
        content_mode = payload.content_mode
    else:
        content_mode = "literal"
    task = OperationTask(
        tenant_id=payload.tenant_id,
        task_type=payload.task_type,
        target_id=payload.target_id,
        channel_message_id=payload.channel_message_id,
        title=payload.title or payload.task_type,
        content=payload.content,
        reaction=payload.reaction,
        account_ids=_account_id_csv(payload.account_ids),
        quantity=payload.quantity,
        actual_quantity=actual_quantity,
        quantity_jitter_ratio=_jitter_ratio_percent(payload.quantity_jitter_ratio),
        content_mode=content_mode,
        interval_seconds=payload.interval_seconds,
    )
    session.add(task)
    session.flush()
    if not _task_accounts(session, task):
        _failure_attempt(
            session,
            task,
            "没有可用在线账号",
            failure_type=FailureType.ACCOUNT_UNAVAILABLE.value,
        )
        audit(session, tenant_id=task.tenant_id, actor=actor, action="创建运营任务", target_type="operation_task", target_id=str(task.id), detail=task.task_type)
        session.commit()
        session.refresh(task)
        return task

    try:
        contents = _content_plan_for_task(session, task)
    except Exception as exc:  # noqa: BLE001 - operator-facing task status.
        _failure_attempt(session, task, str(exc), notify_ai=True)
        audit(session, tenant_id=task.tenant_id, actor=actor, action="创建AI运营任务失败", target_type="operation_task", target_id=str(task.id), detail=str(exc))
        session.commit()
        session.refresh(task)
        return task

    _create_attempt_plan(session, task, contents)
    audit(session, tenant_id=task.tenant_id, actor=actor, action="创建运营任务", target_type="operation_task", target_id=str(task.id), detail=task.task_type)
    session.commit()
    session.refresh(task)
    return task


def filter_operation_tasks(session: Session, tenant_id: int = 1, status: str | None = None) -> list[OperationTask]:
    stmt = select(OperationTask).where(OperationTask.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(OperationTask.status == status)
    return list(session.scalars(stmt.order_by(OperationTask.id.desc())))


def list_operation_attempts(session: Session, tenant_id: int = 1, task_id: int | None = None) -> list[OperationTaskAttempt]:
    stmt = select(OperationTaskAttempt).where(OperationTaskAttempt.tenant_id == tenant_id)
    if task_id:
        stmt = stmt.where(OperationTaskAttempt.task_id == task_id)
    return list(session.scalars(stmt.order_by(OperationTaskAttempt.id.desc())))


def _task_accounts(session: Session, task: OperationTask) -> list[TgAccount]:
    ids = _parse_account_ids(task.account_ids)
    stmt = select(TgAccount).where(
        TgAccount.tenant_id == task.tenant_id,
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.deleted_at.is_(None),
    ).order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    if ids:
        stmt = stmt.where(TgAccount.id.in_(ids))
    return list(session.scalars(stmt))


def _channel_message_context(session: Session, task: OperationTask) -> tuple[ChannelMessage, OperationTarget]:
    message = session.get(ChannelMessage, task.channel_message_id)
    if not message:
        raise ValueError("channel message not found")
    channel = session.get(OperationTarget, message.channel_target_id)
    if not channel or channel.target_type != "channel":
        raise ValueError("channel target not found")
    return message, channel


def _execute_operation_attempt(
    session: Session,
    task: OperationTask,
    attempt: OperationTaskAttempt,
    target: OperationTarget | None,
    channel_message: ChannelMessage | None,
    channel: OperationTarget | None,
) -> tuple[bool, str, str]:
    account = session.get(TgAccount, attempt.account_id) if attempt.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        return False, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用"
    try:
        credentials = credentials_for_account(session, account)
        if task.task_type == "MESSAGE_SEND":
            if not target:
                raise ValueError("target not found")
            result = gateway.send_message_to_target(
                account.id,
                target.tg_peer_id,
                attempt.content,
                target.target_type,
                None,
                account.session_ciphertext,
                credentials,
            )
            ok = result.ok
            failure_type = result.failure_type or ""
            detail = result.detail or ""
            remote_id = result.remote_message_id or ""
        elif task.task_type == "CHANNEL_VIEW":
            assert channel_message and channel
            op = gateway.view_channel_message(account.id, channel.tg_peer_id, channel_message.message_id, account.session_ciphertext, credentials)
            ok = op.ok
            failure_type = op.failure_type
            detail = op.detail
            remote_id = ""
        elif task.task_type == "CHANNEL_REACTION":
            assert channel_message and channel
            op = gateway.send_channel_reaction(
                account.id,
                channel.tg_peer_id,
                channel_message.message_id,
                attempt.reaction or task.reaction,
                account.session_ciphertext,
                credentials,
            )
            ok = op.ok
            failure_type = op.failure_type
            detail = op.detail
            remote_id = ""
        else:
            assert channel_message and channel
            result = gateway.reply_channel_message(
                account.id,
                channel.tg_peer_id,
                channel_message.message_id,
                attempt.content,
                account.session_ciphertext,
                credentials,
            )
            ok = result.ok
            failure_type = result.failure_type or ""
            detail = result.detail or ""
            remote_id = result.remote_message_id or ""
    except Exception as exc:
        ok = False
        failure_type = FailureType.UNKNOWN.value
        detail = str(exc)
        remote_id = ""

    if ok:
        account.last_active_at = _now()
        attempt.status = TaskStatus.COMPLETED.value
        attempt.failure_type = ""
        attempt.failure_detail = ""
        attempt.remote_message_id = remote_id
    else:
        failure_type = failure_type or FailureType.UNKNOWN.value
        detail = detail or "执行失败"
        attempt.status = TaskStatus.FAILED.value
        attempt.failure_type = failure_type
        attempt.failure_detail = detail
        attempt.remote_message_id = remote_id
        if failure_type == FailureType.ACCOUNT_LIMITED.value:
            account.status = AccountStatus.LIMITED.value
            account.health_score = min(account.health_score, 55)
    attempt.executed_at = _now()
    return ok, attempt.failure_type, attempt.failure_detail


def _refresh_operation_task_status(session: Session, task: OperationTask, *, last_failure_type: str = "", last_failure_detail: str = "") -> None:
    attempts = list(session.scalars(select(OperationTaskAttempt).where(OperationTaskAttempt.task_id == task.id)))
    completed = sum(1 for attempt in attempts if attempt.status == TaskStatus.COMPLETED.value)
    failed = sum(1 for attempt in attempts if attempt.status == TaskStatus.FAILED.value)
    queued = sum(1 for attempt in attempts if attempt.status == TaskStatus.QUEUED.value)
    task.completed_count = completed
    if completed >= task.actual_quantity:
        task.status = TaskStatus.COMPLETED.value
        task.failure_type = ""
        task.failure_detail = ""
        task.executed_at = _now()
    elif queued:
        task.status = TaskStatus.RUNNING.value if completed or failed else TaskStatus.QUEUED.value
        if failed:
            task.failure_type = last_failure_type
            task.failure_detail = f"部分失败 {failed}/{task.actual_quantity}; {last_failure_detail}"
    elif completed:
        task.status = TaskStatus.FAILED.value
        task.failure_type = last_failure_type or FailureType.UNKNOWN.value
        task.failure_detail = f"部分成功 {completed}/{task.actual_quantity}; {last_failure_detail}"
        task.executed_at = _now()
    else:
        task.status = TaskStatus.FAILED.value
        task.failure_type = last_failure_type or FailureType.UNKNOWN.value
        task.failure_detail = last_failure_detail or "全部执行失败"
        task.executed_at = _now()


def dispatch_operation_task(session: Session, task_id: int, actor: str) -> OperationTask:
    task = session.get(OperationTask, task_id)
    if not task:
        raise ValueError("task not found")
    if task.status == TaskStatus.COMPLETED.value:
        return task
    last_failure_type = ""
    last_failure_detail = ""
    target = session.get(OperationTarget, task.target_id) if task.target_id else None
    channel_message: ChannelMessage | None = None
    channel: OperationTarget | None = None
    if task.task_type != "MESSAGE_SEND":
        channel_message, channel = _channel_message_context(session, task)
    due_attempts = list(
        session.scalars(
            select(OperationTaskAttempt)
            .where(
                OperationTaskAttempt.task_id == task.id,
                OperationTaskAttempt.status == TaskStatus.QUEUED.value,
                OperationTaskAttempt.scheduled_at <= _now(),
            )
            .order_by(OperationTaskAttempt.scheduled_at.asc(), OperationTaskAttempt.id.asc())
        )
    )
    task.status = TaskStatus.RUNNING.value
    processed = 0
    for attempt in due_attempts:
        ok, failure_type, failure_detail = _execute_operation_attempt(session, task, attempt, target, channel_message, channel)
        processed += 1
        if not ok:
            last_failure_type = failure_type
            last_failure_detail = failure_detail
    _refresh_operation_task_status(session, task, last_failure_type=last_failure_type, last_failure_detail=last_failure_detail)
    audit(session, tenant_id=task.tenant_id, actor=actor, action="执行运营任务", target_type="operation_task", target_id=str(task.id), detail=f"processed={processed}; completed={task.completed_count}/{task.actual_quantity}")
    session.commit()
    session.refresh(task)
    return task


def retry_operation_task(session: Session, task_id: int, actor: str) -> OperationTask:
    task = session.get(OperationTask, task_id)
    if not task:
        raise ValueError("task not found")
    if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
        return task
    task.status = TaskStatus.QUEUED.value
    task.failure_type = ""
    task.failure_detail = ""
    task.executed_at = None
    attempts = list(session.scalars(select(OperationTaskAttempt).where(OperationTaskAttempt.task_id == task.id)))
    planning_failed = bool(attempts) and all(not attempt.account_id and not attempt.content for attempt in attempts)
    if planning_failed:
        for attempt in attempts:
            session.delete(attempt)
        session.flush()
        if not _task_accounts(session, task):
            _failure_attempt(
                session,
                task,
                "没有可用在线账号",
                failure_type=FailureType.ACCOUNT_UNAVAILABLE.value,
            )
            audit(session, tenant_id=task.tenant_id, actor=actor, action="重试运营任务", target_type="operation_task", target_id=str(task.id))
            session.commit()
            session.refresh(task)
            return task
        try:
            contents = _content_plan_for_task(session, task)
            _create_attempt_plan(session, task, contents)
        except Exception as exc:  # noqa: BLE001 - retry should preserve operator-facing failure.
            _failure_attempt(session, task, str(exc), notify_ai=task.content_mode == "ai")
            audit(session, tenant_id=task.tenant_id, actor=actor, action="重试AI运营任务失败", target_type="operation_task", target_id=str(task.id), detail=str(exc))
            session.commit()
            session.refresh(task)
            return task
    else:
        for attempt in attempts:
            if attempt.status != TaskStatus.FAILED.value:
                continue
            attempt.status = TaskStatus.QUEUED.value
            attempt.failure_type = ""
            attempt.failure_detail = ""
            attempt.remote_message_id = ""
            attempt.scheduled_at = _now()
            attempt.planned_delay_seconds = 0
            attempt.executed_at = None
    audit(session, tenant_id=task.tenant_id, actor=actor, action="重试运营任务", target_type="operation_task", target_id=str(task.id))
    session.commit()
    return dispatch_operation_task(session, task_id, actor)


def cancel_operation_task(session: Session, task_id: int, actor: str) -> OperationTask:
    task = session.get(OperationTask, task_id)
    if not task:
        raise ValueError("task not found")
    if task.status in {TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value}:
        raise ValueError("only queued or failed tasks can be cancelled")
    task.status = TaskStatus.CANCELLED.value
    task.failure_type = ""
    task.failure_detail = ""
    for attempt in session.scalars(select(OperationTaskAttempt).where(OperationTaskAttempt.task_id == task.id, OperationTaskAttempt.status == TaskStatus.QUEUED.value)):
        attempt.status = TaskStatus.CANCELLED.value
    audit(session, tenant_id=task.tenant_id, actor=actor, action="取消运营任务", target_type="operation_task", target_id=str(task.id))
    session.commit()
    session.refresh(task)
    return task


def manual_send(session: Session, account_id: int, payload: ManualSendRequest, actor: str) -> ManualOperationRecord:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        raise ValueError("账号不可用")
    target = session.get(OperationTarget, payload.target_id)
    if not target:
        raise ValueError("target not found")
    if target.target_type == "channel" and not target.can_send:
        record = ManualOperationRecord(
            tenant_id=account.tenant_id,
            account_id=account.id,
            target_id=target.id,
            operation_type="MESSAGE_SEND",
            content=payload.content,
            status=TaskStatus.FAILED.value,
            failure_type=FailureType.CHANNEL_POST_DENIED.value,
            failure_detail="频道无发帖权限",
            actor=actor,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record
    credentials = credentials_for_account(session, account)
    result = gateway.send_message_to_target(
        account.id,
        target.tg_peer_id,
        payload.content,
        target.target_type,
        None,
        account.session_ciphertext,
        credentials,
    )
    record = ManualOperationRecord(
        tenant_id=account.tenant_id,
        account_id=account.id,
        target_id=target.id,
        operation_type="MESSAGE_SEND",
        content=payload.content,
        status=TaskStatus.COMPLETED.value if result.ok else TaskStatus.FAILED.value,
        failure_type=result.failure_type or "",
        failure_detail=result.detail or "",
        remote_message_id=result.remote_message_id or "",
        actor=actor,
    )
    if result.ok:
        account.last_active_at = _now()
    elif result.failure_type == FailureType.ACCOUNT_LIMITED.value:
        account.status = AccountStatus.LIMITED.value
    session.add(record)
    audit(session, tenant_id=account.tenant_id, actor=actor, action="账号立即发送", target_type="manual_operation", target_id=str(account.id), detail=target.title)
    session.commit()
    session.refresh(record)
    return record


def list_manual_operations(session: Session, tenant_id: int = 1, account_id: int | None = None) -> list[ManualOperationRecord]:
    stmt = select(ManualOperationRecord).where(ManualOperationRecord.tenant_id == tenant_id)
    if account_id:
        stmt = stmt.where(ManualOperationRecord.account_id == account_id)
    return list(session.scalars(stmt.order_by(ManualOperationRecord.id.desc()).limit(200)))


def drain_operation_tasks(session_factory, limit: int = 10) -> int:
    with session_factory() as session:
        task_ids = list(
            session.scalars(
                select(OperationTask.id)
                .join(OperationTaskAttempt, OperationTaskAttempt.task_id == OperationTask.id)
                .where(
                    OperationTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
                    OperationTaskAttempt.status == TaskStatus.QUEUED.value,
                    OperationTaskAttempt.scheduled_at <= _now(),
                )
                .distinct()
                .order_by(OperationTask.id.asc())
                .limit(limit)
            )
        )
    processed = 0
    for task_id in task_ids:
        with session_factory() as session:
            before = session.get(OperationTask, task_id)
            before_count = before.completed_count if before else 0
            task = dispatch_operation_task(session, task_id, "tg-worker")
            processed += max(1, task.completed_count - before_count)
    return processed


__all__ = [
    "cancel_operation_task",
    "create_channel_message",
    "create_operation_target",
    "create_operation_task",
    "dispatch_operation_task",
    "drain_operation_tasks",
    "ensure_operation_targets_from_legacy_groups",
    "filter_channel_messages",
    "filter_operation_targets",
    "filter_operation_tasks",
    "list_manual_operations",
    "list_operation_attempts",
    "manual_send",
    "operation_target_detail",
    "retry_operation_task",
    "sync_account_targets",
    "sync_operation_target_messages",
    "update_operation_target",
]
