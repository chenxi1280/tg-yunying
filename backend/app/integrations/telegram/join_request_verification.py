"""Verify a fresh join-request challenge from an observed group-admin bot."""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon import functions, types
from telethon.errors import UserNotParticipantError


JOIN_REQUEST_PROTOCOL_SECONDS = 60
OBSERVATION_POLL_SECONDS = 1
PRIVATE_CONTEXT_LIMIT = 120
ARITHMETIC = re.compile(r"数学问题[：:]\s*(-?\d+)\s*([+\-−×*/÷])\s*(-?\d+)\s*=\s*[?？]")
EXPIRY = re.compile(r"请在\s*(\d+)\s*秒内完成回答")


@dataclass(frozen=True)
class JoinRequestBaseline:
    entity: object
    bots: tuple
    cursors: tuple[int, ...]


@dataclass(frozen=True)
class JoinChallenge:
    bot: object
    message: object
    button: object
    expires_at: datetime
    fingerprint: str


async def capture_join_request_baseline(client, entity) -> JoinRequestBaseline:
    admins = await client.get_participants(entity, filter=types.ChannelParticipantsAdmins)
    bots = tuple(user for user in admins if user.bot)
    histories = await asyncio.gather(*(client.get_messages(bot, limit=1) for bot in bots))
    cursors = tuple(messages[0].id if messages else 0 for messages in histories)
    return JoinRequestBaseline(entity, bots, cursors)


async def resolve_join_request(client, baseline, *, requested_at: datetime) -> dict:
    evidence = {"join_request_submitted": True, "group_peer_id": baseline.entity.id,
                "requested_at": requested_at.isoformat(), "membership_observed": False,
                "callback_started": False, "callback_response_observed": False}
    deadline = requested_at + timedelta(seconds=JOIN_REQUEST_PROTOCOL_SECONDS)
    challenge, reason = await _observe_challenge(client, baseline, requested_at=requested_at, deadline=deadline)
    if challenge is None:
        return {**evidence, "verification_status": reason}
    evidence = {**evidence, **_challenge_evidence(challenge)}
    reason = await _revalidate_challenge(client, baseline, challenge=challenge, requested_at=requested_at)
    if reason:
        return {**evidence, "verification_status": reason}
    evidence = {**evidence, "callback_started": True}
    try:
        await client(functions.messages.GetBotCallbackAnswerRequest(
            peer=challenge.bot, msg_id=challenge.message.id, data=challenge.button.data))
    except Exception as exc:
        return {**evidence, "verification_status": "callback_result_unknown", "error_type": type(exc).__name__}
    evidence = {**evidence, "callback_response_observed": True}
    try:
        joined = await _observe_member(client, baseline.entity, deadline=challenge.expires_at)
    except Exception as exc:
        return {**evidence, "verification_status": "membership_observation_failed", "error_type": type(exc).__name__}
    return {**evidence, "membership_observed": joined,
            "verification_status": "membership_observed" if joined else "approval_not_observed"}


async def _observe_challenge(client, baseline, *, requested_at, deadline):
    if not baseline.bots:
        return None, "admin_bot_not_observed"
    while datetime.now(timezone.utc) < deadline:
        histories = await asyncio.gather(*(client.get_messages(bot, limit=PRIVATE_CONTEXT_LIMIT, min_id=cursor)
                                          for bot, cursor in zip(baseline.bots, baseline.cursors)))
        candidates = _challenge_candidates(baseline, histories, requested_at=requested_at)
        if len(candidates) > 1:
            return None, "challenge_ambiguous"
        if candidates:
            return candidates[0], ""
        await asyncio.sleep(OBSERVATION_POLL_SECONDS)
    return None, "fresh_supported_challenge_not_observed"


def _challenge_candidates(baseline, histories, *, requested_at):
    result = []
    for bot, cursor, messages in zip(baseline.bots, baseline.cursors, histories):
        for message in messages:
            if message.id <= cursor:
                continue
            challenge = parse_challenge(message, bot=bot, group_title=baseline.entity.title, requested_at=requested_at)
            if challenge is not None:
                result.append(challenge)
    return result


def parse_challenge(message, *, bot, group_title, requested_at) -> JoinChallenge | None:
    text = message.raw_text or ""
    if message.sender_id != bot.id or not message.is_private or message.out:
        return None
    if not text.startswith(f"来自『{group_title}』的申请入群验证"):
        return None
    # Telegram dates are second-granular; the pre-request ID cursor is the strict ordering proof.
    if message.date < requested_at.replace(microsecond=0):
        return None
    ttl, expression = EXPIRY.search(text), ARITHMETIC.search(text)
    if ttl is None or expression is None:
        return None
    expires_at = message.date + timedelta(seconds=int(ttl.group(1)))
    if datetime.now(timezone.utc) >= expires_at:
        return None
    answer = arithmetic_answer(expression.groups())
    if not answer:
        return None
    buttons = [button for row in getattr(message.reply_markup, "rows", ()) for button in row.buttons
               if isinstance(button, types.KeyboardButtonCallback) and button.text.strip() == answer]
    if len(buttons) != 1:
        return None
    fingerprint = _challenge_hash(message, buttons[0])
    return JoinChallenge(bot, message, buttons[0], expires_at, fingerprint)


def arithmetic_answer(parts) -> str:
    left, operator, right = parts
    left, right = int(left), int(right)
    if operator == "+":
        return str(left + right)
    if operator in {"-", "−"}:
        return str(left - right)
    if operator in {"*", "×"}:
        return str(left * right)
    if right == 0 or left % right:
        return ""
    return str(left // right)


async def _revalidate_challenge(client, baseline, *, challenge, requested_at):
    admins = await client.get_participants(baseline.entity, filter=types.ChannelParticipantsAdmins)
    if not any(user.bot and user.id == challenge.bot.id for user in admins):
        return "challenge_bot_no_longer_admin"
    current = await client.get_messages(challenge.bot, ids=challenge.message.id)
    if current is None:
        return "challenge_disappeared"
    parsed = parse_challenge(current, bot=challenge.bot, group_title=baseline.entity.title, requested_at=requested_at)
    if parsed is None or parsed.fingerprint != challenge.fingerprint:
        return "challenge_changed_or_expired"
    return ""


async def _observe_member(client, entity, *, deadline):
    while datetime.now(timezone.utc) < deadline:
        try:
            await client(functions.channels.GetParticipantRequest(entity, "me"))
            return True
        except UserNotParticipantError:
            await asyncio.sleep(OBSERVATION_POLL_SECONDS)
    return False


def _challenge_hash(message, button):
    payload = (message.raw_text or "").encode() + b"\0" + button.data
    return hashlib.sha256(payload).hexdigest()


def _challenge_evidence(challenge):
    return {"bot_peer_id": challenge.bot.id, "challenge_message_id": challenge.message.id,
            "challenge_hash": challenge.fingerprint, "expires_at": challenge.expires_at.isoformat(),
            "callback_data_hash": hashlib.sha256(challenge.button.data).hexdigest()}
