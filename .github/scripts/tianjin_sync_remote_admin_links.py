from __future__ import annotations

import json

from sqlalchemy import func, select

from app.database import SessionLocal
from app.integrations.telegram.telethon_utils import resolve_telethon_target
from app.models import OperationTarget, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus, GroupAuthStatus
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account
from app.telethon_lifecycle import TelethonClientLifecycle


TARGET_ID = 485
TENANT_ID = 1


def account_payload(account, link):
    return {
        "account_id": account.id,
        "username": account.username,
        "display_name": account.display_name,
        "status": account.status,
        "had_link": bool(link),
        "can_send": bool(link.can_send) if link else False,
    }


def load_target_group(session):
    target = session.get(OperationTarget, TARGET_ID)
    if not target:
        raise RuntimeError(f"target {TARGET_ID} missing")
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == TENANT_ID,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if not group:
        raise RuntimeError(f"target group {target.tg_peer_id} missing")
    return target, group


def configured_admin(session):
    tenant = session.get(Tenant, TENANT_ID)
    account_id = tenant.group_rescue_admin_account_id if tenant else None
    account = session.get(TgAccount, account_id) if account_id else None
    if not account or not account.session_ciphertext:
        raise RuntimeError("configured rescue admin unavailable")
    return account


async def fetch_remote_admin_usernames(raw_session, credentials, target_peer_id):
    if not raw_session:
        raise RuntimeError("configured rescue admin session decrypt failed")
    from telethon import types

    lifecycle = TelethonClientLifecycle()
    client = await lifecycle.get_or_create_client(credentials, raw_session)
    if not await client.is_user_authorized():
        raise RuntimeError("configured rescue admin session is unauthorized")
    target = await resolve_telethon_target(client, target_peer_id, group_id=0)
    usernames = set()
    async for participant in client.iter_participants(target, filter=types.ChannelParticipantsAdmins):
        username = str(getattr(participant, "username", "") or "").lower()
        if username:
            usernames.add(username)
    return usernames


def matched_local_admins(session, usernames):
    if not usernames:
        return []
    return list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.session_ciphertext.is_not(None),
                func.lower(TgAccount.username).in_(usernames),
            )
            .order_by(TgAccount.id.asc())
        )
    )


def upsert_admin_link(session, group, account):
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == TENANT_ID,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account.id,
        )
    )
    before = account_payload(account, link)
    if not link:
        link = TgGroupAccount(tenant_id=TENANT_ID, group_id=group.id, account_id=account.id)
        session.add(link)
    link.can_send = True
    link.permission_label = "Telegram 管理员"
    return before, account_payload(account, link)


def main():
    with SessionLocal() as session:
        target, group = load_target_group(session)
        admin = configured_admin(session)
        raw_session = decrypt_session(admin.session_ciphertext)
        credentials = credentials_for_account(session, admin)
        usernames = TelethonClientLifecycle().run(fetch_remote_admin_usernames(raw_session, credentials, target.tg_peer_id))
        changed = []
        for account in matched_local_admins(session, usernames):
            before, after = upsert_admin_link(session, group, account)
            changed.append({"before": before, "after": after})
        group.can_send = True
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
        target.can_send = True
        target.auth_status = GroupAuthStatus.AUTHORIZED.value
        session.commit()
        print(
            "TIANJIN_REMOTE_ADMIN_LINK_SYNC="
            + json.dumps({"matched_admin_count": len(changed), "changed": changed}, ensure_ascii=False, sort_keys=True),
            flush=True,
        )


if __name__ == "__main__":
    main()
