import pytest
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace

pytestmark = pytest.mark.no_postgres

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ChannelViewDailyIdentityOwner,
    OperationTarget,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.channel_fulfillment import confirm_view_obligation
from app.services.task_center.channel_fulfillment_takeover import _view_fact
from app.services.task_center.channel_view_targets import (
    ensure_channel_view_targets,
)
from app.services.task_center.channel_view_daily_identity import (
    mark_daily_identity_call_issued,
    release_daily_identity,
)
from app.services.task_center.executors.channel_view import build_plan
from app.schemas.task_center import ChannelViewConfig, ChannelViewTaskConfigUpdate
from app.timezone import BEIJING_TZ
from tests.channel_view_coverage_support import (
    add_message,
    add_view_task,
    confirm_actions,
    new_session,
    seed_channel_scenario,
    view_actions,
)


def _set_view_clock(monkeypatch, value: datetime) -> None:
    monkeypatch.setattr("app.services.task_center.executors.channel_view._now", lambda: value)
    monkeypatch.setattr("app.services.task_center.daily_ledgers._now", lambda: value)


def test_channel_view_daily_reuse_across_multiple_days(monkeypatch):
    day1 = datetime(2026, 8, 28, 10, 0, tzinfo=BEIJING_TZ)
    day2 = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)

    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=50, account_count=10)
        channel = scenario.channel

        msg = add_message(
            session,
            channel=channel,
            message_id=1,
            published_at=day1 - timedelta(hours=2),
        )

        task = add_view_task(
            session,
            channel=channel,
            messages=[msg],
            task_id="task-view-daily-reuse",
            daily_target=10,
            total_target=0,  # Unlimited
        )
        task.type_config["message_active_days"] = 7
        task.type_config["account_coverage_mode"] = "all_accounts_daily"
        task.account_config = {"scope": "all", "coverage_mode": "all_accounts_daily"}
        session.commit()

        # --- DAY 1: 2026-08-28 ---
        _set_view_clock(monkeypatch, day1)

        # Day 1: Build plan -> 10 actions
        created1 = build_plan(session, task)
        assert created1 == 10

        # Execute Day 1 actions -> confirm 10 ViewRemoteFacts on Day 1
        actions1 = view_actions(session, task)
        assert len(actions1) == 10
        confirm_actions(scenario, actions=actions1, confirmed_at=day1)

        # Verify Day 1 has 10 distinct ViewRemoteFacts
        facts_day1 = session.scalars(
            select(ViewRemoteFact).where(ViewRemoteFact.obligation_local_date == day1.date())
        ).all()
        assert len(facts_day1) == 10
        assert {f.account_id for f in facts_day1} == set(range(1, 11))
        assert {f.remote_effect_kind for f in facts_day1} == {"daily_view_operation"}
        assert not any(f.counter_increment_proven for f in facts_day1)

        # Same-day retry -> 0 new actions because all 10 accounts viewed today
        created_same_day = build_plan(session, task)
        assert created_same_day == 0

        # --- DAY 2: 2026-08-29 (Cross-day roll-over) ---
        _set_view_clock(monkeypatch, day2)

        # Day 2: Build plan -> All 10 accounts MUST be eligible again!
        created2 = build_plan(session, task)
        assert created2 == 10, f"Expected 10 actions on Day 2, got {created2}"

        # Execute Day 2 actions -> confirm 10 new ViewRemoteFacts on Day 2
        actions2 = [act for act in view_actions(session, task) if act.status != "success"]
        assert len(actions2) == 10
        confirm_actions(scenario, actions=actions2, confirmed_at=day2)

        # Verify total facts across 2 days is 20, 10 on day 1, 10 on day 2
        all_facts = session.scalars(select(ViewRemoteFact)).all()
        assert len(all_facts) == 20
        facts_day2 = session.scalars(
            select(ViewRemoteFact).where(ViewRemoteFact.obligation_local_date == day2.date())
        ).all()
        assert len(facts_day2) == 10
        assert {f.account_id for f in facts_day2} == set(range(1, 11))
        assert {f.remote_effect_kind for f in facts_day2} == {"daily_view_operation"}
        assert not any(f.counter_increment_proven for f in facts_day2)


