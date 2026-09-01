from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    OperationTarget,
    Task,
    TaskCommentCapacityReservation,
    Tenant,
)
from app.services.task_center.channel_comment_capacity import reserve_comment_capacity
from app.services.task_center.channel_comment_capacity_allocation import (
    rebalance_comment_capacity_epoch,
)
from app.timezone import BEIJING_TZ


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 915_188
TASK_ID = "pg-comment-rolling-cap"
MESSAGE_ID = 915_188
PLAN_ID = "pg-comment-rolling-plan"
SOURCE_ID = "pg-comment-rolling-source"
SCHEDULED_AT = datetime(2030, 8, 2, 10, 0, tzinfo=BEIJING_TZ)


def test_postgres_task_lock_serializes_last_rolling_capacity_unit() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    obligation_ids = _seed_scope()
    start = Barrier(2)

    def reserve(obligation_id: str) -> bool:
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            obligation = session.get(CommentFulfillmentObligation, obligation_id)
            start.wait(timeout=5)
            row = reserve_comment_capacity(
                session,
                task,
                obligation,
                scheduled_at=SCHEDULED_AT,
                daily_cap=1,
            )
            session.commit()
            return row is not None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, obligation_ids))
        with SessionLocal() as session:
            count = session.scalar(select(func.count(
                TaskCommentCapacityReservation.id,
            )).where(TaskCommentCapacityReservation.task_id == TASK_ID))
        assert sorted(outcomes) == [False, True]
        assert count == 1
    finally:
        _cleanup()


def test_postgres_task_lock_cas_reuses_same_allocation_epoch() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    _seed_scope()
    start = Barrier(2)

    def allocate() -> int:
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            start.wait(timeout=5)
            epoch = rebalance_comment_capacity_epoch(
                session, task, daily_cap=1, at=SCHEDULED_AT,
            )
            session.commit()
            return epoch.allocation_epoch

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            epochs = list(pool.map(lambda _index: allocate(), range(2)))
        with SessionLocal() as session:
            epoch_count = session.scalar(select(func.count(
                ChannelCommentCapacityAllocationEpoch.id,
            )).where(ChannelCommentCapacityAllocationEpoch.task_id == TASK_ID))
            reservation_count = session.scalar(select(func.count(
                TaskCommentCapacityReservation.id,
            )).where(TaskCommentCapacityReservation.task_id == TASK_ID))
        assert epochs == [1, 1]
        assert epoch_count == reservation_count == 1
    finally:
        _cleanup()


def _seed_scope() -> list[str]:
    obligation_ids = ["pg-comment-obligation-1", "pg-comment-obligation-2"]
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="PG rolling comment cap"))
        session.flush()
        session.add(OperationTarget(
            id=TENANT_ID,
            tenant_id=TENANT_ID,
            target_type="channel",
            tg_peer_id=f"-100{TENANT_ID}",
            title="PG rolling cap channel",
        ))
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="PG rolling comment cap",
            type="channel_comment",
            status="running",
            fulfillment_contract_version="fact_first_v3",
            type_config={"daily_comment_cap": 1},
        ))
        session.flush()
        session.add(ChannelMessage(
            id=MESSAGE_ID,
            tenant_id=TENANT_ID,
            channel_target_id=TENANT_ID,
            message_id=MESSAGE_ID,
            content_preview="PG rolling cap source",
            comment_available=True,
            published_at=SCHEDULED_AT,
        ))
        session.flush()
        _seed_plan(session, obligation_ids)
        session.commit()
    return obligation_ids


def _seed_plan(session, obligation_ids: list[str]) -> None:
    session.add(_source_revision())
    session.flush()
    session.add(_plan_contract())
    session.flush()
    _seed_obligations(session, obligation_ids)


def _source_revision() -> ChannelMessageSourceRevision:
    return ChannelMessageSourceRevision(
        id=SOURCE_ID,
        tenant_id=TENANT_ID,
        channel_message_id=MESSAGE_ID,
        source_revision=1,
        source_remote_message_id=MESSAGE_ID,
        source_published_at=SCHEDULED_AT,
        source_observed_at=SCHEDULED_AT,
        source_text_snapshot="PG rolling cap source",
        source_content_hash="a" * 64,
        observation_identity_hash="b" * 64,
        source_operation="observed",
    )


def _plan_contract() -> ChannelCommentPlanContract:
    return ChannelCommentPlanContract(
        id=PLAN_ID,
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        channel_message_id=MESSAGE_ID,
        comment_plan_revision=1,
        source_revision_id=SOURCE_ID,
        source_published_at=SCHEDULED_AT,
        source_observed_at=SCHEDULED_AT,
        window_start_at=SCHEDULED_AT,
        deadline_at=SCHEDULED_AT,
        eligible_account_count=2,
        eligible_account_ids_hash="c" * 64,
        participation_seed="pg-rolling-cap",
        effective_participation_bps=5000,
        required_distinct_account_count=2,
        grounding_required_count=0,
        planned_fallback_count=2,
        daily_comment_cap=1,
    )


def _seed_obligations(session, obligation_ids: list[str]) -> None:
    for ordinal, obligation_id in enumerate(obligation_ids, start=1):
        session.add(CommentFulfillmentObligation(
            id=obligation_id,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            channel_message_id=MESSAGE_ID,
            comment_plan_revision=1,
            target_ordinal=ordinal,
            plan_contract_id=PLAN_ID,
            status="open",
            pacing_due_at=SCHEDULED_AT,
        ))


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(ChannelMessage).where(ChannelMessage.id == MESSAGE_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
