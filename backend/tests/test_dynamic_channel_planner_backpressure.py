from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Action, ChannelMessage, OperationTarget, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import stats
from app.services.task_center.executors import common
from app.services.task_center.executors.channel_like import build_plan as build_channel_like_plan
from app.services.task_center.executors import channel_view
from app.services.task_center.executors.channel_action_history import channel_message_success_counts, channel_view_daily_action_counts


pytestmark = pytest.mark.no_postgres


def test_dynamic_channel_next_run_uses_beijing_scheduler_clock(monkeypatch) -> None:
    beijing_now = datetime(2026, 7, 19, 4, 0)
    task = Task(
        id="dynamic-channel-timezone",
        tenant_id=1,
        name="动态频道时间基准",
        type="channel_like",
        status="running",
        type_config={"message_scope": "dynamic_new", "listener_interval_seconds": 30},
    )

    monkeypatch.setattr(stats, "_now", lambda: beijing_now)

    assert stats.next_run_after_task(task) == datetime(2026, 7, 19, 4, 0, 30)


def test_channel_message_account_ids_are_loaded_once_for_all_messages() -> None:
    selector = getattr(common, "channel_message_account_ids_for_messages", None)

    assert selector is not None, "missing batched channel action lookup"

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[tuple[str, object]] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "SELECT actions.account_id, JSON_EXTRACT(actions.payload" in statement:
            statements.append((statement, _parameters))

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        task = Task(id="channel-history-batch", tenant_id=1, name="频道历史批量", type="channel_like", status="running")
        first = ChannelMessage(id=11, tenant_id=1, channel_target_id=21, message_id=6101)
        second = ChannelMessage(id=12, tenant_id=1, channel_target_id=21, message_id=6102)
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            task,
            first,
            second,
            Action(
                id="history-first",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=101,
                status="success",
                payload={"channel_message_id": first.id},
            ),
            Action(
                id="history-second",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=102,
                status="success",
                payload={"message_id": second.message_id},
            ),
            Action(
                id="history-skipped",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=103,
                status="skipped",
                payload={"channel_message_id": second.id},
                result={"error_code": "reaction_unavailable_message"},
            ),
            Action(
                id="history-both-identifiers",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=104,
                status="success",
                payload={"channel_message_id": first.id, "message_id": second.message_id},
            ),
            Action(
                id="unrelated-history",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=105,
                status="success",
                payload={"channel_message_id": 9999, "message_id": 9999},
            ),
        ])
        session.commit()
        statements.clear()

        account_ids = selector(
            session,
            task,
            "like_message",
            [first, second],
            include_skipped_codes={"reaction_unavailable_message"},
        )

    assert account_ids == {first.id: {101, 104}, second.id: {102, 103, 104}}
    assert len(statements) == 1
    assert "channel_message_id" in repr(statements[0])
    assert "message_id" in repr(statements[0])
    assert "UNION ALL" in statements[0][0]
    assert " OR " not in statements[0][0]


def test_channel_like_planner_reads_message_history_once() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[tuple[str, object]] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "SELECT actions.account_id, JSON_EXTRACT(actions.payload" in statement:
            statements.append((statement, _parameters))

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        account = TgAccount(
            id=101,
            tenant_id=1,
            display_name="点赞号",
            phone_masked="101",
            status=AccountStatus.ACTIVE.value,
            health_score=100,
            session_ciphertext="session-101",
        )
        channel = OperationTarget(
            id=21,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10021",
            title="点赞频道",
            username="like_channel",
            can_send=True,
            auth_status="已授权运营",
        )
        first = ChannelMessage(id=11, tenant_id=1, channel_target_id=channel.id, message_id=6101)
        second = ChannelMessage(id=12, tenant_id=1, channel_target_id=channel.id, message_id=6102)
        task = Task(
            id="channel-like-history-once",
            tenant_id=1,
            name="点赞历史单次读取",
            type="channel_like",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [account.id], "max_concurrent": 1},
            type_config={
                "target_channel_id": channel.id,
                "message_scope": "specific",
                "message_ids": [first.id, second.id],
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
            },
        )
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            account,
            channel,
            first,
            second,
            task,
            Action(
                id="like-first",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=account.id,
                status="success",
                payload={"channel_message_id": first.id},
            ),
            Action(
                id="like-second",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=account.id,
                status="success",
                payload={"channel_message_id": second.id},
            ),
        ])
        session.commit()
        statements.clear()

        assert build_channel_like_plan(session, task) == 0

    assert len(statements) == 1
    assert "channel_message_id" in repr(statements[0])
    assert "UNION ALL" in statements[0][0]


