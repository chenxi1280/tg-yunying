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
    ChannelMessageComment,
    FailureType,
    GroupArchive,
    GroupAuthStatus,
    ManualOperationRecord,
    MessageTask,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    Task,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import (
    ChannelMessageCreate,
    ManualSendRequest,
    OperationTargetCreate,
    OperationTargetAccountUpdate,
    OperationTargetAdmissionRetryRequest,
    OperationTargetUpdate,
    OperationTaskCreate,
)

from ._common import _now, ai_gateway, audit, gateway
from .ai_config import ai_provider_credentials, get_tenant_ai_setting
from .developer_apps import credentials_for_account
from .group_listeners import collect_group_context, recent_context_messages
from .notifications import notify_ai_failure
from .target_learning import (
    CHANNEL_COMMENT_SCENE as TARGET_CHANNEL_COMMENT_SCENE,
    GROUP_CHAT_SCENE as TARGET_GROUP_CHAT_SCENE,
    learning_profile_preview,
)
from .tenant_learning_samples import GROUP_CHAT_SCENE, record_channel_comment_sample
from .task_center.payloads import EnsureChannelMembershipPayload, create_membership_action


ADMISSION_RETRY_TASK_TYPE = "target_admission_retry"


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
    data = payload.model_dump()
    if data["target_type"] == "channel":
        data.update(_normalize_channel_target_input(data))
    existing = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == data["tenant_id"],
            OperationTarget.tg_peer_id == data["tg_peer_id"],
        )
    )
    if existing:
        raise ValueError("target already exists")
    target = OperationTarget(**data)
    session.add(target)
    session.flush()
    if target.target_type != "channel":
        _ensure_linked_group_for_target(session, target)
    audit(session, tenant_id=target.tenant_id, actor=actor, action="创建运营目标", target_type="operation_target", target_id=str(target.id), detail=target.title)
    session.commit()
    session.refresh(target)
    return target


def _normalize_channel_target_input(data: dict) -> dict:
    raw_peer = str(data.get("tg_peer_id") or "").strip()
    username = str(data.get("username") or "").strip().lstrip("@")
    normalized_peer = raw_peer
    if raw_peer.startswith("@"):
        username = raw_peer.lstrip("@")
        normalized_peer = username
    matched_link = False
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/"):
        if raw_peer.startswith(prefix):
            matched_link = True
            tail = raw_peer.split(prefix, 1)[1].split("?", 1)[0].strip("/")
            if tail and not tail.startswith(("+", "joinchat/")):
                username = tail
                normalized_peer = tail
            else:
                normalized_peer = f"{prefix}{tail}" if tail else raw_peer.split("?", 1)[0].strip("/")
            break
    if not matched_link and raw_peer.startswith("+"):
        normalized_peer = raw_peer.split("?", 1)[0].strip("/")
    data["tg_peer_id"] = normalized_peer
    data["username"] = username
    return data


def _ensure_linked_group_for_target(session: Session, target: OperationTarget) -> TgGroup:
    group = _linked_group_for_target(session, target)
    if group:
        group.title = target.title
        group.group_type = "channel" if target.target_type == "channel" else "supergroup"
        group.member_count = target.member_count
        group.auth_status = target.auth_status
        group.can_send = target.can_send
        return group
    group = TgGroup(
        tenant_id=target.tenant_id,
        tg_peer_id=target.tg_peer_id,
        title=target.title,
        group_type="channel" if target.target_type == "channel" else "supergroup",
        member_count=target.member_count,
        auth_status=target.auth_status,
        can_send=target.can_send,
    )
    session.add(group)
    session.flush()
    return group


TARGET_FIELDS = {"target_type", "tg_peer_id", "title", "username", "member_count", "can_send", "auth_status"}
GROUP_RISK_FIELDS = {
    "active_window",
    "daily_limit",
    "account_cooldown_seconds",
    "group_cooldown_seconds",
    "banned_words",
    "link_whitelist",
    "require_review",
}


