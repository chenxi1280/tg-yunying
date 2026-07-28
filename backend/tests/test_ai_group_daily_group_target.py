from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupMessageMemory,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    GroupBotAdmission,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.executors import group_ai_chat
from app.services.task_center.daily_group_target import (
    daily_group_due_message_count,
    ensure_task_group_daily_target,
)
from app.services.task_center.daily_ledgers import ensure_task_day_ledger


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


def test_zero_quiet_curve_weight_reduces_volume_without_blocking(session: Session) -> None:
    task, group = _seed(session, configured=24, account_count=1)
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        date(2026, 7, 28),
        now=datetime(2026, 7, 28),
    )
    pacing = {
        "operation_profile": {
            "hourly_activity_curve": [0] * 23 + [60],
        },
    }

    due = daily_group_due_message_count(
        target,
        pacing,
        now=datetime(2026, 7, 28, 12),
    )

    assert due > 0


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


def test_day_ledger_materializes_one_coverage_slot_per_frozen_account(
    session: Session,
) -> None:
    task, _group = _seed(session, configured=5, account_count=3)
    timestamp = datetime(2026, 7, 28, 12)
    task.scheduled_start = timestamp

    ledger = ensure_task_day_ledger(session, task, now=timestamp)
    slots = session.query(TaskGroupDailyMessageSlot).filter_by(
        task_day_ledger_id=ledger.id,
    ).order_by(TaskGroupDailyMessageSlot.slot_ordinal).all()

    assert ledger.obligation_local_date == date(2026, 7, 28)
    assert ledger.day_phase == "partial_start"
    assert len(slots) == 5
    assert [slot.slot_kind for slot in slots] == [
        "account_coverage",
        "account_coverage",
        "account_coverage",
        "extra_volume",
        "extra_volume",
    ]
    assert all(slot.task_account_daily_coverage_id for slot in slots[:3])
    assert all(slot.task_account_daily_coverage_id is None for slot in slots[3:])


def test_day_ledger_snapshots_timezone_and_reuses_same_natural_day(
    session: Session,
) -> None:
    task, _group = _seed(session, configured=3, account_count=3)
    task.timezone = "Asia/Shanghai"

    first = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 28, 8),
    )
    second = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 28, 20),
    )

    assert second.id == first.id
    assert first.timezone_snapshot == "Asia/Shanghai"
    assert first.timezone_revision == task.config_revision
    assert first.deadline_at > first.period_start_at
    assert session.query(TaskDayLedger).count() == 1


def test_planner_freezes_relation_slots_against_daily_quantity_slots(
    session: Session,
) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    ledger = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 28, 12),
    )
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        date(2026, 7, 28),
        now=datetime(2026, 7, 28, 12),
    )
    coverages = {
        row.account_id: row
        for row in session.query(TaskAccountDailyCoverage).filter_by(
            task_day_ledger_id=ledger.id,
        )
    }
    items = [
        {
            "slot": {"slot_id": "logical-1", "account_id": 1},
            "reply_target": {"message_id": 501},
        },
        {"slot": {"slot_id": "logical-2", "account_id": 2}},
        {"slot": {"slot_id": "logical-3", "account_id": 3}},
    ]
    blueprint = SimpleNamespace(
        facts=SimpleNamespace(
            target=session.get(OperationTarget, 31),
                config={**task.type_config, "reply_min_per_round": 1},
                coverage=SimpleNamespace(daily_group_target_id=target.id),
                task_config_revision=task.config_revision,
                rule_version=SimpleNamespace(rule_set_id=7, version=2),
                group=group,
            ),
        turn=SimpleNamespace(cycle_index=1),
        profile=SimpleNamespace(
            cycle_id=f"{task.id}:cycle:1",
            coverage_rows=coverages,
        ),
        generation=SimpleNamespace(quality_items=items),
    )

    frozen = group_ai_chat._freeze_content_mix_cycle(
        session,
        task,
        blueprint,
    )
    slots = session.query(ContentMixCycleSlot).filter_by(
        cycle_id=frozen.cycle.id,
    ).order_by(ContentMixCycleSlot.slot_index).all()

    assert session.query(ContentMixCycle).count() == 1
    assert [slot.relation_kind for slot in slots] == ["reply", "direct", "direct"]
    assert slots[0].initial_reply_to_message_id == "501"
    assert len({slot.primary_quantity_slot_id for slot in slots}) == 3


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