def test_channel_view_planner_creates_actions_with_batched_history(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(channel_view, "gate_channel_membership", lambda *_args, **_kwargs: SimpleNamespace(ready=True, created=0))
    monkeypatch.setattr(channel_view, "channel_member_accounts", lambda _session, _task, _channel, accounts: accounts)

    with Session(engine) as session:
        first = ChannelMessage(id=31, tenant_id=1, channel_target_id=41, message_id=7101)
        second = ChannelMessage(id=32, tenant_id=1, channel_target_id=41, message_id=7102)
        task = Task(
            id="channel-view-batched-history",
            tenant_id=1,
            name="浏览批量历史",
            type="channel_view",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [201, 202], "max_concurrent": 2},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "target_channel_id": 41,
                "message_scope": "specific",
                "message_ids": [first.id, second.id],
                "per_message_daily_view_target": 1,
                "per_message_total_view_target": 1,
                "task_daily_view_safety_cap": 2,
                "max_views_per_account_per_day": 1,
                "view_count_jitter": 0,
            },
        )
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            TgAccount(id=201, tenant_id=1, display_name="浏览号一", phone_masked="201", status=AccountStatus.ACTIVE.value, health_score=100, session_ciphertext="session-201"),
            TgAccount(id=202, tenant_id=1, display_name="浏览号二", phone_masked="202", status=AccountStatus.ACTIVE.value, health_score=99, session_ciphertext="session-202"),
            OperationTarget(id=41, tenant_id=1, target_type="channel", tg_peer_id="-10041", title="浏览频道", username="view_channel", can_send=True, auth_status="已授权运营"),
            first,
            second,
            task,
        ])
        session.commit()

        assert channel_view.build_plan(session, task) == 2
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert {action.payload["channel_message_id"] for action in actions} == {first.id, second.id}
    assert {action.payload["execution_date"] for action in actions} == {_now().date().isoformat()}


def test_channel_view_history_batches_success_and_daily_capacity() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    execution_date = _now().date().isoformat()
    previous_date = (_now() - timedelta(days=1)).date().isoformat()

    with Session(engine) as session:
        task = Task(id="channel-view-history-batch", tenant_id=1, name="浏览历史聚合", type="channel_view", status="running")
        first = ChannelMessage(id=51, tenant_id=1, channel_target_id=61, message_id=8101)
        second = ChannelMessage(id=52, tenant_id=1, channel_target_id=61, message_id=8102)
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            task,
            first,
            second,
            Action(id="view-success-today", tenant_id=1, task_id=task.id, task_type=task.type, action_type="view_message", account_id=301, status="success", payload={"channel_message_id": first.id, "execution_date": execution_date}),
            Action(id="view-success-before", tenant_id=1, task_id=task.id, task_type=task.type, action_type="view_message", account_id=302, status="success", payload={"channel_message_id": first.id, "execution_date": previous_date}),
            Action(id="view-pending-today", tenant_id=1, task_id=task.id, task_type=task.type, action_type="view_message", account_id=301, status="pending", payload={"channel_message_id": second.id, "execution_date": execution_date}),
            Action(id="view-other-message", tenant_id=1, task_id=task.id, task_type=task.type, action_type="view_message", account_id=302, status="success", payload={"channel_message_id": 9999, "execution_date": execution_date}),
            Action(id="view-failed-today", tenant_id=1, task_id=task.id, task_type=task.type, action_type="view_message", account_id=303, status="failed", payload={"channel_message_id": first.id, "execution_date": execution_date}),
        ])
        session.commit()

        success_counts = channel_message_success_counts(session, task, "view_message", [first, second])
        daily_counts = channel_view_daily_action_counts(session, task, execution_date)

    assert success_counts == {first.id: 2}
    assert daily_counts.total == 3
    assert daily_counts.by_account == {301: 2, 302: 1}


def test_dynamic_channel_planner_backpressure_migration_repairs_schedule_and_indexes_recovery() -> None:
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0107_dynamic_channel_planner_backpressure.py"

    assert migration.exists(), "missing dynamic channel planner repair migration"

    source = migration.read_text()
    for expected in (
        "dynamic_new",
        "channel_view",
        "channel_like",
        "channel_comment",
        "next_run_at",
        "ix_task_daily_coverage_recovery_terminal",
        "reserved_action_id",
    ):
        assert expected in source


def test_channel_planner_history_backpressure_migration_declares_bounded_lookup_indexes() -> None:
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0109_channel_planner_history_backpressure.py"

    assert migration.exists(), "missing channel planner history backpressure migration"

    source = migration.read_text()
    for expected in (
        "ix_actions_channel_planner_message_history",
        "ix_actions_channel_planner_legacy_history",
        "ix_actions_channel_view_daily_capacity",
        "channel_message_id",
        "message_id",
        "execution_date",
    ):
        assert expected in source