def update_operation_target(session: Session, tenant_id: int, target_id: int, payload: OperationTargetUpdate, actor: str) -> OperationTarget:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id:
        raise ValueError("target not found")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key not in TARGET_FIELDS:
            continue
        setattr(target, key, value)
    if any(key in data for key in GROUP_RISK_FIELDS):
        linked_group = _linked_group_for_target(session, target)
        if not linked_group or linked_group.tenant_id != tenant_id or target.target_type != "group":
            raise ValueError("target group policy not found")
        for key in GROUP_RISK_FIELDS:
            if key in data:
                setattr(linked_group, key, data[key])
        linked_group.can_send = bool(target.can_send)
        linked_group.auth_status = target.auth_status
    target.updated_at = _now()
    audit(session, tenant_id=target.tenant_id, actor=actor, action="更新运营目标", target_type="operation_target", target_id=str(target.id), detail=target.title)
    session.commit()
    session.refresh(target)
    return target


def update_operation_target_account_policy(session: Session, tenant_id: int, target_id: int, account_id: int, payload: OperationTargetAccountUpdate, actor: str) -> dict:
    target = _operation_target_for_tenant(session, tenant_id, target_id)
    linked_group = _linked_group_for_target(session, target)
    if not linked_group or target.target_type != "group":
        raise ValueError("target group account policy not found")
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == linked_group.id,
            TgGroupAccount.account_id == account.id,
        )
    )
    if not link:
        raise ValueError("account not linked to target group")
    data = payload.model_dump(exclude_unset=True)
    if "permission_label" in data and data["permission_label"] is not None:
        link.permission_label = data["permission_label"]
    if "can_send" in data and data["can_send"] is not None:
        link.can_send = bool(data["can_send"])
    if "is_listener" in data and data["is_listener"] is not None:
        link.is_listener = bool(data["is_listener"])
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="更新目标账号风控",
        target_type="operation_target",
        target_id=str(target.id),
        detail=f"account={account.id}; can_send={link.can_send}; is_listener={link.is_listener}",
    )
    session.commit()
    return operation_target_detail(session, tenant_id, target_id)


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


def _target_type_from_group_type(group_type: str) -> str:
    return "channel" if group_type == "channel" else "group"


def _upsert_group_target_from_snapshot(session: Session, account: TgAccount, snapshot) -> OperationTarget:
    now_value = _now()
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == account.tenant_id,
            TgGroup.tg_peer_id == snapshot.tg_peer_id,
        )
    )
    if group is None:
        group = TgGroup(
            tenant_id=account.tenant_id,
            tg_peer_id=snapshot.tg_peer_id,
            title=snapshot.title,
            group_type=snapshot.group_type,
            member_count=snapshot.member_count,
        )
        session.add(group)
        session.flush()
    group.title = snapshot.title
    group.group_type = snapshot.group_type
    group.member_count = snapshot.member_count

    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == account.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account.id,
        )
    )
    if link is None:
        link = TgGroupAccount(
            tenant_id=account.tenant_id,
            group_id=group.id,
            account_id=account.id,
        )
        session.add(link)
    link.permission_label = snapshot.permission_label
    link.can_send = bool(snapshot.can_send)

    all_links = list(
        session.scalars(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == account.tenant_id,
                TgGroupAccount.group_id == group.id,
            )
        )
    )
    group.can_send = any(item.can_send for item in all_links)
    if group.can_send:
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
    elif group.auth_status == GroupAuthStatus.AUTHORIZED.value:
        group.auth_status = GroupAuthStatus.READONLY.value

    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == account.tenant_id,
            OperationTarget.tg_peer_id == snapshot.tg_peer_id,
        )
    )
    if target is None:
        target = OperationTarget(
            tenant_id=account.tenant_id,
            tg_peer_id=snapshot.tg_peer_id,
        )
        session.add(target)
    target.target_type = _target_type_from_group_type(snapshot.group_type)
    target.title = snapshot.title
    target.username = snapshot.username or ""
    target.member_count = snapshot.member_count
    target.can_send = group.can_send
    target.auth_status = GroupAuthStatus.AUTHORIZED.value if group.can_send else GroupAuthStatus.READONLY.value
    target.last_sync_at = now_value
    target.updated_at = now_value
    return target


