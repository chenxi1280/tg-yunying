from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiCoverageVariationIntent,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.content_mix_replan_recovery import (
    recover_stale_pending_content_mix_slots,
)
from app.services.task_center.fulfillment_retry import retry_failed_actions
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


def test_build_plan_continues_when_waiting_replan_creates_nothing(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id="independent-cycle-task",
        tenant_id=1,
        name="独立周期继续规划",
        type="group_ai_chat",
        status="running",
    )
    blueprint = SimpleNamespace()
    independent_blueprint = SimpleNamespace()
    frozen = SimpleNamespace()
    prepared = SimpleNamespace(slots=[])
    prepared_blueprints = []

    def prepare(*_args, include_replan_accounts=True):
        prepared_blueprints.append(include_replan_accounts)
        return blueprint if include_replan_accounts else independent_blueprint

    monkeypatch.setattr(
        group_ai_chat, "_prepare_plan_blueprint", prepare,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_replan_content_mix_slots",
        lambda *_args: group_ai_chat.ContentMixReplanResult(True, 0),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_freeze_content_mix_cycle",
        lambda _session, _task, current: (
            frozen if current is independent_blueprint else None
        ),
    )
    monkeypatch.setattr(
        group_ai_chat, "_prepare_action_slots", lambda *_args: prepared,
    )
    monkeypatch.setattr(
        group_ai_chat, "_prepared_plan_is_blocked", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        group_ai_chat, "_create_reserved_actions", lambda *_args, **_kwargs: 2,
    )
    monkeypatch.setattr(
        group_ai_chat, "_record_plan_completion", lambda *_args, **_kwargs: None,
    )

    assert group_ai_chat.build_plan(session, task) == 2
    assert prepared_blueprints == [True, False]


def test_build_plan_does_not_swallow_unrelated_value_error(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id="unrelated-value-error-task",
        tenant_id=1,
        name="程序错误必须暴露",
        type="group_ai_chat",
        status="running",
    )
    blueprint = SimpleNamespace()
    monkeypatch.setattr(
        group_ai_chat,
        "_prepare_plan_blueprint",
        lambda *_args, **_kwargs: blueprint,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_replan_content_mix_slots",
        lambda *_args: group_ai_chat.ContentMixReplanResult(False, 0),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_freeze_content_mix_cycle",
        lambda *_args: (_ for _ in ()).throw(ValueError("content_mix_target_missing")),
    )

    with pytest.raises(ValueError, match="content_mix_target_missing"):
        group_ai_chat.build_plan(session, task)


def test_build_plan_records_structured_quantity_slot_alignment_failure(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id="quantity-alignment-task",
        tenant_id=1,
        name="数量槽状态变化",
        type="group_ai_chat",
        status="running",
        stats={},
    )
    result = group_ai_chat.QuantitySlotAlignmentResult(
        code="quantity_slot_state_changed",
        ledger_id="ledger-1",
        slots=(),
        requested_count=2,
        missing_coverage_ids=("coverage-1",),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_prepare_plan_blueprint",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_replan_content_mix_slots",
        lambda *_args: group_ai_chat.ContentMixReplanResult(False, 0),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_freeze_content_mix_cycle",
        lambda *_args: (_ for _ in ()).throw(
            group_ai_chat.QuantitySlotAlignmentError(result)
        ),
    )

    assert group_ai_chat.build_plan(session, task) == 0
    assert task.last_error == "数量槽状态已变化，等待重新规划"
    assert task.stats["quantity_slot_alignment"] == {
        "code": "quantity_slot_state_changed",
        "ledger_id": "ledger-1",
        "requested_count": 2,
        "aligned_count": 0,
        "missing_coverage_ids": ["coverage-1"],
        "missing_extra_count": 0,
        "recorded_at": task.stats["quantity_slot_alignment"]["recorded_at"],
    }


