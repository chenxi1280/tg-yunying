from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    Action,
    AccountStatus,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionGroupProbeEvent,
    ChannelDiscussionThreadBinding,
    ChannelDiscussionThreadProbeEvent,
    ChannelMessage,
    ChannelMessageSourceRevision,
    DiscussionMembershipFact,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.channel_comment_discussion_admission import (
    ensure_discussion_membership_actions,
)
from app.services.task_center.channel_comment_discussion_contracts import (
    AUTHORITATIVE_GROUP_STAGE,
    AUTHORITATIVE_THREAD_STAGE,
    GroupProbeObservation,
    MembershipObservation,
    ThreadProbeObservation,
    record_group_probe,
    record_membership_fact,
    record_thread_probe,
)


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 916_201
CHANNEL_ID = 916_202
DISCUSSION_ID = 916_203
ACCOUNT_ID = 916_204
ADMISSION_ACCOUNT_ID = 916_205
MESSAGE_ID = 916_206
TASK_ID = "pg-channel-comment-discussion"
SOURCE_ID = "pg-channel-comment-source"
NOW = datetime(2030, 8, 1, 12, 0)


def test_postgres_discussion_fact_writers_serialize_current_revision() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_scope()
        group_binding_id = _concurrent_group_probes()
        thread_binding_id = _concurrent_thread_probes(group_binding_id)
        membership_revisions = _concurrent_membership_facts(group_binding_id)

        with SessionLocal() as session:
            assert _count(session, ChannelDiscussionGroupProbeEvent) == 2
            assert _count(session, ChannelDiscussionGroupBinding) == 1
            assert _count(session, ChannelDiscussionThreadProbeEvent) == 2
            assert _count(session, ChannelDiscussionThreadBinding) == 1
            assert thread_binding_id == session.scalar(
                select(ChannelDiscussionThreadBinding.id)
            )
            assert membership_revisions == [1, 2]
            assert session.scalar(select(func.count(DiscussionMembershipFact.id)).where(
                DiscussionMembershipFact.is_current.is_(True),
            )) == 1
    finally:
        _cleanup()


def test_postgres_dual_admission_workers_reuse_one_join_action() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_scope()
        binding_id = _concurrent_group_probes()
        _seed_admission(binding_id)
        start = Barrier(2)

        def ensure() -> str:
            with SessionLocal() as session:
                start.wait(timeout=5)
                task = session.get(Task, TASK_ID)
                binding = session.get(ChannelDiscussionGroupBinding, binding_id)
                account = session.get(TgAccount, ADMISSION_ACCOUNT_ID)
                actions = ensure_discussion_membership_actions(
                    session, task, binding, accounts=[account], now_value=NOW,
                )
                session.commit()
                return actions[ADMISSION_ACCOUNT_ID].id

        with ThreadPoolExecutor(max_workers=2) as pool:
            action_ids = list(pool.map(lambda _index: ensure(), range(2)))

        with SessionLocal() as session:
            assert len(set(action_ids)) == 1
            assert session.scalar(select(func.count(Action.id)).where(
                Action.action_type == "ensure_discussion_membership",
            )) == 1
    finally:
        _cleanup()


def _concurrent_group_probes() -> str:
    start = Barrier(2)

    def probe(index: int) -> str:
        with SessionLocal() as session:
            start.wait(timeout=5)
            binding = record_group_probe(session, _group_observation(index))
            session.commit()
            return binding.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        binding_ids = list(pool.map(probe, range(2)))
    assert len(set(binding_ids)) == 1
    return binding_ids[0]


def _concurrent_thread_probes(group_binding_id: str) -> str:
    start = Barrier(2)

    def probe(index: int) -> str:
        with SessionLocal() as session:
            start.wait(timeout=5)
            binding = record_thread_probe(
                session, _thread_observation(group_binding_id, index),
            )
            session.commit()
            return binding.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        binding_ids = list(pool.map(probe, range(2)))
    assert len(set(binding_ids)) == 1
    return binding_ids[0]


def _concurrent_membership_facts(group_binding_id: str) -> list[int]:
    start = Barrier(2)

    def record(_index: int) -> int:
        with SessionLocal() as session:
            start.wait(timeout=5)
            fact = record_membership_fact(
                session, _membership_observation(group_binding_id, ACCOUNT_ID, "joined"),
            )
            session.commit()
            return fact.fact_revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(record, range(2)))


