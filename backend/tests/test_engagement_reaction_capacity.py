from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    Action,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CrossAdapterSourceJourneyPlanRevision,
    OperationTarget,
    ReactionCapacityAllocationEpoch,
    Tenant,
    TgAccount,
)
from app.schemas import ChannelLikeTaskCreate
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.channel_membership import mark_channel_membership_joined
from app.services.task_center.engagement_reaction_capacity import (
    ensure_reaction_capacity_epoch,
    reaction_admissible_account_ids,
)
from app.services.task_center.channel_payloads import LikeMessagePayload
from app.services.task_center.dispatcher import _reaction_final_gate
from app.services.task_center.executors.channel_like_capability import (
    reaction_capability_revision,
)
from app.services.task_center.executors.channel_like_planning import (
    like_actions_for_messages,
)
from app.services.task_center.executors.channel_like_types import LikePlanningSpec
from app.services.task_center.service import create_channel_like_task


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="点赞组"))
    session.add_all(_account(account_id) for account_id in range(11, 15))
    channel = OperationTarget(
        id=101,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-100101",
        title="测试频道",
    )
    session.add(channel)
    session.commit()
    task = create_channel_like_task(
        session,
        1,
        ChannelLikeTaskCreate(
            name="统一点赞",
            target_channel_id=101,
            engagement_contract_version="unified_engagement_v1",
            account_group_ids=[1],
            concurrency_limit_per_group=4,
            target_likes_per_message=3,
            like_count_jitter=0,
            daily_reaction_cap=5,
        ),
        "tester",
    )
    for account_id in range(11, 15):
        mark_channel_membership_joined(
            session, 1, channel.id, account_id,
        )
    return task, channel


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        display_name=f"账号{account_id}",
        phone_masked=str(account_id),
        status="在线",
        session_ciphertext=f"session-{account_id}",
    )


def _messages(
    session: Session,
    channel: OperationTarget,
    *,
    count: int,
    start_id: int = 1,
) -> list[ChannelMessage]:
    published = datetime(2026, 9, 4, 1)
    rows = [
        ChannelMessage(
            id=message_id,
            tenant_id=1,
            channel_target_id=channel.id,
            message_id=1000 + message_id,
            content_preview=f"消息{message_id}",
            published_at=published + timedelta(minutes=message_id),
        )
        for message_id in range(start_id, start_id + count)
    ]
    session.add_all(rows)
    session.flush()
    for row in rows:
        _source_revision(session, row)
    return rows


def _source_revision(
    session: Session,
    message: ChannelMessage,
) -> ChannelMessageSourceRevision:
    revision = ChannelMessageSourceRevision(
        tenant_id=message.tenant_id,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        source_revision=1,
        source_remote_message_id=message.message_id,
        source_published_at=message.published_at,
        source_observed_at=message.published_at,
        source_text_snapshot=message.content_preview,
        source_content_hash="a" * 64,
        observation_identity_hash=f"{message.id:064x}",
        source_length=len(message.content_preview),
        captured_length=len(message.content_preview),
    )
    session.add(revision)
    session.flush()
    message.current_source_revision_id = revision.id
    session.flush()
    return revision


def test_daily_cap_is_shared_fairly_across_sources_and_idempotent() -> None:
    with _session() as session:
        task, channel = _seed(session)
        messages = _messages(session, channel, count=3)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )

        first = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=messages, target=channel
        )
        second = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=messages, target=channel
        )

        assert first is second
        assert first.allocated_count == 5
        assert first.unallocated_count == 4
        assert [
            len(item["allocated_account_ids"]) for item in first.source_allocations
        ] == [2, 2, 1]
        for allocation in first.source_allocations:
            journey = session.get(
                CrossAdapterSourceJourneyPlanRevision,
                allocation["source_journey_plan_id"],
            )
            assert journey.adapter_constraints[0]["required_count"] == len(
                allocation["allocated_account_ids"]
            )
        assert session.scalars(select(ReactionCapacityAllocationEpoch)).all() == [first]


@pytest.mark.parametrize("offline_ids", [(11, 12, 13, 14), (11, 12)])
def test_health_does_not_shrink_reaction_source_allocation(offline_ids) -> None:
    with _session() as session:
        task, channel = _seed(session)
        messages = _messages(session, channel, count=1)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        for account_id in offline_ids:
            session.get(TgAccount, account_id).session_ciphertext = None
        session.flush()

        epoch = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=messages, target=channel
        )

        assert epoch.allocated_count == 3
        assert epoch.source_allocations[0]["required_count"] == 3
        assert len(epoch.source_allocations[0]["allocated_account_ids"]) == 3
        assert reaction_admissible_account_ids(session, epoch, task=task, ledger=ledger, target=channel).isdisjoint(offline_ids)


