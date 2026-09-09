"""Current admission filters every daily coverage page before body planning."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgGroup
from app.services.task_center.executors import group_ai_chat

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 10, 12)
ADMITTED_ACCOUNT = 25


@pytest.fixture
def supply_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="test"))
        task = Task(id="admitted-supply", tenant_id=1, name="test",
                    type="group_ai_chat", status="running")
        session.add_all([task, TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="test")])
        for account_id in range(1, ADMITTED_ACCOUNT + 1):
            session.add(TgAccount(id=account_id, tenant_id=1,
                display_name=str(account_id), phone_masked=str(account_id)))
            session.add(TaskAccountDailyCoverage(id=f"coverage-{account_id}",
                tenant_id=1, task_id=task.id, group_id=1, account_id=account_id,
                coverage_date=NOW.date(), target_count=1, state="ready",
                targeted_at=NOW))
        session.flush()
        yield session, task


def _scan(session, task, monkeypatch, admitted):
    supplied_pages = []
    facts = SimpleNamespace(coverage=group_ai_chat.CoveragePlanState(
        rows=[], rows_by_account={}, due_debt=ADMITTED_ACCOUNT,
        admissible_account_ids=frozenset(admitted),
    ))

    def ready_accounts(_session, _task, _facts, rows):
        supplied_pages.append([row.account_id for row in rows])
        return [session.get(TgAccount, row.account_id) for row in rows], []

    monkeypatch.setattr(group_ai_chat, "_now", lambda: NOW)
    monkeypatch.setattr(group_ai_chat, "_daily_coverage_scan_page_limit", lambda: 20)
    monkeypatch.setattr(group_ai_chat, "_daily_accounts_for_coverage_rows", ready_accounts)
    selected = []
    group_ai_chat._scan_daily_coverage_accounts(
        session, task, facts, selected=selected, admission_waiting=[],
        seen_account_ids=set(), account_limit=1,
    )
    return selected, supplied_pages


def test_pending_prefix_cannot_enter_body_preparation_or_starve_admitted_page(
    supply_session, monkeypatch,
):
    session, task = supply_session
    selected, pages = _scan(session, task, monkeypatch, {ADMITTED_ACCOUNT})

    assert [account.id for account in selected] == [ADMITTED_ACCOUNT]
    assert pages == [[ADMITTED_ACCOUNT]]
    assert session.get(TaskAccountDailyCoverage, "coverage-1").target_count == 1
    assert session.get(TaskAccountDailyCoverage, "coverage-1").reserved_action_id is None


def test_empty_admission_has_no_body_candidates_and_new_admission_enters_next_plan(
    supply_session, monkeypatch,
):
    session, task = supply_session
    selected, pages = _scan(session, task, monkeypatch, set())
    assert selected == []
    assert pages == []

    selected, pages = _scan(session, task, monkeypatch, {1})
    assert [account.id for account in selected] == [1]
    assert pages == [[1]]


def test_replan_admission_filters_before_limit_without_removing_original_slot(
    supply_session,
):
    from app.models import TaskGroupDailyTarget
    from tests.test_content_mix_unmaterialized_replan import _unmaterialized_reply_facts

    session, _task = supply_session
    original = _unmaterialized_reply_facts(session)
    original.task.fulfillment_contract_version = "fact_first_v3"
    original.coverage.state = "pending_admission"
    target = TaskGroupDailyTarget(id="replan-target", tenant_id=1,
        task_id=original.task.id, task_day_ledger_id=original.quantity.task_day_ledger_id,
        group_id=original.group.id, target_date=original.planned_at.date(),
        configured_message_target=1, frozen_account_count=1, effective_message_target=1,
        daily_fulfillment_phase="full_day_committed", scope_frozen_at=original.planned_at,
        full_day_committed_at=original.planned_at)
    session.add(target)
    session.flush()
    facts = SimpleNamespace(coverage=group_ai_chat.CoveragePlanState(
        rows=[], rows_by_account={}, due_debt=1, daily_group_target_id=target.id,
        admissible_account_ids=frozenset(),
    ))

    assert group_ai_chat._replan_coverage_rows_for_plan(session, original.task, facts) == []
    assert original.cycle_slot.slot_state == "unmaterialized"
    assert original.coverage.state == "pending_admission"

    facts.coverage = group_ai_chat.CoveragePlanState(
        rows=[], rows_by_account={}, due_debt=1, daily_group_target_id=target.id,
        admissible_account_ids=frozenset({original.account.id}),
    )
    rows = group_ai_chat._replan_coverage_rows_for_plan(session, original.task, facts)
    assert rows == [original.coverage]