@pytest.mark.parametrize("schema", [ChannelViewConfig, ChannelViewTaskConfigUpdate])
@pytest.mark.parametrize("unlimited_value", [0, None])
def test_channel_view_schema_accepts_unlimited_total(schema, unlimited_value):
    config = schema(
        target_input="@daily_view_contract",
        per_message_daily_view_target=10,
        per_message_total_view_target=unlimited_value,
    )

    assert config.per_message_total_view_target == 0


def test_view_takeover_uses_frozen_execution_date_instead_of_schedule_date():
    fact = ViewRemoteFact(
        tenant_id=1,
        obligation_id="obligation-day-one",
        obligation_local_date=date(2026, 8, 28),
        target_peer_id="-100500",
        channel_message_id=50,
        account_id=5,
        remote_confirmed_at=datetime(2026, 8, 28, 20, tzinfo=timezone.utc),
    )
    session = SimpleNamespace(new=[fact], scalar=lambda _statement: None)
    payload = SimpleNamespace(
        channel_id="-100500",
        channel_message_id=50,
        execution_date="2026-08-28",
    )
    action = SimpleNamespace(
        account_id=5,
        scheduled_at=datetime(2026, 8, 29, 1, tzinfo=timezone.utc),
    )

    assert _view_fact(session, payload, action) is fact


def test_daily_fact_migration_archives_before_restoring_old_contract(monkeypatch):
    migration = import_module("migrations.versions.0172_channel_view_daily_fact")
    events: list[str] = []
    monkeypatch.setattr(migration, "_assert_no_inflight_daily_owners", lambda: events.append("guard"))
    monkeypatch.setattr(migration, "_archive_daily_owners", lambda: events.append("owners"))
    monkeypatch.setattr(migration, "_archive_daily_fact_duplicates", lambda: events.append("facts"))
    monkeypatch.setattr(migration, "_restore_lifetime_fact_contract", lambda: events.append("old_contract"))

    migration.downgrade()

    assert events == ["guard", "owners", "facts", "old_contract"]
    assert "ROW_NUMBER() OVER" in migration._FACT_ARCHIVE_INSERT_SQL
    assert migration.FACT_ARCHIVE in migration._FACT_ARCHIVE_RESTORE_SQL


def test_daily_fact_migration_blocks_inflight_owner_downgrade(monkeypatch):
    migration = import_module("migrations.versions.0172_channel_view_daily_fact")
    result = SimpleNamespace(scalar_one=lambda: 1)
    bind = SimpleNamespace(execute=lambda _statement: result)
    monkeypatch.setattr(migration, "_table_names", lambda: {migration.OWNER_TABLE})
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="channel_view_daily_owner_downgrade_inflight:1"):
        migration._assert_no_inflight_daily_owners()


def test_daily_fact_migration_rejects_archive_readback_mismatch():
    migration = import_module("migrations.versions.0172_channel_view_daily_fact")

    with pytest.raises(RuntimeError, match="archive_mismatch:2:1"):
        migration._assert_archive_count("archive_mismatch", 2, 1)


