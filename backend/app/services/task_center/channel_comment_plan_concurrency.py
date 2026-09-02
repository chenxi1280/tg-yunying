from __future__ import annotations

from sqlalchemy.exc import IntegrityError


PLAN_UNIQUENESS_CONSTRAINTS = frozenset({
    "uq_channel_comment_plan_active",
    "uq_channel_comment_plan_revision",
})


def active_plan_conflict(exc: IntegrityError) -> bool:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
    if constraint in PLAN_UNIQUENESS_CONSTRAINTS:
        return True
    message = str(exc.orig)
    sqlite_columns = (
        "channel_comment_plan_contracts.task_id, "
        "channel_comment_plan_contracts.channel_message_id"
    )
    return any(name in message for name in PLAN_UNIQUENESS_CONSTRAINTS) or (
        sqlite_columns in message
    )