def sync_account_targets(session: Session, account_id: int, actor: str) -> list[OperationTarget]:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    credentials = credentials_for_account(session, account)
    snapshots = gateway.list_groups(account.id, account.session_ciphertext, credentials)
    targets: list[OperationTarget] = []
    for snapshot in snapshots:
        targets.append(_upsert_group_target_from_snapshot(session, account, snapshot))
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步群频道目标", target_type="tg_account", target_id=str(account.id), detail=f"targets={len(targets)}")
    session.commit()
    return filter_operation_targets(session, account.tenant_id)


def sync_all_operation_targets(session: Session, tenant_id: int, actor: str) -> dict:
    accounts = list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
            )
            .order_by(TgAccount.id.asc())
        )
    )
    synced_accounts = 0
    failures: list[dict] = []
    target_total = 0
    for account in accounts:
        try:
            before_count = len(filter_operation_targets(session, tenant_id))
            targets = sync_account_targets(session, account.id, actor)
            target_total = len(targets)
            synced_accounts += 1
            after_count = len(targets)
            audit(
                session,
                tenant_id=tenant_id,
                actor=actor,
                action="全量同步账号目标",
                target_type="tg_account",
                target_id=str(account.id),
                detail=f"targets_before={before_count}; targets_after={after_count}",
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - keep syncing the remaining accounts.
            session.rollback()
            failures.append({"account_id": account.id, "display_name": account.display_name, "error": str(exc)})
    targets = filter_operation_targets(session, tenant_id)
    return {
        "synced_accounts": synced_accounts,
        "failed_accounts": failures,
        "target_count": target_total or len(targets),
        "targets": targets,
    }


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


def filter_channel_message_comments(
    session: Session,
    tenant_id: int = 1,
    channel_target_id: int | None = None,
    channel_message_id: int | None = None,
) -> list[ChannelMessageComment]:
    stmt = select(ChannelMessageComment).where(ChannelMessageComment.tenant_id == tenant_id)
    if channel_target_id:
        stmt = stmt.where(ChannelMessageComment.channel_target_id == channel_target_id)
    if channel_message_id:
        stmt = stmt.where(ChannelMessageComment.channel_message_id == channel_message_id)
    return list(session.scalars(stmt.order_by(ChannelMessageComment.published_at.desc().nullslast(), ChannelMessageComment.id.desc())))


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
    all_links = links_by_group.get(linked_group.id if linked_group else 0, [])
    send_links = [link for link in all_links if link.can_send]
    listener_links = [link for link in links_by_group.get(linked_group.id if linked_group else 0, []) if link.is_listener]
    task_capabilities = _operation_target_task_capabilities(target, linked_group, send_links, listener_links)
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
        "can_archive": bool(linked_group and target.target_type == "group" and target.auth_status == GroupAuthStatus.AUTHORIZED.value),
        "can_task": bool(task_capabilities),
        "task_capabilities": task_capabilities,
        "available_send_account_count": len(all_links) if target.target_type == "channel" else len(send_links),
        "listener_account_count": len(listener_links),
        "last_sync_at": target.last_sync_at,
        "created_at": target.created_at,
        "updated_at": target.updated_at,
    }


def _operation_target_task_capabilities(target: OperationTarget, linked_group: TgGroup | None, send_links: list[TgGroupAccount], listener_links: list[TgGroupAccount]) -> list[str]:
    if target.target_type == "channel" and target.auth_status == GroupAuthStatus.UNVERIFIED.value:
        return ["频道浏览", "频道点赞", "频道评论/回复"]
    if target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return []
    if target.target_type == "channel":
        return ["频道浏览", "频道点赞", "频道评论/回复"]
    capabilities: list[str] = []
    if linked_group and target.can_send and send_links:
        capabilities.append("AI 活跃群")
    if linked_group and listener_links:
        capabilities.append("转发监听源群")
    if linked_group and target.can_send and send_links:
        capabilities.append("转发目标群")
    if target.target_type == "group":
        capabilities.append("群归档")
    return capabilities


