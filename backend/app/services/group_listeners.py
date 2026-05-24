from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountStatus,
    Campaign,
    GroupAuthStatus,
    GroupContextMessage,
    OperationTarget,
    PromptTemplate,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import CampaignCreate, GenerateDraftsRequest

from ._common import SUBSCRIPTION_INACTIVE_DETAIL, _now, audit, gateway, require_system_user_core_features
from .campaigns import approve_all_drafts, create_campaign, generate_drafts
from .developer_apps import credentials_for_account
from .source_media import ensure_source_media_asset


def validate_listener_accounts(session: Session, group: TgGroup, account_ids: list[int]) -> list[TgGroupAccount]:
    links: list[TgGroupAccount] = []
    for account_id in dict.fromkeys(account_ids):
        account = session.get(TgAccount, account_id)
        link = session.scalar(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == group.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.account_id == account_id,
            )
        )
        if not account or account.deleted_at is not None or account.tenant_id != group.tenant_id:
            raise ValueError("listener account not found")
        if account.status != AccountStatus.ACTIVE.value:
            raise ValueError("listener account must be online")
        if not link:
            raise ValueError("listener account must be in target group")
        links.append(link)
    return links


def apply_group_listener_accounts(session: Session, group: TgGroup, account_ids: list[int]) -> None:
    links = list(
        session.scalars(
            select(TgGroupAccount).where(TgGroupAccount.tenant_id == group.tenant_id, TgGroupAccount.group_id == group.id)
        )
    )
    wanted = set(dict.fromkeys(account_ids))
    validate_listener_accounts(session, group, list(wanted))
    for link in links:
        link.is_listener = link.account_id in wanted


def recent_context_messages(session: Session, group: TgGroup, limit: int | None = None) -> list[GroupContextMessage]:
    return list(
        session.scalars(
            select(GroupContextMessage)
            .where(GroupContextMessage.tenant_id == group.tenant_id, GroupContextMessage.group_id == group.id)
            .order_by(GroupContextMessage.sent_at.desc(), GroupContextMessage.id.desc())
            .limit(limit or group.listener_context_limit)
        )
    )


def listener_account_summaries(session: Session, group: TgGroup) -> list[dict]:
    rows = list(
        session.scalars(
            select(TgGroupAccount)
            .where(
                TgGroupAccount.tenant_id == group.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.is_listener.is_(True),
            )
            .order_by(TgGroupAccount.id.asc())
        )
    )
    summaries: list[dict] = []
    for link in rows:
        account = session.get(TgAccount, link.account_id)
        if account and account.deleted_at is None:
            summaries.append({"id": account.id, "display_name": account.display_name, "username": account.username, "status": account.status})
    return summaries


def _managed_sender_keys(session: Session, group: TgGroup) -> set[str]:
    keys: set[str] = set()
    accounts = list(
        session.scalars(
            select(TgAccount)
            .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
            .where(
                TgGroupAccount.group_id == group.id,
                TgAccount.tenant_id == group.tenant_id,
                TgAccount.deleted_at.is_(None),
            )
        )
    )
    for account in accounts:
        keys.add(str(account.id))
        keys.add(f"account:{account.id}")
        keys.add(account.display_name.lower())
        first_name = (account.tg_first_name or "").strip().lower()
        last_name = (account.tg_last_name or "").strip().lower()
        full_name = f"{first_name} {last_name}".strip()
        for name in (first_name, last_name, full_name):
            if name:
                keys.add(name)
        if account.username:
            keys.add(account.username.lower().lstrip("@"))
            keys.add(f"@{account.username.lower().lstrip('@')}")
    return keys


