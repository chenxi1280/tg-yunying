from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import Action, Task, TaskPlannerWakeState, Tenant
from app.services._common import _now
from app.timezone import as_beijing_aware
from app.services.task_center import service
from app.services.task_center.search_lane_refill import refill_search_lane, search_refill_eligible
from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres


@pytest.fixture
def setup(owner_engine, monkeypatch):
    factory = sessionmaker(bind=owner_engine)
    monkeypatch.setattr('app.services.task_center.executors.search_click_direct.get_settings',
                        lambda: replace(get_settings(), search_dispatcher_concurrency=2))
    with factory() as db:
        db.add(Tenant(id=1, name='refill'))
        db.flush()
        db.add(Task(id='search', tenant_id=1, name='search', type='search_click', status='running',
                    fulfillment_contract_version='fact_first_v3', next_run_at=as_beijing_aware(_now())-timedelta(seconds=1)))
        db.commit()
    built = []
    def build(db, task):
        built.append(task.id)
        db.add(Action(id=str(uuid4()), tenant_id=1, task_id=task.id, task_type='search_click',
                      action_type='search_join', execution_lane='search', status='pending', scheduled_at=_now()))
        return 1
    monkeypatch.setattr(service, 'build_task_plan', build)
    monkeypatch.setattr(service, '_prepare_due_task_actions', lambda db, task, **kw: (task, 0, False, False, 0))
    monkeypatch.setattr(service, '_check_stop_conditions', lambda *_: False)
    monkeypatch.setattr(service, '_planning_backlog_blocked', lambda *_: False)
    def plan(task_id, eligible):
        return service._plan_due_task(factory, task_id, None, limit=2, eligible=eligible)
    yield factory, built, plan


def add_action(factory, *, status='executing'):
    with factory() as db:
        db.add(Action(id=str(uuid4()), tenant_id=1, task_id='search', task_type='search_click',
                      action_type='search_join', execution_lane='search', status=status, scheduled_at=_now()))
        db.commit()


def test_full_slots_skip_then_completion_refills_without_global_planner_tick(setup):
    factory, built, plan = setup
    add_action(factory)
    add_action(factory)
    refill_search_lane(factory, limit=2, exclude_task_ids=None, plan_due=plan)
    assert built == []
    with factory() as db:
        row = db.scalar(select(Action).limit(1))
        row.status = 'success'
        db.commit()
    refill_search_lane(factory, limit=1, exclude_task_ids=None, plan_due=plan)
    assert built == ['search']
    with factory() as db:
        statuses = list(db.scalars(select(Action.status)))
        assert statuses.count('executing') == 1 and statuses.count('pending') == 1


@pytest.mark.parametrize('condition', ['paused', 'retired', 'deleted', 'future_wake', 'excluded'])
def test_only_due_live_search_tasks_are_planned(setup, condition):
    factory, built, plan = setup
    with factory() as db:
        task = db.get(Task, 'search')
        if condition == 'paused':
            task.status = 'paused'
        elif condition == 'retired':
            db.add(Task(id='replacement', tenant_id=1, name='replacement', type='search_click', status='paused'))
            db.flush()
            task.status = 'stopped'
            task.next_run_at = None
            task.replaced_by_task_id = 'replacement'
            task.retired_at = _now()
        elif condition == 'deleted':
            task.deleted_at = _now()
        elif condition == 'future_wake':
            db.add(TaskPlannerWakeState(tenant_id=1, task_id=task.id, not_before_at=as_beijing_aware(_now())+timedelta(hours=1)))
        db.commit()
    excluded = {'search'} if condition == 'excluded' else None
    refill_search_lane(factory, limit=2, exclude_task_ids=excluded, plan_due=plan)
    assert built == []


def test_wake_changed_after_selection_is_rechecked_under_planner_lock(setup):
    factory, built, plan = setup
    def concurrent_planner(task_id, eligible):
        with factory() as db:
            db.add(TaskPlannerWakeState(tenant_id=1, task_id=task_id, not_before_at=as_beijing_aware(_now())+timedelta(hours=1)))
            db.commit()
        return plan(task_id, eligible)
    refill_search_lane(factory, limit=2, exclude_task_ids=None, plan_due=concurrent_planner)
    assert built == []


def test_another_planner_holding_task_lock_does_not_duplicate_work(setup):
    factory, built, plan = setup
    with factory() as held:
        held.scalar(select(Task).where(Task.id == 'search').with_for_update())
        plan('search', search_refill_eligible)
        assert built == []
    plan('search', search_refill_eligible)
    assert built == ['search']


def test_unknown_action_is_preserved_while_new_work_uses_free_slot(setup):
    factory, built, plan = setup
    add_action(factory, status='unknown_after_send')
    refill_search_lane(factory, limit=1, exclude_task_ids=None, plan_due=plan)
    assert built == ['search']
    with factory() as db:
        assert list(db.scalars(select(Action.status))).count('unknown_after_send') == 1


def test_real_search_batch_entry_refills_before_claim(setup, monkeypatch):
    factory, built, _ = setup
    claimed = []
    def claim(db, **kwargs):
        rows = list(db.scalars(select(Action).where(Action.status == 'pending')))
        claimed.extend(row.id for row in rows)
        return rows
    monkeypatch.setattr(service, 'claim_actions', claim)
    monkeypatch.setattr(service, '_dispatch_claimed_action_batch', lambda factory, batch: len(batch))
    assert service.drain_search_dispatcher(factory, limit=2) == 1
    assert built == ['search'] and len(claimed) == 1
