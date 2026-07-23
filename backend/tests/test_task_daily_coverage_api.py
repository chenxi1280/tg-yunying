from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routers.task_center import router
from app.database import Base
from app.models import AccountPool, Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgGroup
from app.schemas.task_center import GroupAIChatTaskCreate, TaskAccountCoverageItemOut
from app.security import encrypt_session
from app.services.task_center.account_coverage import list_task_account_coverage_page, task_account_coverage
from app.services.task_center.coverage_capacity import coverage_capacity_proof
from app.services.task_center.precheck import _daily_coverage_capacity_check
from app.timezone import beijing_now


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_capacity_proof_blocks_impossible_group_daily_target() -> None:
    group = TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=120,
        group_cooldown_seconds=60,
    )

    proof = coverage_capacity_proof(
        group=group,
        target_account_count=609,
        target_per_account=1,
        max_actions_per_hour=100,
        account_day_limit=400,
        account_hour_limit=50,
        account_cooldown_seconds=180,
    )

    assert proof["required_daily_messages"] == 609
    assert proof["effective_daily_capacity"] == 120
    assert proof["sufficient"] is False
    assert "group_daily_limit" in proof["blockers"]
    assert proof["blocker_code"] == "daily_coverage_capacity_insufficient"


def test_capacity_proof_uses_total_active_window_task_capacity() -> None:
    group = TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=100,
    )

    proof = coverage_capacity_proof(
        group=group,
        target_account_count=6,
        target_per_account=1,
        max_actions_per_hour=100,
        account_day_limit=100,
        account_hour_limit=100,
        account_cooldown_seconds=0,
        daily_task_capacity=5,
    )

    assert proof["capacity_dimensions"]["task_schedule"] == 5
    assert proof["sufficient"] is False
    assert "task_schedule" in proof["blockers"]


def test_capacity_proof_blocks_when_remaining_group_cooldown_slots_cannot_finish_coverage() -> None:
    group = TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=675,
        group_cooldown_seconds=60,
    )

    proof = coverage_capacity_proof(
        group=group,
        target_account_count=675,
        target_per_account=1,
        confirmed_message_count=20,
        reserved_message_count=140,
        max_actions_per_hour=120,
        account_day_limit=400,
        account_hour_limit=50,
        account_cooldown_seconds=180,
        daily_task_capacity=840,
        occupied_group_actions=160,
        occupied_task_actions=160,
        pending_group_actions=140,
        pending_task_actions=140,
        now=datetime(2026, 7, 23, 15, 15),
    )

    assert proof["remaining_active_window_seconds"] == 7 * 60 * 60 + 45 * 60
    assert proof["capacity_dimensions"]["group_cooldown"] == 326
    assert proof["sufficient"] is False
    assert "group_cooldown" in proof["blockers"]
    assert proof["capacity_gap"] == 189


def test_capacity_proof_does_not_require_confirmed_messages_twice() -> None:
    group = TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=580,
    )

    proof = coverage_capacity_proof(
        group=group,
        target_account_count=580,
        target_per_account=1,
        confirmed_message_count=140,
        max_actions_per_hour=100,
        account_day_limit=400,
        account_hour_limit=50,
        account_cooldown_seconds=0,
        daily_task_capacity=580,
        occupied_group_actions=140,
        occupied_task_actions=140,
    )

    assert proof["required_daily_messages"] == 580
    assert proof["remaining_required_messages"] == 440
    assert proof["capacity_dimensions"]["group_daily_limit"] == 440
    assert proof["sufficient"] is True


def test_capacity_proof_does_not_require_reserved_messages_twice() -> None:
    group = TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=580,
    )

    proof = coverage_capacity_proof(
        group=group,
        target_account_count=580,
        target_per_account=1,
        confirmed_message_count=140,
        reserved_message_count=10,
        max_actions_per_hour=100,
        account_day_limit=400,
        account_hour_limit=50,
        account_cooldown_seconds=0,
        daily_task_capacity=580,
        occupied_group_actions=150,
        occupied_task_actions=150,
    )

    assert proof["remaining_required_messages"] == 430
    assert proof["capacity_dimensions"]["group_daily_limit"] == 430
    assert proof["sufficient"] is True


