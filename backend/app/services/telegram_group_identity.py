from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import OperationTarget, TgGroup


PUBLIC_LINK_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "telegram.me/",
    "https://www.t.me/",
    "http://www.t.me/",
)


@dataclass(frozen=True)
class GroupSnapshotIdentity:
    peer_id: str
    target: OperationTarget | None
    group: TgGroup | None


def resolve_group_snapshot_identity(
    session: Session,
    tenant_id: int,
    snapshot,
) -> GroupSnapshotIdentity:
    target = _target_for_snapshot(session, tenant_id, snapshot)
    peer_id = str(target.tg_peer_id) if target else str(snapshot.tg_peer_id)
    group = _group_for_peer(session, tenant_id, peer_id)
    if not target and not group:
        group = _legacy_group_for_snapshot(session, tenant_id, snapshot)
        peer_id = group.tg_peer_id if group else peer_id
    return GroupSnapshotIdentity(peer_id=peer_id, target=target, group=group)


def _group_for_peer(session: Session, tenant_id: int, peer_id: str) -> TgGroup | None:
    return session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == tenant_id,
            TgGroup.tg_peer_id == peer_id,
        )
    )


def _legacy_group_for_snapshot(session: Session, tenant_id: int, snapshot) -> TgGroup | None:
    username = _public_username(getattr(snapshot, "username", ""))
    if not username:
        return None
    candidates = list(session.scalars(select(TgGroup).where(
        TgGroup.tenant_id == tenant_id,
        TgGroup.tg_peer_id.in_(_public_refs(username)),
    )))
    if len(candidates) > 1:
        raise ValueError("group_snapshot_public_identity_ambiguous")
    return candidates[0] if candidates else None


def _target_for_snapshot(
    session: Session,
    tenant_id: int,
    snapshot,
) -> OperationTarget | None:
    direct = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.tg_peer_id == str(snapshot.tg_peer_id),
        )
    )
    if direct:
        return direct
    username = _public_username(getattr(snapshot, "username", ""))
    if not username:
        return None
    candidates = list(session.scalars(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == _target_type(snapshot),
            or_(
                OperationTarget.username.in_(_public_refs(username)),
                OperationTarget.tg_peer_id.in_(_public_refs(username)),
            ),
        )
    ))
    if len(candidates) > 1:
        raise ValueError("group_snapshot_public_identity_ambiguous")
    return candidates[0] if candidates else None


def _target_type(snapshot) -> str:
    return "channel" if str(getattr(snapshot, "group_type", "")) == "channel" else "group"


def _public_username(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    else:
        lower = raw.lower()
        for prefix in PUBLIC_LINK_PREFIXES:
            if lower.startswith(prefix):
                raw = raw[len(prefix):].split("?", 1)[0].strip("/")
                break
    if not raw or "/" in raw or raw.startswith(("+", "joinchat/", "c/")):
        return ""
    return raw.lower()


def _public_refs(username: str) -> tuple[str, ...]:
    return (
        username,
        f"@{username}",
        *(f"{prefix}{username}" for prefix in PUBLIC_LINK_PREFIXES),
    )


__all__ = ["GroupSnapshotIdentity", "resolve_group_snapshot_identity"]