def _is_ignored_sender(snapshot, ignored_identity: dict[str, set[str]]) -> bool:
    sender_peer_id = str(getattr(snapshot, "sender_peer_id", "") or "").strip().lower()
    sender_peer_ids = _peer_id_keys(sender_peer_id)
    sender_peer_type = str(getattr(snapshot, "sender_peer_type", "") or "").strip().lower()
    sender_name = str(getattr(snapshot, "sender_name", "") or "").lower()
    sender_username = str(getattr(snapshot, "sender_username", "") or "").lower().lstrip("@")
    managed_keys = ignored_identity["managed_keys"]
    if (
        sender_peer_id in managed_keys
        or sender_name in managed_keys
        or sender_username in managed_keys
        or (f"@{sender_username}" in managed_keys if sender_username else False)
    ):
        return True
    if sender_peer_id in ignored_identity["exact_peer_ids"]:
        return True
    if sender_username and sender_username in ignored_identity["usernames"]:
        return True
    if sender_peer_type in {"channel", "chat"}:
        return bool(sender_peer_ids & ignored_identity["peer_aliases"])
    if not sender_peer_type and sender_name in ignored_identity["titles"]:
        return bool(sender_peer_ids & ignored_identity["peer_aliases"])
    return False


def is_listener_ignored_sender(session: Session, group: TgGroup, snapshot) -> bool:
    return _is_ignored_sender(snapshot, _listener_ignored_sender_identity(session, group))


def _listener_ignored_sender_identity(session: Session, group: TgGroup) -> dict[str, set[str]]:
    exact_peer_ids: set[str] = set()
    peer_aliases: set[str] = set()
    usernames: set[str] = set()
    titles: set[str] = set()
    managed_keys = _managed_sender_keys(session, group)
    for value in (group.tg_peer_id, group.title):
        text = str(value or "").strip().lower()
        if not text:
            continue
        if value == group.tg_peer_id:
            exact_peer_ids.add(text)
            peer_aliases.update(_peer_id_keys(value))
        else:
            titles.add(text)

    target = session.scalar(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == group.tenant_id,
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )
    if target:
        for value in (target.tg_peer_id,):
            text = str(value or "").strip().lower()
            if text:
                exact_peer_ids.add(text)
                peer_aliases.update(_peer_id_keys(value))
        for value in (target.title,):
            text = str(value or "").strip().lower()
            if text:
                titles.add(text)
        username = str(target.username or "").strip().lower().lstrip("@")
        if username:
            usernames.add(username)
    return {
        "managed_keys": managed_keys,
        "exact_peer_ids": exact_peer_ids,
        "peer_aliases": peer_aliases,
        "usernames": usernames,
        "titles": titles,
    }


def _peer_id_keys(value) -> set[str]:
    text = str(value or "").strip().lower()
    if not text:
        return set()
    keys = {text}
    if text.startswith("-100") and text[4:].isdigit():
        bare_id = str(int(text[4:]))
        keys.update({bare_id, f"100{bare_id}", f"-100{bare_id}"})
    elif text.isdigit():
        bare_id = str(int(text))
        keys.update({bare_id, f"-100{bare_id}"})
    return keys


def _system_user(session: Session, tenant_id: int):
    return require_system_user_core_features(
        session,
        tenant_id,
        service_name="监听AI服务",
        missing_message="no tenant app user available for AI usage ledger",
    )


def _listener_template_id(session: Session, tenant_id: int) -> int | None:
    return session.scalar(
        select(PromptTemplate.id)
        .where(
            PromptTemplate.is_active.is_(True),
            PromptTemplate.template_type == "监听上下文续聊脚本",
            (PromptTemplate.tenant_id == tenant_id) | (PromptTemplate.tenant_id.is_(None)),
        )
        .order_by(PromptTemplate.tenant_id.is_(None), PromptTemplate.id.asc())
    )


def _send_account_ids(session: Session, group: TgGroup) -> list[int]:
    links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(
                TgGroupAccount.tenant_id == group.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(True),
                TgGroupAccount.is_listener.is_(False),
            )
            .order_by(TgGroupAccount.id.asc())
        )
    )
    ids = [link.account_id for link in links if (account := session.get(TgAccount, link.account_id)) and account.deleted_at is None and account.status == AccountStatus.ACTIVE.value]
    if ids:
        return ids
    fallback_links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(TgGroupAccount.tenant_id == group.tenant_id, TgGroupAccount.group_id == group.id, TgGroupAccount.can_send.is_(True))
            .order_by(TgGroupAccount.id.asc())
        )
    )
    return [
        link.account_id
        for link in fallback_links
        if (account := session.get(TgAccount, link.account_id)) and account.deleted_at is None and account.status == AccountStatus.ACTIVE.value
    ]


