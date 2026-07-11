from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.task_center.executors.group_ai_chat import (
    _coverage_capacity_blocker,
    _coverage_plan_state,
    _coverage_round_config,
    _account_shortage_reason,
    _canonicalized_task_config,
    _online_ready_accounts,
    _quality_fallback_enabled,
    _select_accounts_for_plan,
)
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed(session: Session) -> tuple[Task, TgGroup]:
    session.add(Tenant(id=1, name="租户"))
    session.add(AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True))
    group = TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群")
    task = Task(
        id="coverage-planner", tenant_id=1, name="覆盖 Planner", type="group_ai_chat", status="running",
        account_config={"selection_mode": "all", "max_concurrent": 10},
        type_config={
            "target_group_id": 21,
            "account_coverage_mode": "all_accounts_daily",
            "per_account_daily_min_messages": 1,
        },
    )
    session.add_all([group, task])
    for account_id in (1, 2, 3):
        session.add(TgAccount(
            id=account_id, tenant_id=1, pool_id=10, display_name=f"账号{account_id}",
            phone_masked=str(account_id), account_identity="normal", status="在线", health_score=95,
        ))
        session.add(TgGroupAccount(tenant_id=1, group_id=21, account_id=account_id, can_send=True))
    session.add_all([
        _coverage(task, 1, "ready"),
        _coverage(task, 2, "confirmed", confirmed_count=1),
        _coverage(task, 3, "blocked", blocker_code="account_limited"),
    ])
    session.commit()
    return task, group


def _coverage(
    task: Task,
    account_id: int,
    state: str,
    *,
    confirmed_count: int = 0,
    blocker_code: str = "",
) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id=f"coverage-{account_id}",
        tenant_id=1,
        task_id=task.id,
        group_id=21,
        account_id=account_id,
        coverage_date=date.today(),
        target_count=1,
        confirmed_count=confirmed_count,
        state=state,
        blocker_code=blocker_code,
    )


def test_all_account_planner_selects_only_ready_daily_ledger_accounts(session: Session) -> None:
    task, group = _seed(session)

    selected = _select_accounts_for_plan(
        session,
        task,
        group,
        {},
        task.type_config,
    )

    assert [account.id for account in selected] == [1]


def test_all_account_planner_does_not_fall_back_to_platform_scan_without_ready_debt(session: Session) -> None:
    task, group = _seed(session)
    session.get(TaskAccountDailyCoverage, "coverage-1").state = "confirmed"
    session.get(TaskAccountDailyCoverage, "coverage-1").confirmed_count = 1
    session.commit()

    selected = _select_accounts_for_plan(session, task, group, {}, task.type_config)

    assert selected == []


def test_running_all_account_task_blocks_when_daily_capacity_is_insufficient(session: Session) -> None:
    task, group = _seed(session)
    group.daily_limit = 1

    blocker = _coverage_capacity_blocker(session, task, group, task.type_config)

    assert blocker["blocker_code"] == "daily_coverage_capacity_insufficient"
    assert blocker["capacity_gap"] == 1
    assert task.stats["coverage_capacity_status"] == "blocked"


def test_offline_projection_is_written_to_account_coverage_blocker(session: Session) -> None:
    task, _group = _seed(session)
    account = session.get(TgAccount, 1)

    assert _online_ready_accounts(session, task, [account], {}) == []

    row = session.get(TaskAccountDailyCoverage, "coverage-1")
    assert row.state == "blocked"
    assert row.blocker_code == "account_offline"
    assert row.next_eligible_at is not None


def test_all_account_shortage_reason_does_not_scan_platform_accounts(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    task.stats = {"account_offline_count": 1}
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._has_account_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("platform scan")),
    )

    message, reason = _account_shortage_reason(session, task, group, {})

    assert reason == "account_offline"
    assert "在线" in message


def test_planner_does_not_normalize_legacy_coverage_config(session: Session) -> None:
    task, _group = _seed(session)
    task.type_config = {**task.type_config, "account_coverage_mode": "natural"}

    config = _canonicalized_task_config(session, task, dict(task.type_config))

    assert config["account_coverage_mode"] == "natural"


def test_coverage_plan_state_materializes_scope_once_and_reuses_rows(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    group.active_window = "00:00-23:59"
    calls = 0

    def count_ensure(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.ensure_task_daily_coverage",
        count_ensure,
    )

    state = _coverage_plan_state(session, task, group, task.type_config, {})

    assert calls == 1
    assert set(state.rows_by_account) == {1, 2, 3}


def test_account_selection_uses_supplied_coverage_snapshot_without_reread(session: Session, monkeypatch) -> None:
    task, group = _seed(session)
    row = session.get(TaskAccountDailyCoverage, "coverage-1")
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.ready_coverage_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ledger reread")),
    )

    selected = _select_accounts_for_plan(
        session,
        task,
        group,
        {},
        task.type_config,
        coverage_rows=[row],
    )

    assert [account.id for account in selected] == [1]


def test_all_account_coverage_never_enables_quality_fallback() -> None:
    config = {"account_coverage_mode": "all_accounts_daily"}

    assert _quality_fallback_enabled(config, {"deficit": 10}) is False
    assert _quality_fallback_enabled({"account_coverage_mode": "natural"}, {"deficit": 10}) is True


def test_coverage_round_does_not_repeat_one_account_for_multiple_obligations() -> None:
    config = {"account_coverage_mode": "all_accounts_daily", "allow_account_repeat": True}

    assert _coverage_round_config(config)["allow_account_repeat"] is False


def test_send_message_payload_carries_coverage_ledger_identity() -> None:
    payload = SendMessagePayload(
        group_id=21,
        message_text="自然生成的群聊内容",
        account_coverage_mode="all_accounts_daily",
        coverage_ledger_id="coverage-1",
    )

    assert payload.model_dump()["coverage_ledger_id"] == "coverage-1"
