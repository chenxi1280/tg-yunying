from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Task


def build_plan(session: Session, task: Task) -> int:
    return 0