def test_planner_keeps_only_group_bot_ready_accounts_when_gate_is_required(
    session: Session,
) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    task.type_config = {
        **task.type_config,
        "group_bot_admission_required": True,
    }
    session.add_all([
        GroupBotAdmission(
            tenant_id=1,
            group_id=group.id,
            account_id=1,
            state="group_bot_admission_ready",
        ),
        GroupBotAdmission(
            tenant_id=1,
            group_id=group.id,
            account_id=2,
            state="awaiting_group_bot_confirmation",
        ),
        GroupBotAdmission(
            tenant_id=1,
            group_id=group.id,
            account_id=3,
            state="post_follow_visibility_probe",
        ),
    ])
    session.flush()
    accounts = [SimpleNamespace(id=account_id) for account_id in (1, 2, 3)]

    selected = group_ai_chat._group_bot_ready_accounts_for_plan(
        session,
        task,
        group,
        accounts,
    )

    assert [account.id for account in selected] == [1, 3]


def test_group_volume_candidates_scan_past_uncovered_admission_debt(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    accounts = [SimpleNamespace(id=account_id) for account_id in (1, 2, 3)]
    captured: dict[str, object] = {}

    def select_accounts(*_args, **kwargs):
        captured.update(kwargs)
        return accounts

    monkeypatch.setattr(group_ai_chat, "select_task_accounts", select_accounts)
    monkeypatch.setattr(
        group_ai_chat,
        "_online_ready_accounts",
        lambda _session, _task, candidates, _progress: candidates,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_group_bot_ready_accounts_for_plan",
        lambda _session, _task, _group, candidates: candidates,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "voice_profile_prompt_details",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_daily_success_counts",
        lambda *_args: {1: 2, 2: 0, 3: 1},
    )
    facts = SimpleNamespace(
        config=task.type_config,
        group=group,
        coverage=SimpleNamespace(volume_need_now=2),
    )

    selected = group_ai_chat._daily_group_extra_accounts(
        session,
        task,
        facts,
        selected=[],
        account_limit=2,
    )

    assert captured["scan_all_candidates"] is True
    assert [account.id for account in selected] == [2, 3]


@pytest.mark.parametrize(("extra_account_id", "expected_ids"), [(3, [3]), (None, [1])])
def test_daily_planner_prefers_admitted_volume_but_keeps_admission_driver(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    extra_account_id: int | None,
    expected_ids: list[int],
) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    waiting_account = SimpleNamespace(id=1)
    row = SimpleNamespace(id="coverage-1", account_id=1)
    coverage = group_ai_chat.CoveragePlanState(
        rows=[],
        rows_by_account={},
        due_debt=1,
        volume_need_now=1,
    )
    facts = SimpleNamespace(
        config=task.type_config,
        group=group,
        hard_progress={},
        coverage=coverage,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "ready_coverage_plan_batch",
        lambda *_args, exclude_account_ids=None, **_kwargs: SimpleNamespace(
            rows=[] if exclude_account_ids else [row],
        ),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_select_accounts_for_plan",
        lambda *_args, **_kwargs: [waiting_account],
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_online_ready_accounts",
        lambda _session, _task, accounts, _progress: accounts,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_group_bot_ready_accounts_for_plan",
        lambda *_args: [],
    )
    extras = [SimpleNamespace(id=extra_account_id)] if extra_account_id else []
    monkeypatch.setattr(
        group_ai_chat,
        "_daily_group_extra_accounts",
        lambda *_args, **_kwargs: extras,
    )

    state = group_ai_chat._load_daily_coverage_plan_accounts(
        session,
        task,
        facts,
        account_limit=1,
    )

    assert [account.id for account in state.accounts] == expected_ids


def test_open_volume_counts_only_group_bot_plannable_actions(session: Session) -> None:
    task, group = _seed(session, configured=3, account_count=3)
    session.add_all([
        GroupBotAdmission(
            tenant_id=1,
            group_id=group.id,
            account_id=1,
            state="group_bot_admission_ready",
        ),
        GroupBotAdmission(
            tenant_id=1,
            group_id=group.id,
            account_id=2,
            state="group_bot_policy_unresolved",
        ),
        *[
            Action(
                id=f"open-{account_id}",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=account_id,
                status="pending",
                payload={"group_id": group.id},
            )
            for account_id in (1, 2, 3)
        ],
    ])
    session.flush()

    assert group_ai_chat._valid_open_daily_send_count(session, task) == 1
