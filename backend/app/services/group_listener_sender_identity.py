from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)


SenderIdentity = dict[str, set[str]]


def listener_ignored_sender_identity(
    session: Session,
    group: TgGroup,
) -> SenderIdentity:
    exact_peer_ids, peer_aliases = _group_peer_identity(group)
    usernames: set[str] = set()
    titles = {str(group.title or "").strip().lower()} - {""}
    target = session.scalar(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == group.tenant_id,
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )
    if target is not None:
        _merge_target_identity(
            target,
            exact_peer_ids=exact_peer_ids,
            peer_aliases=peer_aliases,
            usernames=usernames,
            titles=titles,
        )
    return {
        "managed_keys": _managed_sender_keys(session, group),
        "exact_peer_ids": exact_peer_ids,
        "peer_aliases": peer_aliases,
        "usernames": usernames,
        "titles": titles,
        "outbound_remote_ids": set(),
    }


def with_outbound_remote_ids(
    identity: SenderIdentity,
    remote_ids: set[str],
) -> SenderIdentity:
    return {
        **identity,
        "outbound_remote_ids": set(identity.get("outbound_remote_ids", set())) | remote_ids,
    }


def outbound_remote_ids_for_snapshots(
    session: Session,
    group: TgGroup,
    snapshots: Iterable[object],
) -> set[str]:
    remote_ids = {
        str(getattr(snapshot, "remote_message_id", "") or "").strip()
        for snapshot in snapshots
    } - {""}
    if not remote_ids:
        return set()
    rows = session.scalars(
        select(ExecutionAttempt.remote_message_id)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.tenant_id == group.tenant_id,
            Action.action_type == "send_message",
            Action.payload["group_id"].as_integer() == int(group.id),
            ExecutionAttempt.remote_message_id.in_(remote_ids),
            ExecutionAttempt.remote_message_id != "",
        )
    )
    return {str(value) for value in rows if value}


def is_ignored_sender(snapshot: object, identity: SenderIdentity) -> bool:
    remote_id = str(getattr(snapshot, "remote_message_id", "") or "").strip()
    if remote_id and remote_id in identity.get("outbound_remote_ids", set()):
        return True
    sender_peer_id = str(getattr(snapshot, "sender_peer_id", "") or "").strip().lower()
    sender_peer_ids = _peer_id_keys(sender_peer_id)
    sender_peer_type = str(getattr(snapshot, "sender_peer_type", "") or "").strip().lower()
    sender_name = str(getattr(snapshot, "sender_name", "") or "").lower()
    sender_username = str(getattr(snapshot, "sender_username", "") or "").lower().lstrip("@")
    managed_keys = identity["managed_keys"]
    if _matches_managed_identity(sender_peer_id, sender_name, sender_username, managed_keys):
        return True
    if sender_peer_id in identity["exact_peer_ids"]:
        return True
    if sender_username and sender_username in identity["usernames"]:
        return True
    if sender_peer_type in {"channel", "chat"}:
        return bool(sender_peer_ids & identity["peer_aliases"])
    return bool(
        not sender_peer_type
        and sender_name in identity["titles"]
        and sender_peer_ids & identity["peer_aliases"]
    )


def listener_ignored_sender(session: Session, group: TgGroup, snapshot: object) -> bool:
    identity = listener_ignored_sender_identity(session, group)
    remote_ids = outbound_remote_ids_for_snapshots(session, group, [snapshot])
    return is_ignored_sender(snapshot, with_outbound_remote_ids(identity, remote_ids))


def _managed_sender_keys(session: Session, group: TgGroup) -> set[str]:
    accounts = session.scalars(
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgGroupAccount.group_id == group.id,
            TgAccount.tenant_id == group.tenant_id,
            TgAccount.deleted_at.is_(None),
        )
    )
    keys: set[str] = set()
    for account in accounts:
        keys.update(_account_identity_keys(account))
    return keys


def _account_identity_keys(account: TgAccount) -> set[str]:
    first_name = str(account.tg_first_name or "").strip().lower()
    last_name = str(account.tg_last_name or "").strip().lower()
    username = str(account.username or "").strip().lower().lstrip("@")
    values = {
        str(account.id),
        f"account:{account.id}",
        str(account.display_name or "").lower(),
        first_name,
        last_name,
        f"{first_name} {last_name}".strip(),
        username,
        f"@{username}" if username else "",
    }
    return values - {""}


def _group_peer_identity(group: TgGroup) -> tuple[set[str], set[str]]:
    peer_id = str(group.tg_peer_id or "").strip().lower()
    return ({peer_id} - {""}, _peer_id_keys(peer_id))


def _merge_target_identity(
    target: OperationTarget,
    *,
    exact_peer_ids: set[str],
    peer_aliases: set[str],
    usernames: set[str],
    titles: set[str],
) -> None:
    peer_id = str(target.tg_peer_id or "").strip().lower()
    title = str(target.title or "").strip().lower()
    username = str(target.username or "").strip().lower().lstrip("@")
    exact_peer_ids.update({peer_id} - {""})
    peer_aliases.update(_peer_id_keys(peer_id))
    titles.update({title} - {""})
    usernames.update({username} - {""})


def _matches_managed_identity(
    sender_peer_id: str,
    sender_name: str,
    sender_username: str,
    managed_keys: set[str],
) -> bool:
    return bool(
        sender_peer_id in managed_keys
        or sender_name in managed_keys
        or sender_username in managed_keys
        or (sender_username and f"@{sender_username}" in managed_keys)
    )


def _peer_id_keys(value: object) -> set[str]:
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


__all__ = [
    "is_ignored_sender",
    "listener_ignored_sender",
    "listener_ignored_sender_identity",
    "outbound_remote_ids_for_snapshots",
    "with_outbound_remote_ids",
]
