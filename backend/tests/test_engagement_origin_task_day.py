"""Delayed dispatch keeps the frozen participation day instead of searching other days."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Action, TgAccount
from app.services.task_center.channel_fulfillment import ensure_reaction_obligation
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_account_origin import _origin_task_day, _reaction_plan, _task_day_plans, resolve_frozen_account_origin
from app.services.task_center.engagement_reaction_capacity import ensure_reaction_capacity_epoch
from tests.test_engagement_reaction_capacity import _session, _seed, _messages


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 4, 3, tzinfo=timezone.utc)


def seed_original_plan(session):
    task, channel = _seed(session)
    message = _messages(session, channel, count=1)[0]
    ledger = ensure_task_day_ledger(session, task, now=NOW)
    epoch = ensure_reaction_capacity_epoch(session, task, ledger, messages=[message], target=channel)
    account_id = epoch.source_allocations[0]["allocated_account_ids"][0]
    obligation = ensure_reaction_obligation(session, task, message, account_id)
    obligation.pacing_due_at = NOW
    obligation.release_not_before_at = NOW + timedelta(days=1)
    action = Action(task_id=task.id, tenant_id=task.tenant_id, account_id=account_id,
        task_lifecycle_epoch=task.task_lifecycle_epoch, scheduled_at=NOW + timedelta(days=1),
        payload={"reaction_fulfillment_obligation_id": obligation.id})
    return task, channel, message, ledger, obligation, action


def test_delayed_reaction_uses_original_due_day_plan():
    with _session() as session:
        task, channel, message, ledger, _, action = seed_original_plan(session)
        tomorrow = ensure_task_day_ledger(session, task, now=NOW + timedelta(days=1))
        ensure_reaction_capacity_epoch(session, task, tomorrow, messages=[message], target=channel)
        plan = _reaction_plan(session, action, action.payload)
        assert plan.task_day_ledger_id == ledger.id
        assert {item.task_day_ledger_id for item in _task_day_plans(session, action)} == {ledger.id}


def test_other_day_plan_does_not_replace_missing_original_source():
    with _session() as session:
        task, channel, message, _, _, action = seed_original_plan(session)
        original = _reaction_plan(session, action, action.payload)
        original.participation_unit += ":unrelated"
        tomorrow = ensure_task_day_ledger(session, task, now=NOW + timedelta(days=1))
        ensure_reaction_capacity_epoch(session, task, tomorrow, messages=[message], target=channel)
        session.flush()
        assert _reaction_plan(session, action, action.payload) is None
        with pytest.raises(ValueError, match="origin_plan_missing"):
            resolve_frozen_account_origin(session, action, session.get(TgAccount, action.account_id))


def test_reaction_cannot_borrow_foreign_task_obligation():
    with _session() as session:
        _, _, _, _, _, action = seed_original_plan(session)
        action.task_id = "another-task"
        with pytest.raises(ValueError, match="obligation_owner_mismatch"):
            _reaction_plan(session, action, action.payload)


def test_explicit_ledger_must_belong_to_action():
    with _session() as session:
        _, _, _, ledger, _, action = seed_original_plan(session)
        action.payload = {"task_day_ledger_id": ledger.id}
        action.tenant_id += 1
        with pytest.raises(ValueError, match="ledger_owner_mismatch"):
            _origin_task_day(session, action)