def collect_group_context(session: Session, group: TgGroup, account_ids: list[int] | None = None) -> int:
    stmt = select(TgGroupAccount).where(
        TgGroupAccount.tenant_id == group.tenant_id,
        TgGroupAccount.group_id == group.id,
    )
    if account_ids is None:
        stmt = stmt.where(TgGroupAccount.is_listener.is_(True))
    else:
        stmt = stmt.where(TgGroupAccount.account_id.in_(list(dict.fromkeys(account_ids))))
    listener_links = list(session.scalars(stmt.order_by(TgGroupAccount.id.asc())))
    if not listener_links:
        return 0
    ignored_sender_identity = _listener_ignored_sender_identity(session, group)
    inserted = 0
    for link in listener_links:
        account = session.get(TgAccount, link.account_id)
        if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
            continue
        credentials = credentials_for_account(session, account)
        snapshots = gateway.fetch_group_messages(
            account.id,
            group.tg_peer_id,
            account.session_ciphertext,
            credentials,
            limit=group.listener_context_limit,
        )
        for snapshot in snapshots:
            content = str(snapshot.content or "").strip()
            if not content or _is_ignored_sender(snapshot, ignored_sender_identity):
                continue
            exists = session.scalar(
                select(GroupContextMessage.id).where(
                    GroupContextMessage.group_id == group.id,
                    GroupContextMessage.remote_message_id == str(snapshot.remote_message_id),
                )
            )
            if exists:
                continue
            message = GroupContextMessage(
                tenant_id=group.tenant_id,
                group_id=group.id,
                listener_account_id=account.id,
                sender_peer_id=str(snapshot.sender_peer_id or ""),
                sender_name=str(snapshot.sender_name or "真人用户"),
                sender_username=str(getattr(snapshot, "sender_username", "") or "").lstrip("@"),
                is_bot=bool(getattr(snapshot, "is_bot", False)),
                sender_role=str(getattr(snapshot, "sender_role", "") or "member"),
                content=content[:4000],
                message_type=snapshot.message_type,
                remote_message_id=str(snapshot.remote_message_id),
                sent_at=snapshot.sent_at,
            )
            session.add(message)
            session.flush()
            if snapshot.message_type != "text":
                ensure_source_media_asset(
                    session,
                    tenant_id=group.tenant_id,
                    source_group_id=group.id,
                    listener_account_id=account.id,
                    source_peer_id=group.tg_peer_id,
                    source_message_id=str(snapshot.remote_message_id),
                    source_media_group_id=str(getattr(snapshot, "media_group_id", "") or ""),
                    media_group_index=int(getattr(snapshot, "media_group_index", 0) or 0),
                    media_group_total=int(getattr(snapshot, "media_group_total", 1) or 1),
                    media_type=str(getattr(snapshot, "media_type", "") or snapshot.message_type or "media"),
                    caption=str(getattr(snapshot, "caption", "") or content),
                    media_fingerprint=str(getattr(snapshot, "media_fingerprint", "") or ""),
                )
            inserted += 1
    return inserted


