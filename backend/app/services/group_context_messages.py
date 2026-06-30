from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GroupContextMessage


CONTEXT_MESSAGE_DUPLICATE_ERROR_MARKERS = (
    "group_context_messages_group_id_remote_message_id_key",
    "group_context_messages.group_id, group_context_messages.remote_message_id",
)


def try_insert_context_message(session: Session, message: GroupContextMessage) -> bool:
    try:
        with session.begin_nested():
            session.add(message)
            session.flush()
    except IntegrityError as exc:
        if _is_duplicate_context_message_error(exc):
            return False
        raise
    return True


def _is_duplicate_context_message_error(exc: IntegrityError) -> bool:
    detail = " ".join(str(part or "") for part in (exc.orig, exc.statement, exc.params))
    return any(marker in detail for marker in CONTEXT_MESSAGE_DUPLICATE_ERROR_MARKERS)
