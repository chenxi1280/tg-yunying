"""Read-only Telegram reachability and permission probe for AI-group targets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from telethon import errors, functions

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import Task, TgAccount, TgGroup
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account


@dataclass(frozen=True)
class ProbeTarget:
    task_name: str
    group_title: str
    reference: str


@dataclass(frozen=True)
class ProbeResult:
    target: ProbeTarget
    group_type: str
    can_send_plain: bool
    can_send_links: bool
    summary: str


async def main() -> None:
    with SessionLocal() as session:
        targets = _active_targets(session)
        account = _probe_account(session)
        if account is None:
            raise ValueError("未找到状态为在线且存在 Session 的探测账号")
        client = await _client_for_account(session, account)
        me = await client.get_me()
        print(f"只读探测账号 ID={account.id}，TG ID={me.id}")
        results = [
            await _probe_target(client, me=me, target=target)
            for target in targets
        ]
        _print_results(results)


def _active_targets(session) -> list[ProbeTarget]:
    tasks = session.scalars(
        select(Task).where(
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
        )
    )
    targets: dict[str, ProbeTarget] = {}
    for task in tasks:
        config = task.type_config or {}
        group_id = config.get("target_group_id") or config.get("group_id")
        group = session.get(TgGroup, group_id) if group_id else None
        reference = _target_reference(config, group)
        if not reference:
            continue
        targets[reference] = ProbeTarget(
            task_name=task.name,
            group_title=group.title if group else task.name,
            reference=reference,
        )
    print(f"独立目标群数量: {len(targets)}")
    return list(targets.values())


def _target_reference(config: dict, group: TgGroup | None) -> str:
    configured = (
        config.get("group_url")
        or config.get("target_url")
        or config.get("channel_id")
    )
    if configured:
        return str(configured)
    if group and group.tg_peer_id:
        return str(group.tg_peer_id)
    if group and group.username:
        return f"@{group.username}"
    return ""


def _probe_account(session) -> TgAccount | None:
    return session.scalar(
        select(TgAccount)
        .where(
            TgAccount.status == "在线",
            TgAccount.session_ciphertext != "",
        )
        .order_by(TgAccount.id.asc())
        .limit(1)
    )


async def _client_for_account(session, account: TgAccount):
    raw_session = decrypt_session(account.session_ciphertext)
    credentials = credentials_for_account(session, account)
    gateway = TelethonTelegramGateway()
    return await gateway._get_or_create_client(credentials, raw_session)


async def _probe_target(client, *, me, target: ProbeTarget) -> ProbeResult:
    try:
        entity = await client.get_entity(target.reference)
    except errors.UsernameNotOccupiedError:
        return _failed_result(target, "目标用户名不存在")
    except errors.InviteHashExpiredError:
        return _failed_result(target, "邀请链接已失效")
    except Exception as exc:
        return _failed_result(target, f"实体解析失败: {type(exc).__name__}: {exc}")
    group_type = "频道" if getattr(entity, "broadcast", False) else "群组"
    permissions = _default_permissions(entity)
    membership = await _membership_summary(client, entity=entity, me=me)
    summary = _permission_summary(
        group_type=group_type,
        membership=membership,
        can_send_plain=permissions[0],
        can_send_links=permissions[1],
    )
    await _print_full_channel_summary(client, entity)
    return ProbeResult(target, group_type, permissions[0], permissions[1], summary)


def _failed_result(target: ProbeTarget, summary: str) -> ProbeResult:
    return ProbeResult(target, "未知", False, False, summary)


def _default_permissions(entity) -> tuple[bool, bool]:
    rights = getattr(entity, "default_banned_rights", None)
    if rights is None:
        return True, True
    return not bool(rights.send_messages), not bool(rights.embed_links)


async def _membership_summary(client, *, entity, me) -> str:
    try:
        await client(
            functions.channels.GetParticipantRequest(
                channel=entity,
                participant=me,
            )
        )
        return "已在群内"
    except errors.UserNotParticipantError:
        return "尚未加入"
    except Exception as exc:
        return f"成员状态未知:{type(exc).__name__}"


def _permission_summary(
    *,
    group_type: str,
    membership: str,
    can_send_plain: bool,
    can_send_links: bool,
) -> str:
    if group_type == "频道":
        return "目标为单向广播频道"
    if membership != "已在群内":
        return membership
    if not can_send_plain:
        return "群默认禁止普通成员发言"
    if not can_send_links:
        return "可发纯文本但群默认禁止链接预览"
    return "群组可访问且默认允许纯文本"


async def _print_full_channel_summary(client, entity) -> None:
    try:
        result = await client(functions.channels.GetFullChannelRequest(channel=entity))
    except Exception as exc:
        print(f"目标 {getattr(entity, 'id', '')} 详情读取失败: {exc}")
        return
    full = result.full_chat
    print(
        f"目标 {getattr(entity, 'id', '')}: members="
        f"{getattr(full, 'participants_count', 'unknown')}, "
        f"slowmode={getattr(full, 'slowmode_seconds', 0)}"
    )


def _print_results(results: list[ProbeResult]) -> None:
    print("\n群聊只读探测汇总")
    for result in results:
        print(
            f"{result.target.group_title} | {result.target.task_name} | "
            f"{result.group_type} | plain={result.can_send_plain} | "
            f"links={result.can_send_links} | {result.summary}"
        )


if __name__ == "__main__":
    asyncio.run(main())