def test_cross_task_daily_identity_allows_only_one_action(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=77, account_count=1)
        message = add_message(
            session,
            channel=scenario.channel,
            message_id=77,
            published_at=current - timedelta(hours=1),
        )
        first = add_view_task(
            session,
            channel=scenario.channel,
            messages=[message],
            task_id="daily-owner-task-one",
            daily_target=1,
            total_target=0,
        )
        second = add_view_task(
            session,
            channel=scenario.channel,
            messages=[message],
            task_id="daily-owner-task-two",
            daily_target=1,
            total_target=0,
        )
        third = add_view_task(
            session,
            channel=scenario.channel,
            messages=[message],
            task_id="daily-owner-task-three",
            daily_target=1,
            total_target=0,
        )
        session.commit()

        assert build_plan(session, first) == 1
        assert build_plan(session, second) == 0

        owners = session.scalars(select(ChannelViewDailyIdentityOwner)).all()
        actions = session.scalars(select(Action).where(Action.action_type == "view_message")).all()
        assert len(owners) == 1
        assert len(actions) == 1
        assert owners[0].logical_task_id == first.id
        assert owners[0].action_id == actions[0].id
        assert owners[0].state == "pre_gateway"

        assert release_daily_identity(session, actions[0]) is True
        assert build_plan(session, second) == 1
        second_action = session.scalar(
            select(Action).where(
                Action.task_id == second.id,
                Action.action_type == "view_message",
            )
        )
        assert second_action is not None
        mark_daily_identity_call_issued(session, second_action)
        assert release_daily_identity(session, second_action) is False
        assert release_daily_identity(
            session,
            second_action,
            remote_mutation_state="false",
        ) is True
        assert build_plan(session, third) == 1