def _group_accounts_for_detail(session: Session, group: TgGroup, links: list[TgGroupAccount] | None = None) -> list[dict]:
    group_links = links
    if group_links is None:
        group_links = list(
            session.scalars(
                select(TgGroupAccount)
                .where(TgGroupAccount.tenant_id == group.tenant_id, TgGroupAccount.group_id == group.id)
                .order_by(TgGroupAccount.id.asc())
            )
        )
    account_ids = [link.account_id for link in group_links]
    if not account_ids:
        return []
    account_rows = list(
        session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == group.tenant_id,
                TgAccount.id.in_(account_ids),
                TgAccount.deleted_at.is_(None),
            )
        )
    )
    accounts_by_id = {account.id: account for account in account_rows}
    accounts: list[dict] = []
    for link in group_links:
        account = accounts_by_id.get(link.account_id)
        if not account:
            continue
        admission_status = "ready" if link.can_send and account.status == AccountStatus.ACTIVE.value else "failed"
        admission_reason = ""
        if admission_status == "failed":
            if account.status != AccountStatus.ACTIVE.value:
                admission_reason = f"账号状态为 {account.status}"
            else:
                admission_reason = link.permission_label or "未满足发送准入"
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
                "admission_status": admission_status,
                "admission_failure_reason": admission_reason,
                "admission_retryable": admission_status == "failed" and account.status == AccountStatus.ACTIVE.value,
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


def _task_targets_target(task: Task, target: OperationTarget, linked_group: TgGroup | None) -> bool:
    config = task.type_config or {}
    if target.target_type == "channel":
        return int(config.get("target_channel_id") or 0) == target.id
    if not linked_group:
        return False
    group_id = linked_group.id
    if int(config.get("target_group_id") or 0) == group_id:
        return True
    target_ids = {int(item) for item in config.get("target_group_ids") or [] if str(item).isdigit()}
    source_groups = config.get("source_groups") or []
    return group_id in target_ids or any(int(item.get("group_id") or 0) == group_id for item in source_groups if isinstance(item, dict))


def _task_history_for_detail(session: Session, target: OperationTarget, linked_group: TgGroup | None) -> list[dict]:
    rows = list(
        session.scalars(
            select(Task)
            .where(Task.tenant_id == target.tenant_id, Task.deleted_at.is_(None))
            .order_by(Task.updated_at.desc())
            .limit(100)
        )
    )
    result = []
    for task in rows:
        if not _task_targets_target(task, target, linked_group):
            continue
        stats = task.stats or {}
        result.append(
            {
                "id": task.id,
                "name": task.name,
                "type": task.type,
                "status": task.status,
                "success_count": int(stats.get("success_count") or 0),
                "failure_count": int(stats.get("failure_count") or 0),
                "updated_at": task.updated_at,
            }
        )
        if len(result) >= 10:
            break
    return result


def _send_records_for_detail(session: Session, target: OperationTarget) -> list[dict]:
    rows = list(
        session.scalars(
            select(MessageTask)
            .where(
                MessageTask.tenant_id == target.tenant_id,
                MessageTask.target_peer_id == target.tg_peer_id,
            )
            .order_by(MessageTask.created_at.desc())
            .limit(10)
        )
    )
    return [
        {
            "id": item.id,
            "content": item.content,
            "status": item.status,
            "account_id": item.account_id,
            "failure_detail": item.failure_detail or "",
            "sent_at": item.sent_at,
            "created_at": item.created_at,
        }
        for item in rows
    ]


def _archive_records_for_detail(session: Session, target: OperationTarget, linked_group: TgGroup | None) -> list[dict]:
    if not linked_group:
        return []
    rows = list(
        session.scalars(
            select(GroupArchive)
            .where(GroupArchive.tenant_id == target.tenant_id, GroupArchive.group_id == linked_group.id)
            .order_by(GroupArchive.created_at.desc())
            .limit(10)
        )
    )
    return [
        {
            "id": item.id,
            "title": item.title,
            "status": item.status,
            "message_count": item.message_count,
            "member_count": item.member_count,
            "failure_detail": item.failure_detail,
            "created_at": item.created_at,
        }
        for item in rows
    ]


