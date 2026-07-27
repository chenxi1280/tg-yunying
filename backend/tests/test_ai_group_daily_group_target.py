from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.daily_group_target import (
    daily_group_due_message_count,
    ensure_task_group_daily_target,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed(session: Session, *, configured: int, account_count: int) -> tuple[Task, TgGroup]:
    session.add(Tenant(id=1, name="租户"))
    group = TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群")
    target = OperationTarget(
        id=31,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-10021",
        title="目标群",
        auth_status="已授权运营",
        can_send=True,
    )
    task = Task(
        id="daily-target-task",
        tenant_id=1,
        name="日目标任务",
        type="group_ai_chat",
        status="running",
        scheduled_start=datetime(2026, 7, 27, 12),
        type_config={
            "target_group_id": group.id,
            "target_operation_target_id": target.id,
            "daily_message_target": configured,
            "account_coverage_mode": "all_accounts_daily",
        },
    )
    session.add_all([group, target, task])
    session.flush()
    for account_id in range(1, account_count + 1):
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"账号{account_id}",
                phone_masked=f"***{account_id:04d}",
            )
        )
        session.add(
            TaskMembershipAdmissionItem(
                id=account_id,
                tenant_id=1,
                task_id=task.id,
                account_id=account_id,
                target_id=target.id,
                phase="active",
            )
        )
    session.flush()
    return task, group


def test_daily_target_uses_frozen_account_count_as_floor(session: Session) -> None:
    task, group = _seed(session, configured=2, account_count=3)

    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        date(2026, 7, 28),
        now=datetime(2026, 7, 28, 10),
    )

    assert target.configured_message_target == 2
    assert target.frozen_account_count == 3
    assert target.effective_message_target == 3


def test_daily_target_keeps_larger_operator_total(session: Session) -> None:
    task, group = _seed(session, configured=5, account_count=3)

    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        date(2026, 7, 28),
        now=datetime(2026, 7, 28, 10),
    )

    assert target.effective_message_target == 5


def test_midday_start_is_warming_until_next_natural_day(session: Session) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    task.scheduled_start = datetime(2026, 7, 28, 12)

    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        date(2026, 7, 28),
        now=datetime(2026, 7, 28, 12),
    )

    assert target.daily_fulfillment_phase == "admission_warming"
    assert target.full_day_committed_at == datetime(2026, 7, 29)


def test_midday_start_makes_first_message_due_immediately(session: Session) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    timestamp = datetime(2026, 7, 28, 12)
    task.scheduled_start = timestamp
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        timestamp.date(),
        now=timestamp,
    )

    assert daily_group_due_message_count(target, {}, now=timestamp) == 1


def test_account_coverage_target_is_always_one(session: Session) -> None:
    task, group = _seed(session, configured=10, account_count=1)
    item = session.get(TaskMembershipAdmissionItem, 1)
    row = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=group.id,
        account_id=1,
        membership_item_id=item.id,
        coverage_date=date(2026, 7, 28),
        target_count=1,
    )
    session.add(row)
    session.flush()

    assert row.target_count == 1


def test_daily_target_counts_only_success_attempt_with_remote_id(session: Session) -> None:
    task, group = _seed(session, configured=2, account_count=1)
    executed_at = datetime(2026, 7, 28, 12)
    actions = [
        Action(
            id=f"action-{index}",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=1,
            status="success",
            executed_at=executed_at,
            payload={
                "ai_message_memory_id": "memory-1",
                "content_source": "account_mask",
                "account_mask_id": "mask-1",
                "account_mask_version": 1,
                "voice_profile_contract_version": "style_only_v2",
                "account_mask_snapshot_hash": "mask-hash-1",
            } if index == 1 else {},
        )
        for index in range(1, 4)
    ]
    session.add_all(actions)
    session.flush()
    session.add(AiGroupMessageMemory(
        id="memory-1",
        tenant_id=1,
        group_id=group.id,
        task_id=task.id,
        action_id="action-1",
        account_id=1,
        raw_text="今天聊点新鲜的",
        normalized_text="今天聊点新鲜的",
        text_fingerprint="memory-1",
        status="success",
        planned_at=executed_at,
        account_mask_id="mask-1",
        account_mask_version=1,
        mask_contract_version="style_only_v2",
        mask_snapshot_hash="mask-hash-1",
        mask_status="active",
        content_source="account_mask",
    ))
    session.add_all([
        ExecutionAttempt(
            tenant_id=1, action_id="action-1", account_id=1, attempt_no=1,
            status="success", remote_message_id="remote-1",
        ),
        ExecutionAttempt(
            tenant_id=1, action_id="action-2", account_id=1, attempt_no=1,
            status="success", remote_message_id="",
        ),
        ExecutionAttempt(
            tenant_id=1, action_id="action-3", account_id=1, attempt_no=1,
            status="failed", remote_message_id="remote-3",
        ),
    ])
    session.flush()

    target = ensure_task_group_daily_target(
        session, task, group, date(2026, 7, 28), now=executed_at,
    )

    assert target.confirmed_message_count == 1
