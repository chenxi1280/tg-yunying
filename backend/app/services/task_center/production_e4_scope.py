"""Scope discovery and PostgreSQL snapshot setup for read-only E4 reporting."""
from sqlalchemy import select, text

from app.models import Task


STATEMENT_TIMEOUT_SECONDS = 20
LOCK_TIMEOUT_SECONDS = 2


def discover_channel_view_task_ids(session) -> list[str]:
    return list(session.scalars(
        select(Task.id)
        .where(
            Task.type == "channel_view",
            Task.status.in_(("running", "completed")),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.updated_at.desc(), Task.id.asc())
    ))


def configure_readonly_snapshot(session) -> None:
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_SECONDS}s'"))
    session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'"))
