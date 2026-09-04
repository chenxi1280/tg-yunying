from collections import Counter
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.models import AlbumReactionParticipation, ReactionFulfillmentObligation
from app.services.task_center.channel_fulfillment import confirm_reaction_obligation
from app.services.task_center.executors import channel_like_album as albums
from app.services.task_center.executors.channel_like_planning import like_actions_for_messages
from app.services.task_center.executors.channel_like_types import LikePlanningSpec
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def _plan(monkeypatch, *, accounts=50, cap=1000):
    session, task, ledger, members = seed_source_session(accounts=accounts)
    task.type_config = {**task.type_config, "daily_reaction_cap": cap}
    rows = [message(session, i, album="album", metadata={"photo": True}) for i in range(1, 10)]
    monkeypatch.setattr(albums, "ensure_task_day_ledger", lambda *a, **kw: ledger)
    monkeypatch.setattr(albums, "reserve_portfolio_units", lambda *a, **kw: NS(achievable=True))
    monkeypatch.setattr(albums, "available_album_children", lambda *a, **kw: len(kw["children"]))
    spec = LikePlanningSpec(config=task.type_config, messages=rows, accounts=members, reactions=["👍"],
        target_per_message=accounts, account_ids_by_message={m.id: set() for m in rows},
        allocated_ids_by_message={1: [m.id for m in members]}, now=NOW)
    return session, task, spec


def test_nine_photo_album_never_expands_to_450_operations_and_retries_keep_children(monkeypatch):
    session, task, spec = _plan(monkeypatch)
    with session:
        first = like_actions_for_messages(session, task, spec)
        counts = Counter(item.account_id for item in first)
        assert len(counts) == 50
        assert set(counts.values()) == {1, 2}
        assert 50 < len(first) < 100
        session.commit()
        second = like_actions_for_messages(session, task, spec)
        assert {(p.account_id, p.message.id, p.reaction) for p in second} == {(p.account_id, p.message.id, p.reaction) for p in first}


def test_cap_only_fits_one_child_per_selected_account(monkeypatch):
    session, task, spec = _plan(monkeypatch, accounts=5, cap=5)
    with session:
        plans = like_actions_for_messages(session, task, spec)
        assert len(plans) == 5
        assert all(p.child_count == 1 for p in session.scalars(select(AlbumReactionParticipation)))


def test_album_account_requires_all_children_facts(monkeypatch):
    session, task, spec = _plan(monkeypatch, accounts=5)
    with session:
        like_actions_for_messages(session, task, spec)
        parent = session.scalar(select(AlbumReactionParticipation).where(AlbumReactionParticipation.child_count == 2))
        assert parent is not None
        for index, child in enumerate(parent.children):
            obligation = session.get(ReactionFulfillmentObligation, child["obligation_id"])
            confirm_reaction_obligation(session, obligation, target_peer_id=parent.target_peer_id,
                reaction_emoji=child["reaction"], confirmed_at=NOW)
            assert parent.status == ("partial_child_confirmed" if index == 0 else "confirmed")


def test_confirmed_child_does_not_renumber_other_frozen_child_slots(monkeypatch):
    session, task, spec = _plan(monkeypatch, accounts=5)
    with session:
        first = like_actions_for_messages(session, task, spec)
        original = {(item.account_id, item.message.id): (item.slot_ordinal, item.plan_total) for item in first}
        parent = session.scalar(select(AlbumReactionParticipation))
        child = parent.children[0]
        obligation = session.get(ReactionFulfillmentObligation, child["obligation_id"])
        confirm_reaction_obligation(session, obligation, target_peer_id=parent.target_peer_id,
            reaction_emoji=child["reaction"], confirmed_at=NOW)
        second = like_actions_for_messages(session, task, spec)
        assert len(second) == len(first) - 1
        assert all(original[(item.account_id, item.message.id)] == (item.slot_ordinal, item.plan_total) for item in second)


def test_invalid_album_capability_never_reserves_extra_portfolio_units(monkeypatch):
    session, task, spec = _plan(monkeypatch, accounts=5)
    monkeypatch.setattr(albums, "message_reaction_plan", lambda *a, **kw: [])
    monkeypatch.setattr(albums, "reserve_portfolio_units", lambda *a, **kw: pytest.fail("leaked extra reservation"))
    with session:
        assert like_actions_for_messages(session, task, spec) == []
        assert list(session.scalars(select(AlbumReactionParticipation))) == []
        assert list(session.scalars(select(ReactionFulfillmentObligation))) == []


def test_album_detail_merges_children_and_counts_only_fully_confirmed_accounts(monkeypatch):
    from app.schemas.task_center import TaskMessageGroupOut
    from app.services.task_center import album_reaction_progress as progress
    session, task, spec = _plan(monkeypatch, accounts=5)
    monkeypatch.setattr(progress, "configured_album_accounts", lambda *a: {"album": 5})
    with session:
        like_actions_for_messages(session, task, spec)
        parent = session.scalar(select(AlbumReactionParticipation).where(AlbumReactionParticipation.child_count == 2))
        child = parent.children[0]
        confirm_reaction_obligation(session, session.get(ReactionFulfillmentObligation, child["obligation_id"]),
            target_peer_id=parent.target_peer_id, reaction_emoji=child["reaction"], confirmed_at=NOW)
        groups = [{"channel_target_id": 1, "message_id": m.message_id, "actions": [], "stats": {}}
                  for m in spec.messages]
        result = progress.merge_album_message_groups(session, task, groups)
        assert len(result) == 1
        output = TaskMessageGroupOut.model_validate(result[0])
        assert output.album_id == "album"
        assert output.target_count == 5
        assert output.completed_count == 0
        assert output.confirmed_child_reactions == 1
        assert output.target_count_proven is True


def test_album_keeps_frozen_obligation_across_non_identity_config_revision(monkeypatch):
    from app.services.task_center.channel_fulfillment import frozen_reaction_obligation
    session, task, spec = _plan(monkeypatch, accounts=5)
    with session:
        first = like_actions_for_messages(session, task, spec)
        frozen = {(item.account_id, item.message.id): item.obligation_id for item in first}
        assert all(frozen.values())
        task.config_revision += 1
        second = like_actions_for_messages(session, task, spec)
        for item in second:
            obligation = frozen_reaction_obligation(session, task, item=item)
            assert obligation.id == frozen[(item.account_id, item.message.id)]
            assert obligation.reaction_contract_version == 1
        assert len(list(session.scalars(select(ReactionFulfillmentObligation)))) == len(first)


def test_completed_album_clears_old_per_photo_shortfall_but_partial_does_not(monkeypatch):
    from app.services.task_center import album_reaction_facts as facts
    from app.services.task_center.executors.channel_like import _empty_like_plan_message
    session, task, spec = _plan(monkeypatch, accounts=5)
    monkeypatch.setattr(facts, "configured_album_accounts", lambda *a: {"album": 5})
    with session:
        like_actions_for_messages(session, task, spec)
        task.last_error = "没有可新增的有效点赞账号"
        kwargs = dict(messages=spec.messages, target_per_message=5,
                      account_ids_by_message=spec.account_ids_by_message)
        assert _empty_like_plan_message(session, task, **kwargs)
        for parent in session.scalars(select(AlbumReactionParticipation)):
            for child in parent.children:
                confirm_reaction_obligation(session, session.get(ReactionFulfillmentObligation, child["obligation_id"]),
                    target_peer_id=parent.target_peer_id, reaction_emoji=child["reaction"], confirmed_at=NOW)
        assert _empty_like_plan_message(session, task, **kwargs) == ""
