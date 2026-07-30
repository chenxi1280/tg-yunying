from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ContentMixCycle,
    ContentMixCycleSlot,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.executors import group_ai_chat
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def test_replan_materializes_reply_slot_without_previous_action(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    created, blueprint = _install_unmaterialized_replan_stubs(
        facts,
        monkeypatch,
    )

    result = group_ai_chat._replan_content_mix_slots(
        session,
        facts.task,
        blueprint,
    )

    assert result == group_ai_chat.ContentMixReplanResult(True, 1)
    assert created[0].account_id == facts.account.id
    assert created[0].payload.content_mix_cycle_id == facts.cycle.id
    assert created[0].payload.content_mix_cycle_slot_id == facts.cycle_slot.id
    assert created[0].payload.primary_quantity_slot_id == facts.quantity.id
    assert created[0].payload.relation_kind == "reply"
    assert created[0].payload.reply_to_message_id == 777
    assert created[0].payload.slot_attempt == 1


def _install_unmaterialized_replan_stubs(
    facts: _UnmaterializedFacts,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[group_ai_chat.SlotSnapshot], SimpleNamespace]:
    fresh = group_ai_chat.SlotSnapshot(
        account_id=facts.account.id,
        planned_at=facts.planned_at,
        payload=SendMessagePayload(
            group_id=facts.group.id,
            coverage_ledger_id=facts.coverage.id,
            ai_generation_status="pending",
            reply_to_message_id=777,
        ),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_build_slot_snapshot",
        lambda *_args, **_kwargs: fresh,
    )
    created: list[group_ai_chat.SlotSnapshot] = []
    monkeypatch.setattr(
        group_ai_chat,
        "_create_reserved_action",
        lambda _session, _task, snapshot: (
            created.append(snapshot) or SimpleNamespace(id="replacement-action")
        ),
    )
    blueprint = SimpleNamespace(
        facts=SimpleNamespace(
            coverage=SimpleNamespace(daily_group_target_id=""),
        ),
        profile=SimpleNamespace(
            selected=[facts.account],
            coverage_rows={facts.account.id: facts.coverage},
        ),
        generation=SimpleNamespace(
            times=[facts.planned_at],
            quality_items=[
                {
                    "slot_account_id": facts.account.id,
                    "reply_target": {"message_id": 777},
                },
            ],
        ),
    )
    return created, blueprint


def test_quantity_alignment_does_not_borrow_other_coverage_slot() -> None:
    blueprint = SimpleNamespace(
        profile=SimpleNamespace(
            coverage_rows={201: SimpleNamespace(id="coverage-own")},
        ),
        generation=SimpleNamespace(
            quality_items=[{"slot_account_id": 201}],
        ),
    )
    other_slot = SimpleNamespace(
        id="quantity-other",
        task_account_daily_coverage_id="coverage-other",
    )

    selected = group_ai_chat._align_quantity_slots(
        blueprint,
        [other_slot],
    )

    assert selected == []


def test_extra_volume_alignment_uses_uncovered_quantity_slot() -> None:
    coverage_slot = SimpleNamespace(
        id="quantity-coverage",
        task_account_daily_coverage_id="coverage-other",
    )
    extra_slot = SimpleNamespace(
        id="quantity-extra",
        task_account_daily_coverage_id=None,
    )
    blueprint = SimpleNamespace(
        profile=SimpleNamespace(coverage_rows={}),
        generation=SimpleNamespace(
            quality_items=[{"slot_account_id": 201}],
        ),
    )

    selected = group_ai_chat._align_quantity_slots(
        blueprint,
        [coverage_slot, extra_slot],
    )

    assert selected == [extra_slot]


def test_dispatch_releases_mismatched_quantity_coverage_binding(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    mismatch = _mismatched_action_facts(session, facts)

    accepted = dispatcher._ensure_ai_content_mix_binding(
        session,
        mismatch.action,
    )
    dispatcher._sync_action_coverage_state(session, mismatch.action)
    dispatcher._sync_action_content_mix_state(session, mismatch.action)

    assert accepted is False
    assert mismatch.action.status == "skipped"
    assert (
        mismatch.action.result["error_code"]
        == "content_mix_quantity_coverage_mismatch"
    )
    assert mismatch.coverage.state == "ready"
    assert mismatch.coverage.reserved_action_id is None
    assert facts.cycle_slot.slot_state == "replan_required"
    assert facts.quantity.state == "open"


def test_replan_prefers_quantity_coverage_account_over_previous_action() -> None:
    own_account = SimpleNamespace(id=201)
    wrong_account = SimpleNamespace(id=202)
    blueprint = SimpleNamespace(
        generation=SimpleNamespace(
            quality_items=[
                {"slot_account_id": 201},
                {"slot_account_id": 202},
            ],
        ),
    )

    resolved = group_ai_chat._replan_slot_account_and_item(
        blueprint,
        {201: own_account, 202: wrong_account},
        SimpleNamespace(relation_kind="direct"),
        previous=SimpleNamespace(account_id=202),
        coverage=SimpleNamespace(account_id=201),
    )

    assert resolved == (own_account, 0)


class _UnmaterializedFacts(SimpleNamespace):
    planned_at: object
    account: TgAccount
    group: TgGroup
    task: Task
    coverage: TaskAccountDailyCoverage
    quantity: TaskGroupDailyMessageSlot
    cycle: ContentMixCycle
    cycle_slot: ContentMixCycleSlot


class _ScopeFacts(SimpleNamespace):
    account: TgAccount
    group: TgGroup
    target: OperationTarget
    task: Task


class _DailyFacts(SimpleNamespace):
    ledger: TaskDayLedger
    coverage: TaskAccountDailyCoverage
    quantity: TaskGroupDailyMessageSlot


class _MismatchFacts(SimpleNamespace):
    action: Action
    coverage: TaskAccountDailyCoverage


def _unmaterialized_reply_facts(session: Session) -> _UnmaterializedFacts:
    planned_at = _now()
    scope = _content_mix_scope()
    daily = _daily_quantity_facts(planned_at, scope)
    cycle, cycle_slot = _unmaterialized_cycle_facts(
        planned_at,
        scope,
        daily,
    )
    session.add_all(
        [
            scope.account,
            scope.group,
            scope.target,
            scope.task,
            daily.ledger,
            daily.coverage,
            daily.quantity,
            cycle,
            cycle_slot,
        ],
    )
    session.flush()
    return _UnmaterializedFacts(
        planned_at=planned_at,
        account=scope.account,
        group=scope.group,
        task=scope.task,
        coverage=daily.coverage,
        quantity=daily.quantity,
        cycle=cycle,
        cycle_slot=cycle_slot,
    )


def _content_mix_scope() -> _ScopeFacts:
    account = TgAccount(
        id=201,
        tenant_id=1,
        display_name="未物化槽账号",
        phone_masked="201",
    )
    group = TgGroup(
        id=301,
        tenant_id=1,
        tg_peer_id="-100301",
        title="未物化槽群",
    )
    target = OperationTarget(
        id=401,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-100301",
        title="未物化槽目标",
    )
    task = Task(
        id="unmaterialized-cycle-task",
        tenant_id=1,
        name="未物化槽重建",
        type="group_ai_chat",
        status="running",
    )
    return _ScopeFacts(
        account=account,
        group=group,
        target=target,
        task=task,
    )


def _daily_quantity_facts(
    planned_at,
    scope: _ScopeFacts,
) -> _DailyFacts:
    ledger = TaskDayLedger(
        id="unmaterialized-ledger",
        tenant_id=1,
        task_id=scope.task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=planned_at.date(),
        period_start_at=planned_at,
        deadline_at=planned_at,
        day_phase="full_day",
        planning_anchor_at=planned_at,
    )
    coverage = TaskAccountDailyCoverage(
        id="unmaterialized-coverage",
        tenant_id=1,
        task_id=scope.task.id,
        task_day_ledger_id=ledger.id,
        group_id=scope.group.id,
        account_id=scope.account.id,
        coverage_date=planned_at.date(),
        state="ready",
    )
    quantity = TaskGroupDailyMessageSlot(
        id="unmaterialized-quantity",
        tenant_id=1,
        task_id=scope.task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=scope.target.id,
        task_account_daily_coverage_id=coverage.id,
        slot_kind="account_coverage",
        slot_ordinal=1,
        state="open",
    )
    return _DailyFacts(
        ledger=ledger,
        coverage=coverage,
        quantity=quantity,
    )


def _unmaterialized_cycle_facts(
    planned_at,
    scope: _ScopeFacts,
    daily: _DailyFacts,
) -> tuple[ContentMixCycle, ContentMixCycleSlot]:
    cycle = ContentMixCycle(
        id="unmaterialized-cycle",
        tenant_id=1,
        task_id=scope.task.id,
        target_operation_target_id=scope.target.id,
        task_day_ledger_id=daily.ledger.id,
        cycle_seq=1,
        config_revision=1,
        scope_total_slots=1,
        allocation_seed="unmaterialized-seed",
        allocation_closed_at=planned_at,
    )
    cycle_slot = ContentMixCycleSlot(
        id="unmaterialized-cycle-slot",
        tenant_id=1,
        cycle_id=cycle.id,
        slot_index=1,
        primary_quantity_slot_id=daily.quantity.id,
        relation_kind="reply",
        initial_reply_to_message_id="777",
        slot_state="unmaterialized",
    )
    return cycle, cycle_slot


def _mismatched_action_facts(
    session: Session,
    facts: _UnmaterializedFacts,
) -> _MismatchFacts:
    account = TgAccount(
        id=202,
        tenant_id=1,
        display_name="错绑 coverage 账号",
        phone_masked="202",
    )
    coverage = TaskAccountDailyCoverage(
        id="mismatched-coverage",
        tenant_id=1,
        task_id=facts.task.id,
        task_day_ledger_id=facts.quantity.task_day_ledger_id,
        group_id=facts.group.id,
        account_id=account.id,
        coverage_date=facts.planned_at.date(),
        state="reserved",
        reservation_token="mismatched-reservation",
    )
    action = Action(
        id="mismatched-content-mix-action",
        tenant_id=1,
        task_id=facts.task.id,
        task_type=facts.task.type,
        action_type="send_message",
        account_id=account.id,
        status="executing",
        primary_quantity_slot_id=facts.quantity.id,
        content_mix_cycle_slot_id=facts.cycle_slot.id,
        content_mix_slot_attempt=1,
        payload={
            "coverage_ledger_id": coverage.id,
            "primary_quantity_slot_id": facts.quantity.id,
            "content_mix_cycle_slot_id": facts.cycle_slot.id,
        },
    )
    coverage.reserved_action_id = action.id
    facts.cycle_slot.current_action_id = action.id
    facts.cycle_slot.slot_state = "pending"
    session.add_all([account, coverage, action])
    session.flush()
    return _MismatchFacts(action=action, coverage=coverage)