def test_precheck_capacity_uses_all_session_ready_accounts(session: Session) -> None:
    session.add(Tenant(id=1, name="租户"))
    session.add(AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True))
    session.add(TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="09:00-23:00",
        daily_limit=1,
    ))
    for account_id in (1, 2):
        session.add(TgAccount(
            id=account_id,
            tenant_id=1,
            pool_id=10,
            display_name=f"账号{account_id}",
            phone_masked=str(account_id),
            account_identity="normal",
            status="在线",
            session_ciphertext=encrypt_session(f"session-{account_id}"),
        ))
    session.commit()
    payload = GroupAIChatTaskCreate(name="容量不足任务", target_group_id=21, hourly_min_messages=10)

    proof = _daily_coverage_capacity_check(
        session,
        tenant_id=1,
        task_type="group_ai_chat",
        create_payload=payload,
        type_config={"target_group_id": 21, "account_coverage_mode": "all_accounts_daily"},
    )

    assert proof["target_account_count"] == 2
    assert proof["sufficient"] is False
    assert proof["blocker_code"] == "daily_coverage_capacity_insufficient"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed_coverage(session: Session) -> Task:
    session.add(Tenant(id=1, name="租户"))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群", daily_limit=1))
    task = Task(
        id="coverage-api",
        tenant_id=1,
        name="覆盖可观测性",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all"},
        type_config={"target_group_id": 21, "account_coverage_mode": "all_accounts_daily"},
    )
    session.add(task)
    states = [
        (1, "confirmed", "", 1),
        (2, "blocked", "cannot_send", 0),
        (3, "unknown", "unknown_after_send", 0),
        (4, "ready", "duplicate_message", 0),
    ]
    for account_id, state, blocker, confirmed in states:
        session.add(TgAccount(
            id=account_id,
            tenant_id=1,
            display_name=f"账号{account_id}",
            phone_masked=str(account_id),
            status="在线",
        ))
        session.add(TaskAccountDailyCoverage(
            id=f"row-{account_id}",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=account_id,
            coverage_date=beijing_now().date(),
            target_count=1,
            confirmed_count=confirmed,
            state=state,
            blocker_code=blocker,
        ))
    session.commit()
    return task


def test_coverage_summary_uses_frozen_ledger_denominator(session: Session) -> None:
    task = _seed_coverage(session)

    summary = task_account_coverage(session, task)

    assert summary["target_account_count"] == 4
    assert summary["confirmed_account_count"] == 1
    assert summary["remaining_account_count"] == 3
    assert summary["eligible_count"] == 4
    assert summary["covered_count"] == 1
    assert summary["blocked_count"] == 1
    assert summary["unknown_count"] == 1
    assert summary["unknown_after_send_count"] == 1
    assert summary["remaining_count"] == 3
    assert summary["coverage_percent"] == 25
    assert summary["capacity_proof"]["sufficient"] is False
    assert summary["capacity_status"] == "blocked"
    assert summary["required_daily_messages"] == 4
    assert any(item["reason"] == "cannot_send" and item["count"] == 1 for item in summary["blocked_reasons"])
    assert any(item["reason"] == "daily_coverage_capacity_insufficient" for item in summary["blocked_reasons"])


def test_missing_all_account_ledger_never_falls_back_to_dynamic_percentage(session: Session) -> None:
    session.add(Tenant(id=1, name="租户"))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群"))
    task = Task(
        id="missing-ledger",
        tenant_id=1,
        name="缺失账本",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all"},
        type_config={"target_group_id": 21, "account_coverage_mode": "all_accounts_daily"},
    )
    session.add(task)
    session.commit()

    summary = task_account_coverage(session, task)

    assert summary["mode"] == "all_accounts_daily"
    assert summary["coverage_status"] == "scope_uninitialized"
    assert summary["coverage_percent"] == 0
    assert any(item["reason"] == "coverage_scope_uninitialized" for item in summary["blocked_reasons"])


def test_coverage_detail_page_keeps_blocked_accounts_visible(session: Session) -> None:
    task = _seed_coverage(session)

    rows, total = list_task_account_coverage_page(
        session,
        tenant_id=1,
        task_id=task.id,
        coverage_date=beijing_now().date(),
        state="blocked",
        page=1,
        page_size=20,
    )

    assert total == 1
    assert rows[0]["account_id"] == 2
    assert rows[0]["blocker_code"] == "cannot_send"
    assert TaskAccountCoverageItemOut.model_validate(rows[0]).state == "blocked"


def test_router_exposes_paginated_account_coverage_endpoint() -> None:
    paths = {path for route in router.routes if (path := getattr(route, "path", None))}

    assert "/api/tasks/{task_id}/account-coverage" in paths


def test_frontend_exposes_paginated_blocked_account_coverage_table() -> None:
    types_source = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    view_source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    modal_source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    assert "export type TaskAccountCoverageItem" in types_source
    assert "account-coverage" in view_source
    assert "coverageItems" in modal_source
    assert "blocker_detail" in modal_source
