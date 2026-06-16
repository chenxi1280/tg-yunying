from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Task

from ..membership_admission import (
    lock_membership_admission_snapshot,
    plan_membership_admission_actions,
    plan_membership_admission_delete_messages,
    plan_membership_admission_test_messages,
    sync_membership_admission_items,
)


def build_plan(session: Session, task: Task) -> int:
    lock_membership_admission_snapshot(session, task)
    sync_membership_admission_items(session, task)
    created = len(plan_membership_admission_actions(session, task))
    created += len(plan_membership_admission_test_messages(session, task))
    created += len(plan_membership_admission_delete_messages(session, task))
    return created
