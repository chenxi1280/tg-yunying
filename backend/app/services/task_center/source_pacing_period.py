from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import ChannelMessage, TaskDayLedger

from .datetime_compat import utc_storage_as_beijing_wall
from .source_pacing import wall_datetime


def source_period(
    session: Session,
    owner,
    domain: str,
) -> tuple[datetime, datetime, str]:
    if domain in {"ai_send", "view"}:
        ledger = session.get(TaskDayLedger, str(owner.task_day_ledger_id or ""))
        if ledger is None:
            raise LookupError("pacing_source_ledger_missing")
        return (
            utc_storage_as_beijing_wall(ledger.period_start_at),
            utc_storage_as_beijing_wall(ledger.deadline_at),
            str(getattr(owner, "pacing_period_key", None) or ledger.id),
        )
    message = session.get(ChannelMessage, int(owner.channel_message_id or 0))
    if message is None:
        raise LookupError("pacing_source_message_missing")
    period_start = wall_datetime(message.created_at)
    return period_start, period_start + timedelta(days=1), f"message:{message.id}"


__all__ = ["source_period"]
