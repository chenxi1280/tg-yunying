from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Action, ChannelMessage, OperationTarget, Task, Tenant, TgAccount
from app.services.task_center import stats
from app.services.task_center.executors import common
from app.services.task_center.executors.channel_like import build_plan as build_channel_like_plan


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
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "FROM actions" in statement:
            statements.append(statement)

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


def test_channel_like_planner_reads_message_history_once() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "FROM actions" in statement and "actions.account_id, actions.payload, actions.status, actions.result" in statement:
            statements.append(statement)

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