def test_frozen_reaction_allocation_observes_recovery_and_new_failure() -> None:
    with _session() as session:
        task, channel = _seed(session)
        messages = _messages(session, channel, count=1)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        for account_id in range(11, 15):
            session.get(TgAccount, account_id).session_ciphertext = None
        session.flush()
        first = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=messages, target=channel
        )
        original = list(first.source_allocations)
        assert reaction_admissible_account_ids(session, first, task=task, ledger=ledger, target=channel) == set()
        for account_id in range(11, 15):
            session.get(TgAccount, account_id).session_ciphertext = f"session-{account_id}"
        session.flush()

        second = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=messages, target=channel
        )
        assert second is first
        assert second.source_allocations == original
        assert reaction_admissible_account_ids(session, second, task=task, ledger=ledger, target=channel) == {11, 12, 13, 14}
        session.get(TgAccount, 11).session_ciphertext = None
        session.flush()
        assert reaction_admissible_account_ids(session, second, task=task, ledger=ledger, target=channel) == {12, 13, 14}
        assert second.source_allocations == original


def test_late_source_cannot_revoke_capacity_already_frozen() -> None:
    with _session() as session:
        task, channel = _seed(session)
        initial = _messages(session, channel, count=2)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        first = ensure_reaction_capacity_epoch(
            session, task, ledger, messages=initial, target=channel
        )
        late = _messages(session, channel, count=1, start_id=3)

        successor = ensure_reaction_capacity_epoch(
            session,
            task,
            ledger,
            messages=[*initial, *late],
            target=channel,
        )

        assert first.state == "superseded"
        assert successor.allocation_revision == 2
        assert successor.allocated_count == 5
        assert successor.source_allocations[:2] == first.source_allocations
        assert successor.source_allocations[2]["allocated_account_ids"] == []
        assert successor.unallocated_reasons == {
            "reaction_daily_cap_unallocated": 4
        }


def test_reaction_cap_resets_with_new_task_day_ledger() -> None:
    with _session() as session:
        task, channel = _seed(session)
        messages = _messages(session, channel, count=3)
        first_ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        first = ensure_reaction_capacity_epoch(
            session, task, first_ledger, messages=messages, target=channel
        )
        second_ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 5, 3, tzinfo=timezone.utc)
        )

        second = ensure_reaction_capacity_epoch(
            session, task, second_ledger, messages=messages, target=channel
        )

        assert first.id != second.id
        assert second.allocation_revision == 1
        assert second.allocated_count == 5


def test_like_execution_shortfall_does_not_rewrite_frozen_plan_total(
    monkeypatch,
) -> None:
    with _session() as session:
        task, channel = _seed(session)
        message = _messages(session, channel, count=1)[0]
        task.fulfillment_contract_version = "legacy-test"
        accounts = list(
            session.scalars(select(TgAccount).order_by(TgAccount.id).limit(2))
        )
        spec = LikePlanningSpec(
            config=task.type_config,
            messages=[message],
            accounts=accounts,
            reactions=["👍"],
            target_per_message=3,
            account_ids_by_message={message.id: set()},
            allocated_ids_by_message={message.id: [11, 12, 13]},
            now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            "app.services.task_center.executors.channel_like_planning.message_reaction_plan",
            lambda *_args, **_kwargs: ["👍", "👍", "👍"],
        )

        actions = like_actions_for_messages(session, task, spec)

        assert len(actions) == 2
        assert {item.plan_total for item in actions} == {3}


def test_reaction_final_gate_rejects_stale_source_and_capability() -> None:
    with _session() as session:
        task, channel = _seed(session)
        message = _messages(session, channel, count=1)[0]
        revision = session.get(
            ChannelMessageSourceRevision,
            message.current_source_revision_id,
        )
        channel.reaction_capability_mode = "some"
        channel.available_reactions = ["👍"]
        action = Action(
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="like_message",
            account_id=11,
            scheduled_at=datetime(2026, 9, 4, 3),
            plan_batch_key="test",
            action_dedupe_key="test-reaction-final-gate",
        )
        payload = LikeMessagePayload(
            channel_id=channel.tg_peer_id,
            channel_target_id=channel.id,
            channel_message_id=message.id,
            source_revision_id=revision.id,
            message_id=message.message_id,
            reaction_emoji="👍",
            reaction_source_content_hash=revision.source_content_hash,
            reaction_capability_revision=reaction_capability_revision(channel),
        )

        assert _reaction_final_gate(session, action, payload) == ""
        channel.available_reactions = ["🔥"]
        assert _reaction_final_gate(session, action, payload) == (
            "reaction_capability_revision_stale"
        )
        payload.reaction_capability_revision = reaction_capability_revision(channel)
        assert _reaction_final_gate(session, action, payload) == (
            "reaction_capability_blocked"
        )
        payload.reaction_source_content_hash = "c" * 64
        assert _reaction_final_gate(session, action, payload) == (
            "reaction_source_revision_stale"
        )