def _risk_for_detail(target: OperationTarget, accounts: list[dict], linked_group: TgGroup | None) -> dict:
    messages: list[str] = []
    if not target.can_send:
        messages.append("目标当前标记为只读，发送类任务会受限。")
    if target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        messages.append(f"授权状态为 {target.auth_status}，需要确认是否允许运营。")
    send_accounts = [item for item in accounts if item["can_send"] and item["status"] == AccountStatus.ACTIVE.value]
    if target.target_type == "group" and not send_accounts:
        messages.append("没有可用发送账号覆盖该群。")
    if linked_group and linked_group.listener_enabled and not any(item["is_listener"] for item in accounts):
        messages.append("群已启用监听，但未找到监听账号。")
    level = "高风险" if any("没有" in item or "只读" in item for item in messages) else ("需关注" if messages else "正常")
    return {"level": level, "messages": messages}


def _refresh_target_capability_from_group(session: Session, target: OperationTarget, group: TgGroup) -> None:
    links = list(
        session.scalars(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == target.tenant_id,
                TgGroupAccount.group_id == group.id,
            )
        )
    )
    group.can_send = any(link.can_send for link in links)
    group.auth_status = GroupAuthStatus.AUTHORIZED.value if group.can_send else GroupAuthStatus.READONLY.value
    target.can_send = group.can_send
    target.auth_status = group.auth_status
    target.member_count = group.member_count
    target.last_sync_at = _now()
    target.updated_at = _now()


def _ensure_target_group_link(session: Session, target: OperationTarget) -> TgGroup:
    group = _linked_group_for_target(session, target)
    if group:
        return group
    group = TgGroup(
        tenant_id=target.tenant_id,
        tg_peer_id=target.tg_peer_id,
        title=target.title,
        group_type="channel" if target.target_type == "channel" else "supergroup",
        member_count=target.member_count,
        auth_status=target.auth_status,
        can_send=target.can_send,
    )
    session.add(group)
    session.flush()
    return group


def _admission_retry_requested_ids(session: Session, tenant_id: int, group: TgGroup, requested_ids: list[int]) -> list[int]:
    normalized = list(dict.fromkeys(int(item) for item in requested_ids if int(item) > 0))
    if normalized:
        return normalized
    return list(
        session.scalars(
            select(TgGroupAccount.account_id)
            .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
            .where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(False),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.deleted_at.is_(None),
            )
            .order_by(TgGroupAccount.account_id.asc())
        )
    )


def _admission_retry_accounts(session: Session, tenant_id: int, account_ids: list[int]) -> tuple[list[TgAccount], list[str]]:
    accounts: list[TgAccount] = []
    failures: list[str] = []
    for account_id in account_ids:
        account = session.get(TgAccount, account_id)
        if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
            failures.append(f"{account_id}:账号不存在")
            continue
        if account.status != AccountStatus.ACTIVE.value:
            failures.append(f"{account.id}:账号状态为 {account.status}")
            continue
        accounts.append(account)
    return accounts, failures


def _admission_retry_payload(target: OperationTarget) -> EnsureChannelMembershipPayload:
    return EnsureChannelMembershipPayload(
        channel_id=target.tg_peer_id,
        channel_target_id=target.id,
        target_type=target.target_type,
        target_display=target.title,
        target_username=target.username or "",
        require_send=target.target_type == "group",
    )


def _create_admission_retry_task(session: Session, tenant_id: int, target: OperationTarget, accounts: list[TgAccount], actor: str, reason: str) -> Task:
    task = Task(
        id=str(uuid4()),
        tenant_id=tenant_id,
        name=f"重试目标准入：{target.title}",
        type=ADMISSION_RETRY_TASK_TYPE,
        status="running",
        account_config={"account_ids": [account.id for account in accounts]},
        type_config={"target_operation_target_id": target.id, "target_type": target.target_type},
        stats={"admission_retry_reason": reason, "created_by": actor, "queued_account_count": len(accounts)},
    )
    session.add(task)
    session.flush()
    return task


def _queue_admission_retry_actions(session: Session, task: Task, target: OperationTarget, accounts: list[TgAccount]) -> int:
    payload = _admission_retry_payload(target)
    action_ids: set[str] = set()
    for account in accounts:
        action = create_membership_action(session, task, account.id, _now(), payload)
        action_ids.add(action.id)
    return len(action_ids)


