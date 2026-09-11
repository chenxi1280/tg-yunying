"""Delayed reaction identities retain the participation that created the obligation."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AccountGroupMembershipSnapshotSet, ReactionFulfillmentObligation, TaskAccountGroupBindingSetRevision, TaskParticipationUnitPlan, TgAccount
from app.services.task_center.engagement_account_origin import resolve_frozen_account_origin
from app.services.task_center.reaction_source_identity import reaction_source_identity
from app.models import ChannelMessage
from tests.postgres_pacing_e4_fixture import factory
from tests.test_channel_source_gap_postgres import _seed, NOW

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _plans(db, action, owner):
    owner.created_at = NOW
    owner.pacing_due_at = NOW + timedelta(days=1)
    binding = TaskAccountGroupBindingSetRevision(id='binding', tenant_id=1, task_id=action.task_id,
        account_group_ids=[2], binding_set_hash='binding', effective_from=NOW-timedelta(seconds=2))
    db.add(binding)
    db.flush()
    snapshot = AccountGroupMembershipSnapshotSet(id='snapshot', tenant_id=1, task_id=action.task_id,
        binding_set_revision_id=binding.id, participation_unit='origin', member_union_hash='snapshot')
    db.add(snapshot)
    db.flush()
    source = reaction_source_identity(db.get(ChannelMessage, owner.channel_message_id))
    plans = []
    for name, day, created, state, accounts in (
        ('original', NOW.date(), NOW-timedelta(seconds=1), 'superseded', [1]),
        ('later', NOW.date(), NOW+timedelta(seconds=1), 'active', [1]),
        ('due_day', (NOW+timedelta(days=1)).date(), NOW+timedelta(days=1), 'active', [2]),
    ):
        plan = TaskParticipationUnitPlan(id=name, tenant_id=1, task_id=action.task_id,
            membership_snapshot_set_id=snapshot.id, participation_kind='source',
            participation_unit=f'task_day:{day}:source:{source}', plan_revision=len(plans)+1,
            policy_revision='test', selection_seed=name, selection_hash=name, created_at=created,
            selected_account_ids=accounts, selected_origin_groups={str(i):2 for i in accounts}, state=state)
        db.add(plan)
        plans.append(plan)
    db.flush()
    return plans


def test_due_day_and_later_revision_cannot_replace_original_account_origin(factory):
    with factory() as db:
        action, _, _, _ = _seed(db)
        owner = db.scalar(select(ReactionFulfillmentObligation))
        original, later, _ = _plans(db, action, owner)
        account = db.get(TgAccount, 1)
        origin = resolve_frozen_account_origin(db, action, account)
        assert origin.participation_plan_id == original.id and origin.account_pool_id == 2
        original.task_lifecycle_epoch += 1
        db.flush()
        with pytest.raises(ValueError, match='origin_plan_missing'):
            resolve_frozen_account_origin(db, action, account)
        original.task_lifecycle_epoch -= 1
        original.selected_account_ids = []
        db.flush()
        with pytest.raises(ValueError, match='origin_plan_missing'):
            resolve_frozen_account_origin(db, action, account)
        assert later.created_at > owner.created_at
