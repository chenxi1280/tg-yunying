from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/task_fulfillment_e4_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_module():
    spec = importlib.util.spec_from_file_location("task_fulfillment_e4_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_group_daily_snapshot_requires_due_coverage_and_new_remote_fact():
    module = load_module()
    snapshot = {
        "task_id": "ai-task",
        "task_type": "group_ai_chat",
        "task_status": "running",
        "ledger_id": "ledger-ai",
        "planner_runtime_error": None,
        "attempts": {"post_release_remote_success_count": 1},
        "group_daily": {
            "target_row_count": 1,
            "due_message_count": 3,
            "confirmed_message_count": 3,
            "coverage_required_count": 2,
            "coverage_confirmed_count": 2,
        },
    }

    assert module.e4_blockers(snapshot) == []

    snapshot["group_daily"]["confirmed_message_count"] = 2
    snapshot["attempts"]["post_release_remote_success_count"] = 0
    assert module.e4_blockers(snapshot) == [
        "ai_daily_due_unmet",
        "ai_post_release_remote_fact_missing",
    ]


def test_search_and_view_snapshots_require_typed_remote_evidence():
    module = load_module()
    search = {
        "task_id": "search-task",
        "task_type": "search_click",
        "task_status": "running",
        "ledger_id": "ledger-search",
        "planner_runtime_error": None,
        "search_click": {"required_count": 2, "confirmed_count": 1, "post_release_confirmed_count": 0},
    }
    view = {
        "task_id": "view-task",
        "task_type": "channel_view",
        "task_status": "running",
        "ledger_id": "ledger-view",
        "planner_runtime_error": None,
        "channel_view": {
            "required_count": 1,
            "materialized_count": 1,
            "source_state": "active",
            "confirmed_count": 1,
            "remote_fact_count": 0,
            "post_release_remote_fact_count": 0,
        },
    }

    assert module.e4_blockers(search) == ["search_click_unmet", "search_click_post_release_fact_missing"]
    assert module.e4_blockers(view) == ["channel_view_remote_fact_missing", "channel_view_post_release_fact_missing"]


def test_view_due_gap_and_source_wait_are_not_reported_as_missing_obligations():
    module = load_module()
    gap = {
        "task_type": "channel_view",
        "task_status": "running",
        "ledger_id": "ledger-view",
        "planner_runtime_error": None,
        "channel_view": {
            "required_count": 1370,
            "materialized_count": 31,
            "source_state": "active",
            "confirmed_count": 0,
            "remote_fact_count": 0,
            "post_release_remote_fact_count": 0,
        },
    }
    waiting = {
        **gap,
        "channel_view": {
            "required_count": 0,
            "materialized_count": 0,
            "source_state": "waiting_for_source",
            "confirmed_count": 0,
            "remote_fact_count": 0,
            "post_release_remote_fact_count": 0,
        },
    }

    assert module.e4_blockers(gap) == [
        "channel_view_due_unmaterialized",
        "channel_view_unmet",
        "channel_view_post_release_fact_missing",
    ]
    assert module.e4_blockers(waiting) == [
        "channel_view_waiting_for_source",
        "channel_view_post_release_fact_missing",
    ]


def test_view_structural_capacity_shortfall_is_explicit() -> None:
    module = load_module()
    snapshot = {
        "task_type": "channel_view",
        "task_status": "running",
        "ledger_id": "ledger-view",
        "planner_runtime_error": None,
        "channel_view": {
            "required_count": 1000,
            "materialized_count": 869,
            "source_state": "active",
            "capacity_warning": "每条消息目标浏览 1000，当前参与账号 869 个",
            "confirmed_count": 869,
            "remote_fact_count": 869,
            "post_release_remote_fact_count": 1,
        },
    }

    assert module.e4_blockers(snapshot) == [
        "channel_view_structural_capacity_shortfall",
        "channel_view_due_unmaterialized",
        "channel_view_unmet",
    ]


def test_view_source_overage_cannot_hide_another_source_deficit() -> None:
    module = load_module()
    snapshot = {
        "task_type": "channel_view",
        "task_status": "running",
        "ledger_id": "ledger-view",
        "planner_runtime_error": None,
        "channel_view": {
            "required_count": 20,
            "materialized_count": 20,
            "materialization_gap": 10,
            "confirmation_gap": 10,
            "remote_fact_gap": 0,
            "source_state": "active",
            "confirmed_count": 20,
            "remote_fact_count": 20,
            "post_release_remote_fact_count": 1,
        },
    }

    assert module.e4_blockers(snapshot) == [
        "channel_view_due_unmaterialized",
        "channel_view_unmet",
    ]


def test_view_due_uses_ledger_target_snapshot_after_config_change_and_expiry() -> None:
    module = load_module()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime.now(timezone.utc)
    with Session(engine) as session:
        task, ledger, target = _view_target_fixture(now_value)
        session.add_all([task, ledger, target])
        session.flush()

        due = module._view_due_snapshot(session, task, ledger)

    assert due["expected_due_count"] == 10
    assert due["source_message_count"] == 1
    assert due["source_state"] == "active"


def test_view_message_snapshot_reads_channel_message_evidence() -> None:
    module = load_module()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime.now(timezone.utc)
    with Session(engine) as session:
        task, ledger, _target = _view_target_fixture(now_value)
        message = ChannelMessage(
            id=901,
            tenant_id=1,
            channel_target_id=7,
            message_id=1901,
            published_at=now_value,
        )
        obligation = ViewFulfillmentObligation(
            tenant_id=1,
            task_day_ledger_id=ledger.id,
            channel_message_id=message.id,
            account_id=31,
            status="open",
        )
        session.add_all([task, ledger, message, obligation])
        session.flush()

        rows = module._view_message_snapshot(session, ledger)

    assert rows[0]["remote_message_id"] == 1901


def _view_target_fixture(
    now_value: datetime,
) -> tuple[Task, TaskDayLedger, ChannelViewDailyMessageTarget]:
    task = Task(
        id="view-target-task",
        tenant_id=1,
        name="view",
        type="channel_view",
        type_config={"per_message_daily_view_target": 1},
        pacing_config={"mode": "fixed", "interval_seconds_min": 0},
    )
    ledger = TaskDayLedger(
        id="view-target-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date.today(),
        period_start_at=now_value - timedelta(hours=10),
        deadline_at=now_value + timedelta(hours=1),
        day_phase="active",
        planning_anchor_at=now_value - timedelta(hours=10),
    )
    target = ChannelViewDailyMessageTarget(
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_peer_id="-1001",
        channel_message_id=901,
        target_revision=1,
        daily_target_snapshot=10,
        total_target_snapshot=100,
        lifetime_confirmed_at_attach=90,
        ledger_confirmed_at_attach=0,
        effective_target_snapshot=10,
        accrual_anchor_at=ledger.period_start_at,
        active_until=now_value - timedelta(minutes=1),
        due_count=10,
        source_state="expired",
    )
    return task, ledger, target


def test_missing_ledger_and_current_planner_error_fail_closed():
    module = load_module()
    snapshot = {
        "task_id": "task-x",
        "task_type": "group_ai_chat",
        "task_status": "running",
        "ledger_id": None,
        "planner_runtime_error": {"message": "planner exploded"},
        "attempts": {"post_release_remote_success_count": 0},
        "group_daily": {},
    }

    assert module.e4_blockers(snapshot) == ["task_day_ledger_missing", "planner_runtime_error"]


def test_business_attempts_are_scoped_to_the_task_action_type():
    module = load_module()

    assert module.BUSINESS_ACTION_TYPES == {
        "group_ai_chat": "send_message",
        "search_click": "search_join",
        "channel_view": "view_message",
    }