def test_replan_coverage_is_loaded_before_normal_keyset(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    target = TaskGroupDailyTarget(
        id="unmaterialized-daily-target",
        tenant_id=1,
        task_id=facts.task.id,
        task_day_ledger_id=facts.quantity.task_day_ledger_id,
        group_id=facts.group.id,
        target_date=facts.planned_at.date(),
        configured_message_target=1,
        frozen_account_count=1,
        effective_message_target=1,
        daily_fulfillment_phase="full_day_committed",
        scope_frozen_at=facts.planned_at,
        full_day_committed_at=facts.planned_at,
    )
    session.add(target)
    session.flush()
    plan_facts = SimpleNamespace(
        coverage=SimpleNamespace(daily_group_target_id=target.id),
    )

    rows = group_ai_chat._replan_coverage_rows_for_plan(
        session,
        facts.task,
        plan_facts,
    )

    assert [row.id for row in rows] == [facts.coverage.id]


def test_fact_first_initial_replan_accounts_precede_normal_keyset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(fulfillment_contract_version="fact_first_v3")
    facts = SimpleNamespace()
    coverage = SimpleNamespace(account_id=201)
    account = SimpleNamespace(id=201)
    monkeypatch.setattr(
        group_ai_chat,
        "_replan_coverage_rows_for_plan",
        lambda *_args: [coverage],
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_bound_coverage_account_ids_for_plan",
        lambda *_args: {201, 202},
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_daily_accounts_for_coverage_rows",
        lambda *_args: ([account], []),
    )

    selected, waiting, seen = group_ai_chat._initial_replan_daily_accounts(
        SimpleNamespace(),
        task,
        facts,
        account_limit=20,
        include_replan_accounts=True,
    )

    assert selected == [account]
    assert waiting == []
    assert seen == {201, 202}


def test_bound_pending_coverage_is_excluded_from_normal_keyset(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    facts.cycle_slot.slot_state = "pending"
    target = TaskGroupDailyTarget(
        id="pending-bound-daily-target",
        tenant_id=1,
        task_id=facts.task.id,
        task_day_ledger_id=facts.quantity.task_day_ledger_id,
        group_id=facts.group.id,
        target_date=facts.planned_at.date(),
        configured_message_target=1,
        frozen_account_count=1,
        effective_message_target=1,
        daily_fulfillment_phase="full_day_committed",
        scope_frozen_at=facts.planned_at,
        full_day_committed_at=facts.planned_at,
    )
    session.add(target)
    session.flush()
    plan_facts = SimpleNamespace(
        coverage=SimpleNamespace(daily_group_target_id=target.id),
    )

    excluded = group_ai_chat._bound_coverage_account_ids_for_plan(
        session,
        facts.task,
        plan_facts,
    )

    assert excluded == {facts.account.id}


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


def test_repeated_account_uses_extra_after_own_coverage_slot() -> None:
    blueprint = SimpleNamespace(
        profile=SimpleNamespace(
            coverage_rows={201: SimpleNamespace(id="coverage-own")},
        ),
        generation=SimpleNamespace(
            quality_items=[
                {"slot_account_id": 201},
                {"slot_account_id": 201},
            ],
        ),
    )
    coverage_slot = SimpleNamespace(
        id="quantity-coverage",
        task_account_daily_coverage_id="coverage-own",
    )
    extra_slot = SimpleNamespace(
        id="quantity-extra",
        task_account_daily_coverage_id=None,
    )

    selected = group_ai_chat._align_quantity_slots(
        blueprint,
        [coverage_slot, extra_slot],
    )

    assert selected == [coverage_slot, extra_slot]


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


def test_confirmed_coverage_slot_is_not_reused_as_open_coverage() -> None:
    blueprint = SimpleNamespace(
        profile=SimpleNamespace(
            coverage_rows={
                201: SimpleNamespace(
                    id="coverage-confirmed",
                    target_count=1,
                    confirmed_count=1,
                ),
            },
        ),
        generation=SimpleNamespace(
            quality_items=[{"slot_account_id": 201}],
        ),
    )
    confirmed_slot = SimpleNamespace(
        id="quantity-confirmed",
        task_account_daily_coverage_id="coverage-confirmed",
    )

    selected = group_ai_chat._align_quantity_slots(
        blueprint,
        [confirmed_slot],
    )

    assert selected == []


def test_extra_accounts_are_bounded_by_real_extra_volume_slots() -> None:
    facts = SimpleNamespace(
        coverage=SimpleNamespace(volume_need_now=20),
    )

    assert group_ai_chat._daily_group_extra_account_limit(
        facts,
        selected_count=4,
        account_limit=20,
        extra_slot_count=0,
    ) == 0
    assert group_ai_chat._daily_group_extra_account_limit(
        facts,
        selected_count=4,
        account_limit=20,
        extra_slot_count=3,
    ) == 3


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


def test_planner_recovers_stale_pending_slot_without_gateway_attempt(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    action = _bound_terminal_action(facts)
    session.add(action)
    facts.coverage.state = "reserved"
    facts.coverage.reserved_action_id = action.id
    facts.cycle_slot.slot_state = "pending"
    facts.cycle_slot.current_action_id = action.id
    session.flush()

    recovered = recover_stale_pending_content_mix_slots(
        session,
        facts.task,
    )

    assert recovered == 1
    assert facts.cycle_slot.slot_state == "replan_required"
    assert facts.cycle_slot.current_action_id is None
    assert facts.quantity.state == "open"
    assert facts.coverage.state == "ready"
    assert facts.coverage.reserved_action_id is None


def test_planner_does_not_recover_gateway_started_slot(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    action = _bound_terminal_action(facts)
    facts.cycle_slot.slot_state = "pending"
    facts.cycle_slot.current_action_id = action.id
    session.add_all([
        action,
        ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=facts.account.id,
            attempt_no=1,
            status="failed",
            gateway_call_started_at=facts.planned_at,
        ),
    ])
    session.flush()

    recovered = recover_stale_pending_content_mix_slots(
        session,
        facts.task,
    )

    assert recovered == 0
    assert facts.cycle_slot.slot_state == "pending"
    assert facts.cycle_slot.current_action_id == action.id


def test_fact_first_bound_ai_failure_is_rebuilt_not_retried(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    facts.task.fulfillment_contract_version = "fact_first_v3"
    facts.task.failure_policy = {"max_retries": 3}
    action = _bound_terminal_action(facts)
    session.add(action)
    session.flush()

    retried = retry_failed_actions(
        session,
        facts.task,
        now_value=facts.planned_at,
    )

    assert retried == 0
    assert action.status == "failed"
    assert action.retry_count == 0


def test_fact_first_build_plan_skips_legacy_content_mix_pipeline(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id="fact-first-direct-plan",
        tenant_id=1,
        name="事实优先直连规划",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    session.add(task)
    session.flush()
    blueprint = SimpleNamespace()
    called: dict[str, object] = {}

    monkeypatch.setattr(
        group_ai_chat,
        "_prepare_plan_blueprint",
        lambda *_args, **_kwargs: blueprint,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_replan_content_mix_slots",
        lambda *_args: pytest.fail("fact-first must not run legacy replan"),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_freeze_content_mix_cycle",
        lambda *_args: pytest.fail("fact-first must not freeze legacy cycle"),
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_prepare_action_slots",
        lambda *_args: called.setdefault(
            "frozen_mix", _args[3]
        ) or group_ai_chat.PreparedActionPlan([], {}),
    )
    monkeypatch.setattr(
        group_ai_chat, "_prepared_plan_is_blocked", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        group_ai_chat, "_create_reserved_actions", lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        group_ai_chat, "_record_plan_completion", lambda *_args, **_kwargs: None,
    )

    assert group_ai_chat.build_plan(session, task) == 0
    assert called["frozen_mix"] is None


def test_fact_first_direct_action_does_not_bind_legacy_slot(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    facts.task.fulfillment_contract_version = "fact_first_v3"
    slot = group_ai_chat.SlotSnapshot(
        account_id=facts.account.id,
        planned_at=facts.planned_at,
        payload=SendMessagePayload(
            group_id=facts.group.id,
            coverage_ledger_id=facts.coverage.id,
            content_variation_key="fact-first-direct-variation",
            content_context_version="context-v1",
            ai_generation_status="pending",
        ),
    )

    action = group_ai_chat._create_reserved_action(session, facts.task, slot)

    assert action is not None
    assert action.primary_quantity_slot_id is None
    assert action.content_mix_cycle_slot_id is None
    assert facts.cycle_slot.slot_state == "unmaterialized"
    session.refresh(facts.coverage)
    assert facts.coverage.state == "reserved"
    assert facts.coverage.reserved_action_id == action.id


def test_fact_first_recovery_releases_old_binding_without_touching_slot(
    session: Session,
) -> None:
    facts = _unmaterialized_reply_facts(session)
    facts.task.fulfillment_contract_version = "fact_first_v3"
    action = _bound_terminal_action(facts)
    facts.coverage.state = "reserved"
    facts.coverage.reserved_action_id = action.id
    facts.cycle_slot.slot_state = "pending"
    facts.cycle_slot.current_action_id = action.id
    intent = AiCoverageVariationIntent(
        tenant_id=1,
        coverage_ledger_id=facts.coverage.id,
        action_id=action.id,
        content_variation_key="old-slot-variation",
        context_version="old-context",
    )
    session.add_all([action, intent])
    session.flush()

    recovered = group_ai_chat._recover_stale_fact_first_actions(
        session,
        facts.task,
    )

    assert recovered == 1
    assert facts.cycle_slot.slot_state == "pending"
    assert facts.cycle_slot.current_action_id == action.id
    session.refresh(facts.coverage)
    session.refresh(intent)
    assert facts.coverage.state == "ready"
    assert facts.coverage.reserved_action_id is None
    assert intent.action_id is None


def _bound_terminal_action(facts: _UnmaterializedFacts) -> Action:
    return Action(
        id=f"bound-terminal-{facts.task.id}",
        tenant_id=1,
        task_id=facts.task.id,
        task_type=facts.task.type,
        action_type="send_message",
        account_id=facts.account.id,
        status="failed",
        primary_quantity_slot_id=facts.quantity.id,
        content_mix_cycle_slot_id=facts.cycle_slot.id,
        content_mix_slot_attempt=1,
        payload={
            "coverage_ledger_id": facts.coverage.id,
            "primary_quantity_slot_id": facts.quantity.id,
            "content_mix_cycle_slot_id": facts.cycle_slot.id,
        },
        result={"error_code": "content_mix_binding_invalid"},
    )


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
