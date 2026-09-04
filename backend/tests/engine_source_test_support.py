from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ChannelMessage, OperationTarget, Task, TaskDayLedger, Tenant, TgAccount
from app.timezone import BEIJING_TZ


NOW = datetime(2026, 9, 4, 12)


def seed_source_session(*, task_type="channel_like", accounts=3):
    session = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(session.get_bind())
    session.add(Tenant(id=1, name="test"))
    session.add(OperationTarget(id=1, tenant_id=1, target_type="channel", tg_peer_id="-1001",
        title="channel", username="public", reaction_capability_mode="all", available_reactions=["👍"]))
    members = [TgAccount(id=i, tenant_id=1, display_name=f"account {i}", phone_masked="***", status="在线") for i in range(1, accounts + 1)]
    session.add_all(members)
    task = Task(id="task", tenant_id=1, name="test", type=task_type, status="running",
        created_at=NOW, task_lifecycle_epoch=1, config_revision=1,
        fulfillment_contract_version="fact_first_v3", stats={"started_at": NOW.isoformat()},
        type_config={"engagement_contract_version": "unified_engagement_v1", "target_channel_id": 1,
                     "initial_historical_post_limit": 5, "daily_reaction_cap": 1000},
        pacing_config={"daily_start_time": "00:00", "daily_end_time": "23:59"})
    session.add(task)
    ledger = TaskDayLedger(id="day", tenant_id=1, task_id=task.id, timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=NOW.date(), period_start_at=_utc(NOW.replace(hour=0)),
        deadline_at=_utc(NOW.replace(hour=0)+timedelta(days=1)), day_phase="partial_day", planning_anchor_at=_utc(NOW))
    session.add(ledger)
    session.commit()
    return session, task, ledger, members


def _utc(value):
    return value.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)


def message(session, message_id, *, at=None, album="", metadata=None):
    row = ChannelMessage(id=message_id, tenant_id=1, channel_target_id=1, message_id=message_id,
        content_preview="hello", published_at=at or NOW-timedelta(minutes=1), created_at=NOW,
        grouped_id=album, source_metadata={"observed": True, **(metadata or {})})
    session.add(row)
    session.flush()
    return row
