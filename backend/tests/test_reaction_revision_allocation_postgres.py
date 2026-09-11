"""Source edits cannot move frozen selection into a different source revision."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelMessageSourceRevision, TaskParticipationUnitPlan, Tenant, AccountPool
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_reaction_capacity import ensure_reaction_capacity_epoch
from tests.owner_postgres_support import owner_engine
from tests.test_engagement_reaction_capacity import _messages, _account
from app.models import OperationTarget
from app.schemas import ChannelLikeTaskCreate
from app.services.task_center.service import create_channel_like_task
from app.services.task_center.channel_membership import mark_channel_membership_joined
from tests.account_group_revision_test_support import bootstrap_groups

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def test_edited_source_does_not_inherit_old_larger_selection(owner_engine):
    with Session(owner_engine) as session:
        task, target = seed(session)
        messages = _messages(session, target, count=1)
        ledger = ensure_task_day_ledger(session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc))
        original = ensure_reaction_capacity_epoch(session, task, ledger, messages=messages, target=target)
        old_allocation = list(original.source_allocations)
        old = session.get(ChannelMessageSourceRevision, messages[0].current_source_revision_id)
        edited = ChannelMessageSourceRevision(**{
            column.key: getattr(old, column.key) for column in old.__table__.columns
            if column.key not in {"id", "source_revision", "observation_identity_hash", "source_content_hash"}
        }, source_revision=2, observation_identity_hash="b"*64, source_content_hash="c"*64)
        session.add(edited)
        session.flush()
        messages[0].current_source_revision_id = edited.id
        task.type_config = {**task.type_config, "target_likes_per_message": 1}
        session.flush()

        current = ensure_reaction_capacity_epoch(session, task, ledger, messages=messages, target=target)

        assert current.id != original.id
        assert current.source_allocations[0]["required_count"] == 1
        assert len(current.source_allocations[0]["allocated_account_ids"]) <= 1
        assert original.source_allocations == old_allocation
        assert len(old_allocation[0]["allocated_account_ids"]) == 3
        plans = session.scalars(select(TaskParticipationUnitPlan).where(
            TaskParticipationUnitPlan.task_id == task.id,
            TaskParticipationUnitPlan.state == "active")).all()
        assert sorted(plan.required_count for plan in plans) == [1, 3]
        session.rollback()


def seed(session):
    session.add(Tenant(id=1, name="test"))
    session.flush()
    session.add(AccountPool(id=1, tenant_id=1, name="pool"))
    session.flush()
    session.add_all(_account(i) for i in range(11, 15))
    target = OperationTarget(id=101, tenant_id=1, target_type="channel", tg_peer_id="-100101", title="channel")
    session.add(target)
    session.flush()
    bootstrap_groups(session, 1, (1,))
    session.commit()
    task = create_channel_like_task(session, 1, ChannelLikeTaskCreate(name="test", target_channel_id=101,
        engagement_contract_version="unified_engagement_v1", account_group_ids=[1],
        concurrency_limit_per_group=4, target_likes_per_message=3, like_count_jitter=0, daily_reaction_cap=5), "tester")
    for account_id in range(11,15):
        mark_channel_membership_joined(session, 1, target.id, account_id)
    return task, target
