"""Runtime membership contention is local; explicit start remains all-or-nothing."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, OperationTarget, Task, TgAccount
from app.services.task_center.channel_membership import gate_channel_membership
from app.services.task_center.channel_membership_start import prepare_channel_membership_on_start
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from tests.test_engagement_membership_foundation import _initialize, _seed, _task
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
TARGET_ID = 101


def _seed_task(database):
    with Session(database) as session:
        _seed(session)
        _initialize(session)
        session.add(OperationTarget(id=TARGET_ID, tenant_id=1, target_type="channel",
            tg_peer_id="-100101", title="membership QA"))
        task = _task(session, task_type="channel_view")
        task.type_config = {**task.type_config, "target_channel_id": TARGET_ID}
        task.account_config = {"selection_mode": "group", "account_group_ids": [1]}
        session.commit()
        return task.id


def test_runtime_membership_skips_busy_account_and_recovers_it(database):
    task_id = _seed_task(database)
    with Session(database) as writer, Session(database) as planner:
        writer.scalar(select(TgAccount).where(TgAccount.id == 11).with_for_update())
        task, target = planner.get(Task, task_id), planner.get(OperationTarget, TARGET_ID)
        gate = gate_channel_membership(planner, task, target)
        planner.commit()
        assert gate.created == 1
        assert list(planner.scalars(select(Action.account_id))) == [12]
        summary = task.stats["account_assignment_eligibility"]
        assert summary["candidate_count"] == 2 and summary["pending_count"] == 1
        writer.rollback()
        gate_channel_membership(planner, task, target)
        planner.commit()
        assert set(planner.scalars(select(Action.account_id))) == {11, 12}
        assert task.stats["account_assignment_eligibility"]["pending_count"] == 0


def test_all_busy_membership_waits_without_declaring_no_eligible_accounts(database):
    task_id = _seed_task(database)
    with Session(database) as writer, Session(database) as planner:
        list(writer.scalars(select(TgAccount).where(TgAccount.id.in_([11, 12])).with_for_update()))
        task = planner.get(Task, task_id)
        gate = gate_channel_membership(planner, task, planner.get(OperationTarget, TARGET_ID))
        planner.commit()
        assert gate.waiting and not gate.blocked and not gate.ready
        assert gate.blocker_reason == "account_eligibility_busy"
        assert task.stats["membership_stage"] == "eligibility_pending"
        assert task.stats["account_assignment_eligibility"]["pending_count"] == 2
        assert planner.scalar(select(func.count(Action.id))) == 0


def test_explicit_start_does_not_partially_materialize_while_account_is_busy(database):
    task_id = _seed_task(database)
    with Session(database) as writer, Session(database) as starter:
        writer.scalar(select(TgAccount).where(TgAccount.id == 11).with_for_update())
        task = starter.get(Task, task_id)
        with pytest.raises(RuntimeResourceBlocked, match="account_eligibility_busy"):
            prepare_channel_membership_on_start(starter, task)
        starter.rollback()
        assert starter.scalar(select(func.count(Action.id))) == 0
        writer.rollback()
        prepare_channel_membership_on_start(starter, task)
        starter.commit()
        assert set(starter.scalars(select(Action.account_id))) == {11, 12}