def retry_operation_target_admission(
    session: Session,
    tenant_id: int,
    target_id: int,
    payload: OperationTargetAdmissionRetryRequest,
    actor: str,
) -> dict:
    target = _operation_target_for_tenant(session, tenant_id, target_id)
    operator_reason = payload.reason.strip()
    if not operator_reason:
        raise ValueError("重试原因不能为空")
    group = _ensure_target_group_link(session, target)
    requested_ids = _admission_retry_requested_ids(session, tenant_id, group, payload.account_ids)
    if not requested_ids:
        raise ValueError("no failed admission accounts")
    accounts, failure_details = _admission_retry_accounts(session, tenant_id, requested_ids)
    retry_task = _create_admission_retry_task(session, tenant_id, target, accounts, actor, operator_reason)
    queued = _queue_admission_retry_actions(session, retry_task, target, accounts)
    summary = {
        "mode": "queued",
        "task_id": retry_task.id,
        "retried_account_count": len(accounts),
        "queued_action_count": queued,
        "recovered_account_count": 0,
        "failed_account_count": len(failure_details),
        "failure_details": failure_details,
    }
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="重试目标准入",
        target_type="operation_target",
        target_id=str(target.id),
        detail=(
            f"reason={operator_reason}; retried={summary['retried_account_count']}; "
            f"queued={summary['queued_action_count']}; failed={summary['failed_account_count']}"
        ),
    )
    session.commit()
    return operation_target_detail(session, tenant_id, target_id, admission_retry=summary)


