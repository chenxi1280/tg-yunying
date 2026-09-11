from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.timezone import BEIJING_TZ
from app.models import (Action, ChannelMessage, OperationTarget, ReactionFulfillmentObligation,
                        Task, TaskDayLedger, Tenant, TgAccount, ViewFulfillmentObligation)
from app.services.task_center.source_owner_cursor import attach_owner_history
from app.services.task_center.source_pacing import SourcePacingSlot, schedule_source_pacing_points, wall_datetime
from tests.owner_postgres_support import owner_engine
from tests.test_source_frozen_recovery import NOW, GAP, DEADLINE

pytestmark = pytest.mark.isolated_postgres


def _seed(engine):
    with Session(engine) as db:
        db.add(Tenant(id=1, name='source'))
        db.flush()
        db.add(Task(id='task', tenant_id=1, name='source', type='channel_like', status='running'))
        db.add(OperationTarget(id=1, tenant_id=1, tg_peer_id='-1001', title='source'))
        for account_id in (1, 2, 3):
            db.add(TgAccount(id=account_id, tenant_id=1, display_name='source', phone_masked='test'))
        db.flush()
        db.add(ChannelMessage(id=1, channel_target_id=1, message_id=1))
        db.flush()
        for i, at in enumerate((NOW-GAP, NOW-GAP, DEADLINE), start=1):
            db.add(ReactionFulfillmentObligation(id=str(i), tenant_id=1, task_id='task',
                channel_message_id=1, account_id=i, reaction_contract_version=1,
                task_lifecycle_epoch=1, pacing_period_key='message:1', pacing_source_key_hash='source',
                pacing_due_at=(NOW-GAP).replace(tzinfo=BEIJING_TZ), release_not_before_at=at.replace(tzinfo=BEIJING_TZ), pacing_plan_total=12, pacing_slot_ordinal=i))
        db.commit()


def _schedule(db, identity, *, owner_model=ReactionFulfillmentObligation, task_id='task'):
    task = db.get(Task, task_id)
    owner = db.get(owner_model, identity)
    slot = SourcePacingSlot(source_key='message', slot_key=identity, slot_ordinal=owner.pacing_slot_ordinal,
        plan_total=12, period_start_at=NOW-timedelta(hours=1), deadline_at=DEADLINE,
        frozen_due_at=owner.pacing_due_at, release_not_before_at=owner.release_not_before_at,
        owner_id=identity, pacing_period_key='message:1', pacing_source_key_hash='source')
    slots = attach_owner_history(db, task, [slot], owner_model=owner_model,
                                 config={}, seed_id='source')
    return slots, schedule_source_pacing_points(slots, {}, now_at=NOW, seed_id='source')


def test_source_lock_serializes_recovery_and_next_batch_observes_reserved_point(owner_engine):
    _seed(owner_engine)
    with Session(owner_engine) as first, Session(owner_engine) as second:
        slots, points = _schedule(first, '1')
        first_point = points['1'].release_not_before_at
        first.get(ReactionFulfillmentObligation, '1').release_not_before_at = first_point.replace(tzinfo=BEIJING_TZ)
        second.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(OperationalError, match='lock timeout'):
            _schedule(second, '2')
        second.rollback()
        first.commit()
        slots, points = _schedule(second, '2')
        assert points['2'].release_not_before_at - first_point >= GAP
        assert points['2'].release_not_before_at < DEADLINE


def test_bound_owner_is_not_given_recovery_permission(owner_engine):
    _seed(owner_engine)
    with Session(owner_engine) as db:
        db.add(Action(id='bound', tenant_id=1, task_id='task', task_type='channel_like',
                      action_type='like_message', status='unknown_after_send'))
        db.flush()
        db.get(ReactionFulfillmentObligation, '1').current_action_id = 'bound'
        db.commit()
        slots, points = _schedule(db, '1')
        assert slots[0].recovery_source_history is None
        assert points == {}
        assert db.get(Action, 'bound').status == 'unknown_after_send'


def test_view_history_is_scoped_through_its_original_day_ledger(owner_engine):
    _seed(owner_engine)
    with Session(owner_engine) as db:
        db.add(Task(id='view', tenant_id=1, name='view', type='channel_view', status='running'))
        db.flush()
        start = (NOW-timedelta(hours=1)).replace(tzinfo=BEIJING_TZ)
        db.add(TaskDayLedger(id='day', tenant_id=1, task_id='view', timezone_snapshot='Asia/Shanghai',
            timezone_revision=1, obligation_local_date=NOW.date(), period_start_at=start,
            deadline_at=DEADLINE.replace(tzinfo=BEIJING_TZ), day_phase='active', planning_anchor_at=start))
        db.flush()
        for i, at in enumerate((NOW-GAP, DEADLINE), start=1):
            db.add(ViewFulfillmentObligation(id=f'v{i}', tenant_id=1, task_day_ledger_id='day',
                channel_message_id=1, account_id=i, task_lifecycle_epoch=1,
                pacing_period_key='message:1', pacing_source_key_hash='source',
                pacing_due_at=(NOW-GAP).replace(tzinfo=BEIJING_TZ),
                release_not_before_at=at.replace(tzinfo=BEIJING_TZ), pacing_plan_total=12,
                pacing_slot_ordinal=i))
        db.commit()
        slots, points = _schedule(db, 'v1', owner_model=ViewFulfillmentObligation, task_id='view')
        assert slots[0].recovery_source_history is not None
        assert NOW < points['v1'].release_not_before_at < NOW+GAP
