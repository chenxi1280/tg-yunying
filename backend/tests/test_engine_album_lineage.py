from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from sqlalchemy import select

from app.models import Action, AlbumReactionParticipation, TaskParticipationUnitPlan
from app.services.task_center.channel_fulfillment import ensure_reaction_obligation
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_account_origin import _reaction_plan
from app.services.task_center.engagement_reaction_capacity import ensure_reaction_capacity_epoch, _round_robin_allocations
from tests.test_engagement_reaction_capacity import _session, _seed, _messages


pytestmark = pytest.mark.no_postgres


def test_album_child_resolves_the_exact_logical_source_participation_plan():
    with _session() as session:
        task, channel = _seed(session)
        rows = _messages(session, channel, count=2)
        for row in rows:
            row.grouped_id = "album"
        now = datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        ledger = ensure_task_day_ledger(session, task, now=now)
        epoch = ensure_reaction_capacity_epoch(session, task, ledger, messages=[rows[0]], target=channel)
        account_id = epoch.source_allocations[0]["allocated_account_ids"][0]
        child = ensure_reaction_obligation(session, task, rows[1], account_id)
        action = Action(task_id=task.id, task_lifecycle_epoch=task.task_lifecycle_epoch,
            tenant_id=task.tenant_id, scheduled_at=now, account_id=account_id)
        plan = _reaction_plan(session, action, {"reaction_fulfillment_obligation_id": child.id})
        assert isinstance(plan, TaskParticipationUnitPlan)
        assert plan.participation_unit.endswith(":source:channel:101:album:album")
        assert account_id in plan.selected_account_ids


def test_representative_photo_change_does_not_reselect_frozen_accounts():
    previous = NS(source_allocations=[{"channel_message_id": 1,
        "source_identity": "channel:101:album:album", "allocated_account_ids": [11, 12]}])
    result = _round_robin_allocations({2: [13, 14]}, cap=2, previous=previous,
        source_ids={"channel:101:album:album": 2}, demand_limits={2: 2})
    assert result == {2: [11, 12]}


def test_one_source_never_receives_more_accounts_than_its_target():
    result = _round_robin_allocations({1: [11, 12, 13, 14]}, cap=5, previous=None,
        source_ids={"message:1": 1}, demand_limits={1: 3})
    assert result == {1: [11, 12, 13]}


def test_album_adapter_materializes_exact_frozen_children_with_real_pacing(monkeypatch):
    from app.services.task_center.executors import channel_like
    from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
    now = datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(channel_like, "_now", lambda: now)
    with _session() as session:
        task, channel = _seed(session)
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        channel.reaction_capability_mode = "all"
        channel.available_reactions = ["👍"]
        rows = _messages(session, channel, count=3)
        for row in rows:
            row.grouped_id = "album"
            row.source_metadata = {"observed": True, "photo": True}
            row.created_at = now
        ledger = ensure_task_day_ledger(session, task, now=now)
        created = channel_like._build_like_actions(session, task, channel=channel,
            messages=rows, config=task.type_config, ledger=ledger, now_value=now)
        parents = list(session.scalars(select(AlbumReactionParticipation)))
        assert len(parents) == 3, task.last_error
        assert created == sum(parent.child_count for parent in parents), task.last_error
        assert 3 <= created <= 5
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))
        frozen = {child["obligation_id"] for parent in parents for child in parent.children}
        assert {a.payload["reaction_fulfillment_obligation_id"] for a in actions} == frozen
        assert all(a.effective_claim_at is not None for a in actions)
        repeated = channel_like._build_like_actions(session, task, channel=channel,
            messages=rows, config=task.type_config, ledger=ledger, now_value=now)
        assert repeated == 0
        assert {child["obligation_id"] for parent in parents for child in parent.children} == frozen
