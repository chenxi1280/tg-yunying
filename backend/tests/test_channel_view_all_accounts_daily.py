from __future__ import annotations

from datetime import date, datetime, timedelta
import pytest

pytestmark = pytest.mark.no_postgres

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    OperationTarget,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.channel_fulfillment import confirm_view_obligation
from app.services.task_center.channel_view_targets import ensure_channel_view_targets
from app.services.task_center.executors.channel_view import build_plan
from app.timezone import BEIJING_TZ
from tests.channel_view_coverage_support import (
    add_message,
    confirm_actions,
    new_session,
    seed_channel_scenario,
    view_actions,
)


def _set_view_clock(monkeypatch, value: datetime) -> None:
    monkeypatch.setattr("app.services.task_center.executors.channel_view._now", lambda: value)
    monkeypatch.setattr("app.services.task_center.daily_ledgers._now", lambda: value)


def _create_view_task(
    session: Session,
    *,
    channel: OperationTarget,
    messages: list[ChannelMessage],
    task_id: str,
    per_message_daily_target: int | None = None,
    total_target: int = 0,
    message_scope: str = "specific",
) -> Task:
    type_config = {
        "target_channel_id": channel.id,
        "message_scope": message_scope,
        "message_ids": [message.id for message in messages],
        "per_message_total_view_target": total_target,
        "message_active_days": 7,
        "account_coverage_mode": "all_accounts_daily",
    }
    if per_message_daily_target is not None:
        type_config["per_message_daily_view_target"] = per_message_daily_target
        type_config["target_views_per_message"] = per_message_daily_target

    task = Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="channel_view",
        status="running",
        timezone="Asia/Shanghai",
        account_config={"selection_mode": "all"},
        pacing_config={
            "mode": "fixed",
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        },
        type_config=type_config,
    )
    session.add(task)
    session.commit()
    return task


def test_channel_view_dynamic_all_accounts_daily_coverage(monkeypatch):
    """验证取消固定每日目标后，系统自动使用全量可用账号池（10个账号）每日全覆盖浏览。"""
    day1 = datetime(2026, 9, 2, 10, 0, tzinfo=BEIJING_TZ)
    day2 = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING_TZ)

    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=101, account_count=10)
        channel = scenario.channel

        msg1 = add_message(session, channel=channel, message_id=1, published_at=day1 - timedelta(hours=1))

        # per_message_daily_view_target is None -> should dynamically cover all 10 accounts
        task = _create_view_task(
            session,
            channel=channel,
            messages=[msg1],
            task_id="task-view-all-accounts-dynamic",
            per_message_daily_target=None,
            total_target=0,
        )

        # --- DAY 1 ---
        _set_view_clock(monkeypatch, day1)
        created1 = build_plan(session, task)
        assert created1 == 10, f"Expected 10 actions for 10 accounts, got {created1}"

        actions1 = view_actions(session, task)
        assert len(actions1) == 10
        assert {a.account_id for a in actions1} == set(range(1, 11))

        # Confirm actions
        confirm_actions(scenario, actions=actions1, confirmed_at=day1)

        # Second build_plan on Day 1 -> 0 new actions (all accounts covered for Day 1)
        created1_again = build_plan(session, task)
        assert created1_again == 0

        # --- DAY 2 ---
        _set_view_clock(monkeypatch, day2)
        created2 = build_plan(session, task)
        assert created2 == 10, f"Expected 10 actions on Day 2, got {created2}"

        actions2 = [a for a in view_actions(session, task) if a.id not in {x.id for x in actions1}]
        assert len(actions2) == 10
        assert {a.account_id for a in actions2} == set(range(1, 11))


def test_channel_view_dynamic_new_messages_multi_message_all_accounts(monkeypatch):
    """验证多条消息时，每个账号对每条消息各浏览一次。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, now)

    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=102, account_count=5)
        channel = scenario.channel

        msg1 = add_message(session, channel=channel, message_id=10, published_at=now - timedelta(hours=2))
        msg2 = add_message(session, channel=channel, message_id=11, published_at=now - timedelta(hours=1))

        task = _create_view_task(
            session,
            channel=channel,
            messages=[msg1, msg2],
            task_id="task-view-multi-msg-all-accounts",
            per_message_daily_target=None,
            total_target=0,
        )

        created = build_plan(session, task)
        # 2 messages * 5 accounts = 10 actions
        assert created == 10

        actions = view_actions(session, task)
        assert len(actions) == 10
        msg1_accounts = {a.account_id for a in actions if a.payload.get("channel_message_id") == msg1.id}
        msg2_accounts = {a.account_id for a in actions if a.payload.get("channel_message_id") == msg2.id}
        assert msg1_accounts == set(range(1, 6))
        assert msg2_accounts == set(range(1, 6))