def test_channel_view_unlimited_vs_finite_cap():
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=20, account_count=100)
        channel = scenario.channel

        msg = add_message(
            session,
            channel=channel,
            message_id=2,
            published_at=datetime(2026, 8, 25, 10, 0, tzinfo=BEIJING_TZ),
        )

        # 1. Test Unlimited (total = 0)
        task_unlimited = Task(
            id="task-unlimited",
            tenant_id=1,
            type="channel_view",
            name="无上限任务",
            status="running",
            type_config={
                "target_channel_id": channel.id,
                "target_type": "channel",
                "per_message_daily_view_target": 100,
                "per_message_total_view_target": 0,  # Unlimited
            },
        )
        session.add(task_unlimited)

        ledger_unlimited = TaskDayLedger(
            id="ledger-unlimited-1",
            tenant_id=1,
            task_id=task_unlimited.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 28),
            day_phase="full_day",
            period_start_at=datetime(2026, 8, 28, 0, 0, tzinfo=BEIJING_TZ),
            planning_anchor_at=datetime(2026, 8, 28, 0, 0, tzinfo=BEIJING_TZ),
            deadline_at=datetime(2026, 8, 29, 0, 0, tzinfo=BEIJING_TZ),
        )
        session.add(ledger_unlimited)
        session.commit()

        targets_unlimited = ensure_channel_view_targets(
            session,
            task_unlimited,
            channel,
            ledger=ledger_unlimited,
            messages=[msg],
            config=task_unlimited.type_config,
            now=datetime.now(BEIJING_TZ),
        )
        target_unlimited = targets_unlimited[msg.id]
        assert target_unlimited.daily_target_snapshot == 100
        assert target_unlimited.total_target_snapshot == 0
        assert target_unlimited.effective_target_snapshot == 100

        # 2. Test Finite Cap (total = 150, daily = 100)
        task_finite = Task(
            id="task-finite",
            tenant_id=1,
            type="channel_view",
            name="有限上限任务",
            status="running",
            type_config={
                "target_channel_id": channel.id,
                "target_type": "channel",
                "per_message_daily_view_target": 100,
                "per_message_total_view_target": 150,
                "message_active_days": 7,
            },
        )
        session.add(task_finite)

        ledger_finite_1 = TaskDayLedger(
            id="ledger-finite-1",
            tenant_id=1,
            task_id=task_finite.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 28),
            day_phase="full_day",
            period_start_at=datetime(2026, 8, 28, 0, 0, tzinfo=BEIJING_TZ),
            planning_anchor_at=datetime(2026, 8, 28, 0, 0, tzinfo=BEIJING_TZ),
            deadline_at=datetime(2026, 8, 29, 0, 0, tzinfo=BEIJING_TZ),
        )
        session.add(ledger_finite_1)
        session.commit()

        # Day 1: baseline 0 -> effective = min(100, 150 - 0) = 100
        targets_finite_1 = ensure_channel_view_targets(
            session,
            task_finite,
            channel,
            ledger=ledger_finite_1,
            messages=[msg],
            config=task_finite.type_config,
            now=datetime.now(BEIJING_TZ),
        )
        assert targets_finite_1[msg.id].effective_target_snapshot == 100

        # Simulate 100 facts confirmed on Day 1
        for i in range(1, 101):
            ob = ViewFulfillmentObligation(
                id=f"ob-finite-d1-{i}",
                tenant_id=1,
                task_day_ledger_id=ledger_finite_1.id,
                channel_message_id=msg.id,
                account_id=i,
                status="confirmed",
            )
            session.add(ob)
            fact = ViewRemoteFact(
                id=f"fact-finite-d1-{i}",
                tenant_id=1,
                obligation_id=ob.id,
                obligation_local_date=date(2026, 8, 28),
                target_peer_id=channel.tg_peer_id,
                channel_message_id=msg.id,
                account_id=i,
                remote_confirmed_at=datetime(2026, 8, 28, 12, 0, tzinfo=BEIJING_TZ),
            )
            session.add(fact)
        session.commit()

        # Day 2: baseline 100 is below the soft target, so the full daily batch
        # is retained and the cumulative result may reach 200 (> 150).
        ledger_finite_2 = TaskDayLedger(
            id="ledger-finite-2",
            tenant_id=1,
            task_id=task_finite.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 29),
            day_phase="full_day",
            period_start_at=datetime(2026, 8, 29, 0, 0, tzinfo=BEIJING_TZ),
            planning_anchor_at=datetime(2026, 8, 29, 0, 0, tzinfo=BEIJING_TZ),
            deadline_at=datetime(2026, 8, 30, 0, 0, tzinfo=BEIJING_TZ),
        )
        session.add(ledger_finite_2)
        session.commit()

        targets_finite_2 = ensure_channel_view_targets(
            session,
            task_finite,
            channel,
            ledger=ledger_finite_2,
            messages=[msg],
            config=task_finite.type_config,
            now=datetime.now(BEIJING_TZ),
        )
        assert targets_finite_2[msg.id].effective_target_snapshot == 100

        for i in range(1, 101):
            obligation = ViewFulfillmentObligation(
                id=f"ob-finite-d2-{i}",
                tenant_id=1,
                task_day_ledger_id=ledger_finite_2.id,
                channel_message_id=msg.id,
                account_id=i,
                status="confirmed",
            )
            session.add(obligation)
            session.flush()
            session.add(ViewRemoteFact(
                id=f"fact-finite-d2-{i}",
                tenant_id=1,
                obligation_id=obligation.id,
                obligation_local_date=date(2026, 8, 29),
                target_peer_id=channel.tg_peer_id,
                channel_message_id=msg.id,
                account_id=i,
                remote_confirmed_at=datetime(2026, 8, 29, 12, 0, tzinfo=BEIJING_TZ),
            ))
        ledger_finite_3 = TaskDayLedger(
            id="ledger-finite-3",
            tenant_id=1,
            task_id=task_finite.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 30),
            day_phase="full_day",
            period_start_at=datetime(2026, 8, 30, 0, 0, tzinfo=BEIJING_TZ),
            planning_anchor_at=datetime(2026, 8, 30, 0, 0, tzinfo=BEIJING_TZ),
            deadline_at=datetime(2026, 8, 31, 0, 0, tzinfo=BEIJING_TZ),
        )
        session.add(ledger_finite_3)
        session.flush()

        targets_finite_3 = ensure_channel_view_targets(
            session,
            task_finite,
            channel,
            ledger=ledger_finite_3,
            messages=[msg],
            config=task_finite.type_config,
            now=datetime(2026, 8, 30, 10, 0, tzinfo=BEIJING_TZ),
        )
        assert targets_finite_3[msg.id].lifetime_confirmed_at_attach == 200
        assert targets_finite_3[msg.id].effective_target_snapshot == 0