def operation_target_detail(
    session: Session,
    tenant_id: int,
    target_id: int,
    *,
    sync_error: str = "",
    admission_retry: dict | None = None,
    include_learning_profile: bool = False,
) -> dict:
    target = _operation_target_for_tenant(session, tenant_id, target_id)
    linked_group = _linked_group_for_target(session, target)
    group_links = (
        list(
            session.scalars(
                select(TgGroupAccount).where(
                    TgGroupAccount.tenant_id == tenant_id,
                    TgGroupAccount.group_id == linked_group.id,
                )
            )
        )
        if linked_group
        else []
    )
    accounts = _group_accounts_for_detail(session, linked_group, group_links) if linked_group else []
    group_messages = _group_messages_for_detail(session, linked_group) if linked_group and target.target_type == "group" else []
    channel_messages = filter_channel_messages(session, tenant_id, target.id) if target.target_type == "channel" else []
    channel_comments = filter_channel_message_comments(session, tenant_id, target.id) if target.target_type == "channel" else []
    task_history = _task_history_for_detail(session, target, linked_group)
    send_records = _send_records_for_detail(session, target)
    archive_records = _archive_records_for_detail(session, target, linked_group) if target.target_type == "group" else []
    risk = _risk_for_detail(target, accounts, linked_group)
    learning_preview = _learning_preview_for_detail(
        session,
        tenant_id,
        target,
        include_learning_profile=include_learning_profile,
    )
    return {
        "target": _operation_target_list_payload(target, linked_group, {linked_group.id: group_links} if linked_group else {}),
        "linked_group": (
            {
                "id": linked_group.id,
                "title": linked_group.title,
                "group_type": linked_group.group_type,
                "member_count": linked_group.member_count,
                "auth_status": linked_group.auth_status,
                "can_send": linked_group.can_send,
                "active_window": linked_group.active_window,
                "daily_limit": linked_group.daily_limit,
                "account_cooldown_seconds": linked_group.account_cooldown_seconds,
                "group_cooldown_seconds": linked_group.group_cooldown_seconds,
                "banned_words": linked_group.banned_words,
                "link_whitelist": linked_group.link_whitelist,
                "require_review": linked_group.require_review,
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
        "channel_comments": channel_comments,
        "task_history": task_history,
        "send_records": send_records,
        "archive_records": archive_records,
        "learning_profile_preview": learning_preview,
        "risk": risk,
        "sync_error": sync_error,
        "stats": {
            "available_accounts": sum(1 for item in accounts if item["can_send"] and item["status"] == AccountStatus.ACTIVE.value),
            "admission_failed_accounts": sum(1 for item in accounts if item["admission_status"] == "failed"),
            "listener_accounts": sum(1 for item in accounts if item["is_listener"]),
            "group_messages": len(group_messages),
            "channel_messages": len(channel_messages),
            "channel_comments": len(channel_comments),
            "task_history": len(task_history),
            "send_records": len(send_records),
            "archive_records": len(archive_records),
        },
        "admission_retry": admission_retry or {},
    }


def _channel_message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    if channel.tg_peer_id.startswith("-100") and channel.tg_peer_id[4:].isdigit():
        return f"https://t.me/c/{channel.tg_peer_id[4:]}/{message_id}"
    return ""


def _learning_preview_for_detail(
    session: Session,
    tenant_id: int,
    target: OperationTarget,
    *,
    include_learning_profile: bool,
) -> dict:
    if not include_learning_profile:
        return {}
    profile_scene = TARGET_CHANNEL_COMMENT_SCENE if target.target_type == "channel" else TARGET_GROUP_CHAT_SCENE
    return learning_profile_preview(session, tenant_id, target.id, profile_scene)


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
            existing.comment_available = bool(snapshot.comment_available)
            existing.published_at = published_at or existing.published_at
            continue
        session.add(
            ChannelMessage(
                tenant_id=target.tenant_id,
                channel_target_id=target.id,
                message_id=message_id,
                message_url=snapshot.message_url or _channel_message_url(target, message_id),
                content_preview=snapshot.content_preview,
                comment_available=bool(snapshot.comment_available),
                published_at=published_at,
            )
        )
        inserted += 1
    target.last_sync_at = _now()
    target.updated_at = _now()
    session.flush()
    return inserted


def _sync_channel_message_comments(session: Session, message: ChannelMessage, *, limit: int = 100) -> int:
    target = session.get(OperationTarget, message.channel_target_id)
    if not target or target.tenant_id != message.tenant_id or target.target_type != "channel":
        raise ValueError("channel target not found")
    account = _channel_sync_account(session, message.tenant_id)
    if not account:
        raise ValueError("没有可用于采集频道评论的在线账号")
    snapshots = gateway.fetch_channel_comments(
        account.id,
        target.tg_peer_id,
        message.message_id,
        account.session_ciphertext,
        credentials_for_account(session, account),
        limit=limit,
    )
    snapshot_comment_ids = {int(snapshot.comment_message_id or 0) for snapshot in snapshots if int(snapshot.comment_message_id or 0) > 0}
    known_comment_ids = snapshot_comment_ids | set(
        session.scalars(
            select(ChannelMessageComment.comment_message_id).where(
                ChannelMessageComment.tenant_id == message.tenant_id,
                ChannelMessageComment.channel_target_id == message.channel_target_id,
                ChannelMessageComment.channel_message_id == message.id,
            )
        )
    )
    inserted = 0
    for snapshot in snapshots:
        comment_message_id = int(snapshot.comment_message_id or 0)
        if comment_message_id <= 0:
            continue
        raw_parent_id = int(snapshot.parent_comment_message_id or 0)
        parent_comment_message_id = raw_parent_id if raw_parent_id in known_comment_ids and raw_parent_id != comment_message_id else None
        existing = session.scalar(
            select(ChannelMessageComment).where(
                ChannelMessageComment.tenant_id == message.tenant_id,
                ChannelMessageComment.channel_target_id == message.channel_target_id,
                ChannelMessageComment.channel_message_id == message.id,
                ChannelMessageComment.comment_message_id == comment_message_id,
            )
        )
        published_at = _normalize_snapshot_datetime(snapshot.published_at)
        if existing:
            existing.parent_comment_message_id = parent_comment_message_id
            existing.author_peer_id = snapshot.author_peer_id or existing.author_peer_id
            existing.author_username = str(getattr(snapshot, "author_username", "") or existing.author_username).lstrip("@")
            existing.author_name = snapshot.author_name or existing.author_name
            existing.is_bot = bool(getattr(snapshot, "is_bot", False) or existing.is_bot)
            existing.content_preview = snapshot.content_preview or existing.content_preview
            existing.reply_count = int(snapshot.reply_count or existing.reply_count or 0)
            existing.published_at = published_at or existing.published_at
            record_channel_comment_sample(session, existing)
            continue
        comment = ChannelMessageComment(
            tenant_id=message.tenant_id,
            channel_target_id=message.channel_target_id,
            channel_message_id=message.id,
            comment_message_id=comment_message_id,
            parent_comment_message_id=parent_comment_message_id,
            author_peer_id=snapshot.author_peer_id,
            author_username=str(getattr(snapshot, "author_username", "") or "").lstrip("@"),
            author_name=snapshot.author_name,
            is_bot=bool(getattr(snapshot, "is_bot", False)),
            content_preview=snapshot.content_preview,
            reply_count=int(snapshot.reply_count or 0),
            published_at=published_at,
        )
        session.add(comment)
        session.flush()
        record_channel_comment_sample(session, comment)
        inserted += 1
    target.last_sync_at = _now()
    target.updated_at = _now()
    session.flush()
    return inserted


def sync_channel_message_comments(session: Session, tenant_id: int, channel_message_id: int, actor: str, *, limit: int = 100) -> dict:
    message = session.get(ChannelMessage, channel_message_id)
    if not message or message.tenant_id != tenant_id:
        raise ValueError("channel message not found")
    inserted = 0
    sync_error = ""
    try:
        inserted = _sync_channel_message_comments(session, message, limit=limit)
        audit(
            session,
            tenant_id=message.tenant_id,
            actor=actor,
            action="同步频道评论",
            target_type="channel_message",
            target_id=str(message.id),
            detail=f"message_id={message.message_id}; inserted={inserted}",
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - sync should report and preserve cached comments.
        session.rollback()
        sync_error = str(exc)
    return {
        "inserted": inserted,
        "comments": filter_channel_message_comments(session, tenant_id, message.channel_target_id, message.id),
        "sync_error": sync_error,
    }


def sync_operation_target_messages(session: Session, tenant_id: int, target_id: int, actor: str, *, include_learning_profile: bool = False) -> dict:
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
            inserted = collect_group_context(session, group, create_source_media=False, learning_scene=GROUP_CHAT_SCENE)
            target.last_sync_at = _now()
            target.updated_at = _now()
            session.flush()
        audit(session, tenant_id=target.tenant_id, actor=actor, action="同步目标消息", target_type="operation_target", target_id=str(target.id), detail=f"inserted={inserted}")
        session.commit()
    except Exception as exc:  # noqa: BLE001 - sync should report and preserve cached detail.
        session.rollback()
        sync_error = str(exc)
    return {
        "inserted": inserted,
        "detail": operation_target_detail(
            session,
            tenant_id,
            target_id,
            sync_error=sync_error,
            include_learning_profile=include_learning_profile,
        ),
    }


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
    accounts = list(session.scalars(stmt))
    if task.task_type in {"CHANNEL_VIEW", "CHANNEL_REACTION", "CHANNEL_REPLY"}:
        try:
            _message, channel = _channel_message_context(session, task)
        except ValueError:
            return []
        group = _linked_group_for_target(session, channel)
        if not group:
            return []
        member_ids = {
            int(account_id)
            for account_id in session.scalars(
                select(TgGroupAccount.account_id).where(
                    TgGroupAccount.tenant_id == task.tenant_id,
                    TgGroupAccount.group_id == group.id,
                )
            )
        }
        return [account for account in accounts if account.id in member_ids]
    return accounts


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
    if task.task_type in {"CHANNEL_VIEW", "CHANNEL_REACTION", "CHANNEL_REPLY"}:
        if not channel or not _legacy_channel_account_has_membership(session, task.tenant_id, account.id, channel):
            return False, FailureType.ACCOUNT_UNAVAILABLE.value, "账号未关注目标频道，已拦截频道互动"
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


def _legacy_channel_account_has_membership(session: Session, tenant_id: int, account_id: int, channel: OperationTarget) -> bool:
    group = _linked_group_for_target(session, channel)
    if not group:
        return False
    return bool(
        session.scalar(
            select(TgGroupAccount.id).where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.account_id == account_id,
            )
        )
    )


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
    "filter_channel_message_comments",
    "filter_channel_messages",
    "filter_operation_targets",
    "filter_operation_tasks",
    "list_manual_operations",
    "list_operation_attempts",
    "manual_send",
    "operation_target_detail",
    "retry_operation_target_admission",
    "retry_operation_task",
    "sync_account_targets",
    "sync_all_operation_targets",
    "sync_channel_message_comments",
    "sync_operation_target_messages",
    "update_operation_target_account_policy",
    "update_operation_target",
]
