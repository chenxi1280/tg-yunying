"""Read-only comparison of platform accounts and Telegram group administrators."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from telethon import errors, functions, types

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import Task, TaskMembershipAdmissionItem, TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account


DEFAULT_GROUP_PEER = "@yuebao8"
DEFAULT_TASK_QUERY = "天津一品楼"
MAX_ACCOUNT_PROBES = 20


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读检查群管理员与平台账号交集")
    parser.add_argument("--group", default=DEFAULT_GROUP_PEER, help="精确群 username/peer")
    parser.add_argument("--task-name", default=DEFAULT_TASK_QUERY, help="任务名称关键词")
    return parser.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        task = _find_task(session, args.task_name)
        if task is None:
            raise ValueError(f"未找到任务: {args.task_name}")
        accounts = _completed_accounts(session, task)
        print(f"任务下已完成准入账号数: {len(accounts)}")
        gateway = TelethonTelegramGateway()
        admins = await _find_platform_admins(
            session,
            gateway,
            accounts=accounts[:MAX_ACCOUNT_PROBES],
            group_peer=args.group,
        )
        if admins:
            return
        await _compare_remote_admins(
            session,
            gateway,
            task=task,
            accounts=accounts,
            group_peer=args.group,
        )


def _find_task(session, query: str) -> Task | None:
    return session.scalar(
        select(Task)
        .where(Task.name.like(f"%{query}%"))
        .where(Task.deleted_at.is_(None))
    )


def _completed_accounts(session, task: Task) -> list[TgAccount]:
    items = session.scalars(
        select(TaskMembershipAdmissionItem).where(
            TaskMembershipAdmissionItem.task_id == task.id,
            TaskMembershipAdmissionItem.phase == "completed",
        )
    )
    accounts = [session.get(TgAccount, item.account_id) for item in items]
    return [account for account in accounts if account and account.session_ciphertext]


async def _find_platform_admins(
    session,
    gateway: TelethonTelegramGateway,
    *,
    accounts: list[TgAccount],
    group_peer: str,
) -> list[TgAccount]:
    admins = []
    for account in accounts:
        try:
            client, entity = await _client_and_entity(
                session,
                gateway,
                account=account,
                group_peer=group_peer,
            )
            participant = await client(
                functions.channels.GetParticipantRequest(
                    channel=entity,
                    participant=await client.get_me(),
                )
            )
            if isinstance(
                participant.participant,
                (types.ChannelParticipantAdmin, types.ChannelParticipantCreator),
            ):
                admins.append(account)
                rights = getattr(participant.participant, "admin_rights", None)
                print(
                    f"账号 {account.id} 是管理员，"
                    f"invite_users={getattr(rights, 'invite_users', False)}"
                )
        except errors.UserNotParticipantError:
            print(f"账号 {account.id} 不在群内")
        except Exception as exc:
            print(f"账号 {account.id} 探测失败: {type(exc).__name__}: {exc}")
    return admins


async def _client_and_entity(
    session,
    gateway: TelethonTelegramGateway,
    *,
    account: TgAccount,
    group_peer: str,
):
    raw_session = decrypt_session(account.session_ciphertext)
    credentials = credentials_for_account(session, account)
    client = await gateway._get_or_create_client(credentials, raw_session)
    return client, await client.get_entity(group_peer)


async def _compare_remote_admins(
    session,
    gateway: TelethonTelegramGateway,
    *,
    task: Task,
    accounts: list[TgAccount],
    group_peer: str,
) -> None:
    if not accounts:
        print("没有可用于只读管理员列表探测的已准入账号")
        return
    client, entity = await _client_and_entity(
        session,
        gateway,
        account=accounts[0],
        group_peer=group_peer,
    )
    result = await client(
        functions.channels.GetParticipantsRequest(
            channel=entity,
            filter=types.ChannelParticipantsAdmins(),
            offset=0,
            limit=50,
            hash=0,
        )
    )
    remote_ids = {user.id for user in result.users}
    platform_accounts = session.scalars(
        select(TgAccount).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.deleted_at.is_(None),
        )
    )
    matched = [
        account for account in platform_accounts
        if int(account.tg_id or 0) in remote_ids
    ]
    print(f"远端管理员数量={len(remote_ids)}，平台匹配账号数量={len(matched)}")
    for account in matched:
        print(f"账号 ID={account.id} | TG ID={account.tg_id} | Status={account.status}")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
