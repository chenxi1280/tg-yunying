from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
import socket
from typing import Sequence
from uuid import uuid4

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from app.models import Action, Task


RECOVERY_CLAIM_SECONDS = 300


@dataclass(frozen=True)
class RecoveryClaim:
    action_id: str
    token: str


def claim_recovery_actions(
    session: Session,
    *,
    conditions: Sequence[ColumnElement[bool]],
    order_by: Sequence,
    now: datetime,
    limit: int,
) -> list[RecoveryClaim]:
    statement = (
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(
            *conditions,
            Task.status == "running",
            Task.deleted_at.is_(None),
            _claim_available(now),
        )
        .order_by(*order_by)
        .limit(max(1, int(limit)))
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)
    actions = list(session.scalars(statement))
    token = str(uuid4())
    expires_at = now + timedelta(seconds=RECOVERY_CLAIM_SECONDS)
    owner = f"recovery:{socket.gethostname()}:{os.getpid()}"
    for action in actions:
        action.claim_owner = owner
        action.claim_token = token
        action.claim_expires_at = expires_at
    session.commit()
    return [RecoveryClaim(action_id=action.id, token=token) for action in actions]


def recovery_claim_owned(action: Action | None, claim: RecoveryClaim) -> bool:
    return bool(action and action.claim_token == claim.token and action.claim_owner.startswith("recovery:"))


def release_recovery_claim(action: Action, claim: RecoveryClaim) -> bool:
    if not recovery_claim_owned(action, claim):
        return False
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    return True


def _claim_available(now: datetime):
    return or_(
        Action.claim_token == "",
        Action.claim_token.is_(None),
        Action.claim_expires_at.is_(None),
        Action.claim_expires_at <= now,
    )


__all__ = [
    "RecoveryClaim",
    "claim_recovery_actions",
    "recovery_claim_owned",
    "release_recovery_claim",
]