def _seed_scope() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="discussion postgres"))
        session.flush()
        session.add_all([
            OperationTarget(
                id=CHANNEL_ID, tenant_id=TENANT_ID, target_type="channel",
                tg_peer_id="-100916202", title="channel",
            ),
            OperationTarget(
                id=DISCUSSION_ID, tenant_id=TENANT_ID, target_type="group",
                tg_peer_id="-100916203", title="discussion",
            ),
            _account(ACCOUNT_ID),
            _account(ADMISSION_ACCOUNT_ID),
        ])
        session.flush()
        session.add(ChannelMessage(
            id=MESSAGE_ID, tenant_id=TENANT_ID, channel_target_id=CHANNEL_ID,
            message_id=9001, published_at=NOW,
        ))
        session.flush()
        session.add(ChannelMessageSourceRevision(
            id=SOURCE_ID,
            tenant_id=TENANT_ID,
            channel_target_id=CHANNEL_ID,
            channel_message_id=MESSAGE_ID,
            source_revision=1,
            source_remote_message_id=9001,
            source_published_at=NOW,
            source_observed_at=NOW,
            source_text_snapshot="source",
            source_content_hash="a" * 64,
            observation_identity_hash="b" * 64,
        ))
        session.commit()


def _seed_admission(group_binding_id: str) -> None:
    with SessionLocal() as session:
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="discussion admission",
            type="channel_comment",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [ADMISSION_ACCOUNT_ID]},
            type_config={
                "auto_join_discussion_enabled": True,
                "discussion_join_account_ids": [ADMISSION_ACCOUNT_ID],
                "discussion_join_budget": 1,
                "discussion_join_pacing_policy_version": "pacing-v1",
                "discussion_join_pacing_policy": {"interval_seconds": 60},
            },
        ))
        record_membership_fact(
            session,
            _membership_observation(
                group_binding_id, ADMISSION_ACCOUNT_ID, "not_participant",
            ),
        )
        session.commit()


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=TENANT_ID,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=f"session-{account_id}",
    )


def _group_observation(index: int) -> GroupProbeObservation:
    return GroupProbeObservation(
        tenant_id=TENANT_ID,
        channel_target_id=CHANNEL_ID,
        target_reference_revision=1,
        channel_peer_id="-100916202",
        discussion_target_id=DISCUSSION_ID,
        discussion_peer_id="-100916203",
        probe_request_id=f"group-probe-{index}",
        probe_status="success",
        probe_stage=AUTHORITATIVE_GROUP_STAGE,
        observed_at=NOW,
        fresh_until_at=NOW + timedelta(hours=1),
    )


def _thread_observation(group_binding_id: str, index: int) -> ThreadProbeObservation:
    return ThreadProbeObservation(
        tenant_id=TENANT_ID,
        source_revision_id=SOURCE_ID,
        group_binding_id=group_binding_id,
        probe_request_id=f"thread-probe-{index}",
        probe_status="success",
        probe_stage=AUTHORITATIVE_THREAD_STAGE,
        observed_at=NOW,
        discussion_peer_id="-100916203",
        thread_root_message_id=8001,
    )


def _membership_observation(
    group_binding_id: str,
    account_id: int,
    status: str,
) -> MembershipObservation:
    return MembershipObservation(
        tenant_id=TENANT_ID,
        account_id=account_id,
        group_binding_id=group_binding_id,
        discussion_peer_id="-100916203",
        membership_status=status,
        can_send=status == "joined",
        observed_at=NOW,
        fresh_until_at=NOW + timedelta(hours=1),
    )


def _count(session, model) -> int:
    return int(session.scalar(select(func.count(model.id))) or 0)


def _cleanup() -> None:
    with SessionLocal() as session:
        for model in (
            Action,
            DiscussionMembershipFact,
            ChannelDiscussionThreadBinding,
            ChannelDiscussionThreadProbeEvent,
            ChannelDiscussionGroupBinding,
            ChannelDiscussionGroupProbeEvent,
            ChannelMessageSourceRevision,
            ChannelMessage,
            Task,
            TgAccount,
            OperationTarget,
        ):
            session.execute(delete(model).where(model.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
