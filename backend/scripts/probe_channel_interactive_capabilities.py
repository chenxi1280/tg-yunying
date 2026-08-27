from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, NoReturn

from sqlalchemy import select

from app.database import SessionLocal
from app.models import OperationTarget, TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_task_account


async def probe_channel(
    account_session_raw: str,
    channel_peer: str,
    credentials,
) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(account_session_raw), credentials.api_id, credentials.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"error": "account_session_unauthorized"}
        return await _collect_probe(client, channel_peer)
    except Exception as exc:  # noqa: BLE001 - probe output must expose the exact remote failure.
        return {"channel_peer": channel_peer, "error": f"{type(exc).__name__}:{exc}"}
    finally:
        await client.disconnect()


async def _collect_probe(client, channel_peer: str) -> dict[str, Any]:
    from telethon import functions

    from app.integrations.telegram.telethon_content import resolve_channel_reaction_capability

    target: int | str = int(channel_peer) if channel_peer.lstrip("-").isdigit() else channel_peer
    entity = await client.get_entity(target)
    full = await client(functions.channels.GetFullChannelRequest(channel=entity))
    full_chat = full.full_chat
    capability = await resolve_channel_reaction_capability(
        client,
        getattr(full_chat, "available_reactions", None),
    )
    result = {
        "channel_peer": channel_peer,
        "available_reactions": list(capability.available_reactions),
        "reactions_mode": capability.mode,
        "has_linked_discussion": bool(getattr(full_chat, "linked_chat_id", None)),
        "linked_discussion_id": getattr(full_chat, "linked_chat_id", None),
        "recent_messages": await _recent_messages(client, entity),
    }
    result.update(await _discussion_details(client, result["linked_discussion_id"]))
    return result


async def _discussion_details(client, linked_id) -> dict[str, Any]:
    if not linked_id:
        return {"discussion_send_permission": False, "discussion_slowmode_seconds": 0}
    from telethon import functions

    try:
        entity = await client.get_entity(linked_id)
        full = await client(functions.channels.GetFullChannelRequest(channel=entity))
        rights = getattr(full.full_chat, "default_banned_rights", None)
        return {
            "discussion_send_permission": not bool(rights and getattr(rights, "send_messages", False)),
            "discussion_slowmode_seconds": getattr(full.full_chat, "slowmode_seconds", 0) or 0,
        }
    except Exception as exc:  # noqa: BLE001 - partial probe reports the exact discussion failure.
        return {
            "discussion_send_permission": False,
            "discussion_slowmode_seconds": 0,
            "discussion_probe_error": f"{type(exc).__name__}:{exc}",
        }


async def _recent_messages(client, entity) -> list[dict[str, Any]]:
    messages = await client.get_messages(entity, limit=5)
    return [_message_summary(message) for message in messages if message]


def _message_summary(message) -> dict[str, Any]:
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", []) if reactions else []
    replies = getattr(message, "replies", None)
    return {
        "message_id": message.id,
        "text_snippet": (message.text or "")[:60].replace("\n", " "),
        "views": getattr(message, "views", 0) or 0,
        "forwards": getattr(message, "forwards", 0) or 0,
        "reactions_count": sum(item.count for item in results),
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
        "replies_count": getattr(replies, "replies", 0) if replies else 0,
    }


def main() -> None:
    args = _parse_args()
    with SessionLocal() as session:
        account = _probe_account(session, args.tenant_id, args.account_id)
        if account is None:
            _fail("no_active_account_found")
        raw_session = decrypt_session(account.session_ciphertext)
        if not raw_session:
            _fail("account_session_decrypt_failed")
        channel_peer = args.channel_peer or _first_channel_peer(session, args.tenant_id)
        if not channel_peer:
            _fail("no_channel_found")
        credentials = credentials_for_task_account(session, account, "channel_like")
    result = asyncio.run(probe_channel(raw_session, channel_peer, credentials))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe channel interaction capabilities")
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--channel-peer", type=str, default="")
    parser.add_argument("--account-id", type=int, default=0)
    return parser.parse_args()


def _probe_account(session, tenant_id: int, account_id: int) -> TgAccount | None:
    if account_id:
        account = session.get(TgAccount, account_id)
        return account if account and account.tenant_id == tenant_id else None
    return session.scalars(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.is_active.is_(True),
            TgAccount.session_ciphertext.is_not(None),
        )
        .order_by(TgAccount.id.asc())
    ).first()


def _first_channel_peer(session, tenant_id: int) -> str:
    target = session.scalars(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "channel",
        )
        .order_by(OperationTarget.id.asc())
    ).first()
    return str(target.tg_peer_id or "") if target else ""


def _fail(error: str) -> NoReturn:
    print(json.dumps({"error": error}))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
