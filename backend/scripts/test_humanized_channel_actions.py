from __future__ import annotations

import argparse
import asyncio
import json
import random
from typing import Any

from app.database import SessionLocal
from app.integrations.telegram.telethon_content import fetch_channel_reaction_capability
from app.models import TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_task_account


VALID_ACTIONS = frozenset({"view", "like", "comment"})


async def execute_view(client, channel_entity, message_id: int) -> dict[str, Any]:
    from telethon import functions

    await client(functions.messages.GetMessagesViewsRequest(
        peer=channel_entity,
        id=[message_id],
        increment=True,
    ))
    return {"status": "success", "message_id": message_id}


async def execute_like(
    client,
    channel_entity,
    message_id: int,
    *,
    capability,
    requested_reaction: str,
) -> dict[str, Any]:
    from telethon import functions, types

    available = list(capability.available_reactions)
    reaction = requested_reaction or _random_reaction(available)
    if capability.mode not in {"all", "some"} or reaction not in available:
        return {
            "status": "blocked",
            "reason": "reaction_capability_unavailable",
            "capability_mode": capability.mode,
            "requested_reaction": reaction,
        }
    await client(functions.messages.SendReactionRequest(
        peer=channel_entity,
        msg_id=message_id,
        reaction=[types.ReactionEmoji(emoticon=reaction)],
    ))
    return {"status": "success", "reaction_emoji": reaction}


def _random_reaction(available: list[str]) -> str:
    return random.SystemRandom().choice(available) if available else ""


async def execute_comment(
    client,
    channel_entity,
    message_id: int,
    *,
    comment_text: str,
) -> dict[str, Any]:
    message = await client.send_message(
        channel_entity,
        comment_text,
        comment_to=message_id,
    )
    return {"status": "success", "remote_message_id": str(message.id)}


async def run_pipeline(
    account_session_raw: str,
    channel_peer: str,
    credentials,
    *,
    actions_to_run: list[str],
    target_message_id: int,
    sample_comment: str,
    requested_reaction: str,
) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(account_session_raw), credentials.api_id, credentials.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("account_session_unauthorized")
        target: int | str = int(channel_peer) if channel_peer.lstrip("-").isdigit() else channel_peer
        entity = await client.get_entity(target)
        capability = await fetch_channel_reaction_capability(client, channel_peer)
        return await _execute_actions(
            client,
            entity,
            target_message_id,
            actions_to_run=actions_to_run,
            capability=capability,
            sample_comment=sample_comment,
            requested_reaction=requested_reaction,
        )
    finally:
        await client.disconnect()


async def _execute_actions(
    client,
    entity,
    message_id: int,
    *,
    actions_to_run: list[str],
    capability,
    sample_comment: str,
    requested_reaction: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {"target_message_id": message_id}
    if "view" in actions_to_run:
        results["view"] = await execute_view(client, entity, message_id)
    if "like" in actions_to_run:
        results["like"] = await execute_like(
            client,
            entity,
            message_id,
            capability=capability,
            requested_reaction=requested_reaction,
        )
    if "comment" in actions_to_run:
        results["comment"] = await execute_comment(
            client,
            entity,
            message_id,
            comment_text=sample_comment,
        )
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run exact production-equivalent channel mutations")
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--channel-peer", type=str, default="")
    parser.add_argument("--account-id", type=int, default=0)
    parser.add_argument("--actions", type=str, default="")
    parser.add_argument("--message-id", type=int, default=0)
    parser.add_argument("--comment", type=str, default="")
    parser.add_argument("--reaction", type=str, default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and (not args.account_id or not args.channel_peer or not args.message_id):
        parser.error("--apply requires exact --account-id, --channel-peer and --message-id")
    return args


def main() -> None:
    args = _parse_args()
    actions = [value.strip() for value in args.actions.split(",") if value.strip()]
    invalid = sorted(set(actions) - VALID_ACTIONS)
    if invalid:
        raise ValueError(f"unsupported_actions:{','.join(invalid)}")
    if not args.apply:
        print(json.dumps({"mode": "preview", "mutations_executed": False, "actions": actions}))
        return
    if not actions:
        raise ValueError("at_least_one_action_required")
    if "comment" in actions and not args.comment.strip():
        raise ValueError("comment_text_required")
    result = _run_exact_pipeline(args, actions)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run_exact_pipeline(args: argparse.Namespace, actions: list[str]) -> dict[str, Any]:
    with SessionLocal() as session:
        account = session.get(TgAccount, args.account_id)
        if account is None or account.tenant_id != args.tenant_id:
            raise ValueError("account_not_found_in_tenant")
        raw_session = decrypt_session(account.session_ciphertext)
        if not raw_session:
            raise RuntimeError("account_session_decrypt_failed")
        credentials = credentials_for_task_account(session, account, "channel_like")
    return asyncio.run(run_pipeline(
        raw_session,
        args.channel_peer,
        credentials,
        actions_to_run=actions,
        target_message_id=args.message_id,
        sample_comment=args.comment,
        requested_reaction=args.reaction,
    ))


if __name__ == "__main__":
    main()
