from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelViewDailyMessageTarget


def source_plan_total(
    session: Session,
    owner,
    *,
    domain: str,
    fallback: int,
) -> int:
    if domain != "view":
        return fallback
    total = session.scalar(
        select(func.coalesce(func.sum(ChannelViewDailyMessageTarget.due_count), 0)).where(
            ChannelViewDailyMessageTarget.task_day_ledger_id
            == str(owner.task_day_ledger_id or ""),
            ChannelViewDailyMessageTarget.source_state == "active",
        )
    )
    return int(total or fallback)


__all__ = ["source_plan_total"]
