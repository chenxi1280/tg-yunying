from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupContextMessage, TgAccount, TgAccountAuthorization, TgGroup


SURFACE_POLICY_VERSION = 1


@dataclass(frozen=True)
class ProbeSurface:
    account: TgAccount
    group: TgGroup
    authorization: TgAccountAuthorization | None
    identity: dict
    start_cursor: int | None


def surface_identity(
    group: TgGroup,
    *,
    authorization: TgAccountAuthorization | None,
    account_id: int,
    session_ciphertext: str,
    start_cursor: int,
    end_cursor: int,
    observation_version: int,
) -> dict:
    return {
        "surface_kind": "target_group_control_stream",
        "surface_peer_id": str(group.tg_peer_id),
        "viewer_account_id": account_id,
        "viewer_session_identity_hash": fact_hash(session_ciphertext),
        "viewer_authorization_id": authorization.id if authorization else "",
        "viewer_authorization_fact_version": (
            int(authorization.fact_version or 1) if authorization else 0
        ),
        "listener_instance_epoch": observation_version,
        "listener_policy_version": SURFACE_POLICY_VERSION,
        "observed_start_cursor": str(start_cursor),
        "observed_end_cursor": str(end_cursor),
    }


def surface_is_current(
    identity: dict,
    group: TgGroup,
    authorization: TgAccountAuthorization | None,
    *,
    account_id: int,
    session_ciphertext: str,
) -> bool:
    return bool(
        identity.get("surface_kind") == "target_group_control_stream"
        and str(identity.get("surface_peer_id") or "") == str(group.tg_peer_id)
        and int(identity.get("viewer_account_id") or 0) == account_id
        and str(identity.get("viewer_session_identity_hash") or "")
        == fact_hash(session_ciphertext)
        and str(identity.get("viewer_authorization_id") or "")
        == str(authorization.id if authorization else "")
        and int(identity.get("viewer_authorization_fact_version") or 0)
        == int(authorization.fact_version or 1 if authorization else 0)
    )


def current_authorization(
    session: Session,
    account_id: int,
) -> TgAccountAuthorization | None:
    return session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.is_current.is_(True),
        TgAccountAuthorization.status == "active",
    ))


def latest_group_cursor(session: Session, group_id: int) -> int:
    values = session.scalars(
        select(GroupContextMessage.remote_message_id)
        .where(GroupContextMessage.group_id == group_id)
        .order_by(GroupContextMessage.id.desc())
        .limit(50)
    )
    cursors = [numeric_cursor(value) for value in values]
    return max((value for value in cursors if value is not None), default=1)


def max_cursor(messages: list, baseline: int) -> int:
    values = [
        numeric_cursor(getattr(message, "remote_message_id", ""))
        for message in messages
    ]
    return max((value for value in values if value is not None), default=baseline)


def numeric_cursor(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def fact_hash(value) -> str:
    payload = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def unusable_telegram_error(exc: Exception) -> bool:
    detail = f"{type(exc).__name__}:{exc}".lower()
    return any(code in detail for code in (
        "session_revoked",
        "session_unauthorized",
        "session_expired",
        "auth_key_unregistered",
        "need_relogin",
        "relogin",
        "user_deactivated",
        "account_banned",
        "channel_invalid",
        "channel_private",
        "chat_id_invalid",
        "chat_write_forbidden",
        "user_banned_in_channel",
        "user_not_participant",
        "peer_id_invalid",
    ))


__all__ = [
    "ProbeSurface",
    "current_authorization",
    "fact_hash",
    "latest_group_cursor",
    "max_cursor",
    "numeric_cursor",
    "surface_identity",
    "surface_is_current",
    "unusable_telegram_error",
]