def trigger_listener_auto_reply(session: Session, group: TgGroup) -> int:
    unprocessed = list(
        session.scalars(
            select(GroupContextMessage)
            .where(
                GroupContextMessage.tenant_id == group.tenant_id,
                GroupContextMessage.group_id == group.id,
                GroupContextMessage.used_for_ai.is_(False),
                GroupContextMessage.is_bot.is_(False),
            )
            .order_by(GroupContextMessage.sent_at.asc(), GroupContextMessage.id.asc())
        )
    )
    if not unprocessed or not group.listener_auto_reply_enabled:
        return 0
    try:
        user = _system_user(session, group.tenant_id)
    except ValueError as exc:
        if str(exc) != SUBSCRIPTION_INACTIVE_DETAIL:
            raise
        group.listener_last_error = SUBSCRIPTION_INACTIVE_DETAIL
        session.commit()
        return 0
    account_ids = _send_account_ids(session, group)
    if not account_ids:
        group.listener_last_error = "没有可用于自动续聊的发送账号"
        return 0
    template_id = _listener_template_id(session, group.tenant_id)
    selected = {str(group.id): account_ids}
    campaign = create_campaign(
        session,
        CampaignCreate(
            tenant_id=group.tenant_id,
            group_id=group.id,
            title=f"{group.title} 监听自动续聊",
            campaign_type="监听上下文续聊",
            topic=group.topic_direction or "群内真人问题接话",
            send_window=group.active_window,
            intensity="自动续聊",
            prompt_template_id=template_id,
            jitter_min_seconds=0,
            jitter_max_seconds=0,
            batch_interval_seconds=max(1, group.group_cooldown_seconds),
            respect_send_window=True,
            target_group_ids=[group.id],
            selected_account_ids_by_group=selected,
        ),
        actor="监听AI服务",
    )
    contexts = [item for item in recent_context_messages(session, group, group.listener_context_limit) if not bool(getattr(item, "is_bot", False))]
    listener_ids = [item.listener_account_id for item in unprocessed]
    payload = GenerateDraftsRequest(
        count=min(len(account_ids), 5),
        tone="自然、像真实群成员聊天，接住真人上下文继续聊",
        use_ai=True,
        fallback_to_mock=False,
        selected_account_ids_by_group=selected,
        listener_account_id=listener_ids[-1] if listener_ids else None,
        conversation_context=[
            {
                "sender_name": item.sender_name,
                "content": item.content,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
            }
            for item in reversed(contexts)
        ],
    )
    drafts = generate_drafts(session, campaign.id, payload, user)
    tasks = approve_all_drafts(session, campaign.id, "监听AI服务")
    with session.no_autoflush:
        for message_id in [item.id for item in unprocessed]:
            message = session.get(GroupContextMessage, message_id)
            if message:
                message.used_for_ai = True
        refreshed_group = session.get(TgGroup, group.id)
        if refreshed_group:
            refreshed_group.listener_last_reply_at = _now()
            refreshed_group.listener_last_error = ""
        audit(
            session,
            tenant_id=group.tenant_id,
            actor="监听AI服务",
            action="监听上下文自动续聊",
            target_type="tg_group",
            target_id=str(group.id),
            detail=f"contexts={len(unprocessed)}; drafts={len(drafts)}; tasks={len(tasks)}; accounts={json.dumps(account_ids)}",
        )
        session.commit()
    return len(tasks)


def process_group_listener(session: Session, group_id: int) -> int:
    group = session.get(TgGroup, group_id)
    if not group or not group.listener_enabled:
        return 0
    if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        return 0
    now_value = _now()
    if group.listener_last_polled_at and group.listener_last_polled_at + timedelta(seconds=group.listener_interval_seconds) > now_value:
        return 0
    try:
        inserted = collect_group_context(session, group)
        group = session.get(TgGroup, group_id)
        group.listener_last_polled_at = now_value
        group.listener_last_error = ""
        session.commit()
        if inserted:
            if get_settings().enable_legacy_campaign_worker:
                return inserted + trigger_listener_auto_reply(session, group)
            return inserted
        return 0
    except Exception as exc:  # noqa: BLE001 - operator-facing listener status.
        session.rollback()
        group = session.get(TgGroup, group_id)
        if group:
            group.listener_last_error = str(exc)
            group.listener_last_polled_at = now_value
            session.commit()
        return 0


def drain_group_listeners(session_factory, limit: int = 10) -> int:
    with session_factory() as session:
        group_ids = list(
            session.scalars(
                select(TgGroup.id)
                .where(TgGroup.listener_enabled.is_(True), TgGroup.auth_status == GroupAuthStatus.AUTHORIZED.value)
                .order_by(TgGroup.listener_last_polled_at.asc().nullsfirst(), TgGroup.id.asc())
                .limit(limit)
            )
        )
    processed = 0
    for group_id in group_ids:
        with session_factory() as session:
            processed += process_group_listener(session, group_id)
    return processed


__all__ = [
    "apply_group_listener_accounts",
    "drain_group_listeners",
    "is_listener_ignored_sender",
    "listener_account_summaries",
    "process_group_listener",
    "recent_context_messages",
    "trigger_listener_auto_reply",
    "validate_listener_accounts",
]
