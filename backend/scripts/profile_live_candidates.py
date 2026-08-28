from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import TelegramDeveloperApp, TgAccount
from app.security import decrypt_secret, decrypt_session
from scripts.profile_candidate_filter import GroupCandidateProfile, ProfileFilter


logger = logging.getLogger("test_copy_group_profiles")


async def scrape_live_group_participants(
    session: Session,
    profile_filter: ProfileFilter,
    *,
    account_id: int,
    max_groups: int = 5,
    per_group_limit: int = 50,
) -> list[GroupCandidateProfile]:
    from telethon import TelegramClient, errors, functions
    from telethon.sessions import StringSession

    account = _live_source_account(session, account_id)
    dev_app = session.get(TelegramDeveloperApp, account.developer_app_id)
    if dev_app is None:
        raise ValueError(f"live_developer_app_missing:{account.id}")
    client = TelegramClient(
        StringSession(decrypt_session(account.session_ciphertext)),
        dev_app.api_id,
        decrypt_secret(dev_app.api_hash_ciphertext),
        flood_sleep_threshold=0,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError(f"live_account_unauthorized:{account.id}")
        dialogs = await client.get_dialogs(limit=30)
        groups = [dialog for dialog in dialogs if dialog.is_group or dialog.is_channel]
        return await _scrape_live_groups(
            client,
            groups[:max_groups],
            profile_filter,
            functions=functions,
            flood_wait_error=errors.FloodWaitError,
            per_group_limit=per_group_limit,
        )
    finally:
        await client.disconnect()


def _live_source_account(session: Session, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if account is None or account.status != "在线" or not account.session_ciphertext:
        raise ValueError(f"live_account_unavailable:{account_id}")
    return account


async def _scrape_live_groups(
    client,
    groups: list,
    profile_filter: ProfileFilter,
    *,
    functions,
    flood_wait_error,
    per_group_limit: int,
) -> list[GroupCandidateProfile]:
    candidates: list[GroupCandidateProfile] = []
    seen_names: set[str] = set()
    hit_flood_wait = False
    for dialog in groups:
        rows, hit_flood_wait = await _scrape_live_dialog(
            client,
            dialog,
            profile_filter,
            functions=functions,
            flood_wait_error=flood_wait_error,
            per_group_limit=per_group_limit,
            seen_names=seen_names,
            hit_flood_wait=hit_flood_wait,
        )
        candidates.extend(rows)
    return candidates


async def _scrape_live_dialog(
    client,
    dialog,
    profile_filter: ProfileFilter,
    *,
    functions,
    flood_wait_error,
    per_group_limit: int,
    seen_names: set[str],
    hit_flood_wait: bool,
) -> tuple[list[GroupCandidateProfile], bool]:
    title = dialog.title or "Unknown"
    entity = await client.get_entity(dialog.id)
    candidates: list[GroupCandidateProfile] = []
    scanned = 0
    async for user in client.iter_participants(entity, limit=per_group_limit):
        scanned += 1
        bio, hit_flood_wait = await _live_participant_bio(
            client,
            user,
            functions=functions,
            flood_wait_error=flood_wait_error,
            skip=hit_flood_wait,
        )
        candidate = _live_candidate(
            user,
            profile_filter,
            bio=bio,
            group_title=title,
            group_peer_id=str(dialog.id),
        )
        if candidate is None:
            continue
        normalized = ProfileFilter.normalize_name(candidate.display_name)
        if normalized in seen_names:
            continue
        seen_names.add(normalized)
        candidates.append(candidate)
    logger.info("Group %s: scanned=%d, accepted=%d", title, scanned, len(candidates))
    return candidates, hit_flood_wait


async def _live_participant_bio(
    client,
    user,
    *,
    functions,
    flood_wait_error,
    skip: bool,
) -> tuple[str, bool]:
    if skip or getattr(user, "bot", False) or getattr(user, "deleted", False):
        return "", skip
    try:
        full = await asyncio.wait_for(
            client(functions.users.GetFullUserRequest(user)),
            timeout=2.0,
        )
        return (full.full_user.about or "").strip(), False
    except flood_wait_error as exc:
        logger.info("Live profile bio flood wait: %ds", exc.seconds)
        return "", True
    except Exception as exc:
        logger.warning("Live profile bio lookup failed for %s: %s", user.id, exc)
        return "", False


def _live_candidate(
    user,
    profile_filter: ProfileFilter,
    *,
    bio: str,
    group_title: str,
    group_peer_id: str,
) -> GroupCandidateProfile | None:
    first_name = (user.first_name or "").strip()
    last_name = (user.last_name or "").strip()
    display_name = f"{first_name} {last_name}".strip() if last_name else first_name
    username = (user.username or "").strip()
    result = profile_filter.filter_candidate(
        user_id=str(user.id),
        display_name=display_name,
        username=username,
        bio=bio,
        is_bot=getattr(user, "bot", False),
        is_deleted=getattr(user, "deleted", False),
    )
    if not result.is_valid:
        return None
    return GroupCandidateProfile(
        source_type="telethon_participant",
        group_title=group_title,
        group_peer_id=group_peer_id,
        user_id=str(user.id),
        username=username,
        display_name=display_name,
        first_name=first_name,
        last_name=last_name,
        bio=bio,
        collected_at=datetime.utcnow().isoformat(),
    )
