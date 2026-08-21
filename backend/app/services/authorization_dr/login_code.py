from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.integrations.telegram import VerificationCodeSnapshot
from app.timezone import as_beijing

from .contracts import AuthorizationDrError


@dataclass(frozen=True)
class BoundLoginCode:
    code: str
    message_id: str
    received_at: datetime


def bind_login_code(
    snapshots: list[VerificationCodeSnapshot],
    *,
    challenge_sent_at: datetime,
    expected_message_id: str = "",
) -> BoundLoginCode | None:
    cutoff = as_beijing(challenge_sent_at)
    eligible = [item for item in snapshots if _received_at(item) >= cutoff]
    if expected_message_id:
        matches = [item for item in eligible if item.message_id == expected_message_id]
        if len(matches) > 1:
            raise AuthorizationDrError("login_code_challenge_mismatch", "Bound login code message is duplicated")
        return _bound(matches[0]) if matches else None
    if len(eligible) > 1:
        raise AuthorizationDrError("login_code_challenge_mismatch", "Multiple login codes match the challenge window")
    return _bound(eligible[0]) if eligible else None


def _received_at(snapshot: VerificationCodeSnapshot) -> datetime:
    received = as_beijing(snapshot.received_at)
    if not snapshot.message_id or received is None:
        raise AuthorizationDrError("login_code_challenge_mismatch", "Login code message metadata is incomplete")
    return received


def _bound(snapshot: VerificationCodeSnapshot) -> BoundLoginCode:
    return BoundLoginCode(snapshot.code, snapshot.message_id, _received_at(snapshot))


__all__ = ["BoundLoginCode", "bind_login_code"]
