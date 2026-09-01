from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    AuditLog,
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentContentRevisionOperation,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    ChannelCommentQualityTargetRevision,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    OperationTarget,
    Task,
    TaskCommentCapacityReservation,
    TaskRuntimeSummary,
    Tenant,
)
from app.services.task_center.channel_comment_capacity import reserve_comment_capacity
from app.services.task_center.channel_comment_capacity_allocation import (
    rebalance_comment_capacity_epoch,
)
from app.services.task_center.channel_comment_content_revision import (
    reconcile_channel_comment_source_edit,
)
from app.services.task_center.channel_comment_quality_target import (
    freeze_initial_quality_target,
    quality_assignment_content,
)
from app.services.task_center.channel_comment_source_delete import (
    settle_channel_comment_source_deleted,
)
from app.services.task_center.service import pause_task, resume_task, stop_task
from app.timezone import BEIJING_TZ


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 915_188
TASK_ID = "pg-comment-rolling-cap"
MESSAGE_ID = 915_188
PLAN_ID = "pg-comment-rolling-plan"
SOURCE_ID = "pg-comment-rolling-source"
EDIT_SOURCE_ID = "pg-comment-edited-source"
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


def test_postgres_plan_lock_cas_reuses_same_content_revision_operation() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    _seed_source_edit_scope()
    start = Barrier(2)

    def reconcile() -> str:
        with SessionLocal() as session:
            message = session.get(ChannelMessage, MESSAGE_ID)
            source = session.get(ChannelMessageSourceRevision, EDIT_SOURCE_ID)
            start.wait(timeout=5)
            operation = reconcile_channel_comment_source_edit(
                session, message, source, at=SCHEDULED_AT,
            )[0]
            session.commit()
            return operation.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            operation_ids = list(pool.map(lambda _index: reconcile(), range(2)))
        with SessionLocal() as session:
            operation_count = session.scalar(select(func.count(
                ChannelCommentContentRevisionOperation.id,
            )).where(ChannelCommentContentRevisionOperation.task_id == TASK_ID))
            assignments = list(session.scalars(select(
                ChannelCommentGroundingAssignment,
            ).where(ChannelCommentGroundingAssignment.plan_contract_id == PLAN_ID)))
            quality_targets = list(session.scalars(select(
                ChannelCommentQualityTargetRevision,
            ).where(
                ChannelCommentQualityTargetRevision.plan_contract_id == PLAN_ID,
            ).order_by(ChannelCommentQualityTargetRevision.quality_target_revision)))
            plan = session.get(ChannelCommentPlanContract, PLAN_ID)
        assert operation_ids[0] == operation_ids[1]
        assert operation_count == 1
        assert len(assignments) == 4
        assert sum(row.assignment_state == "active" for row in assignments) == 2
        assert all(
            row.source_revision_id == EDIT_SOURCE_ID
            for row in assignments if row.assignment_state == "active"
        )
        assert len(quality_targets) == 2
        assert plan.current_quality_target_revision_id == quality_targets[-1].id
    finally:
        _cleanup()


def test_postgres_plan_lock_cas_reuses_same_source_delete_event() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    obligation_ids = _seed_scope()
    start = Barrier(2)

    def settle() -> str:
        with SessionLocal() as session:
            message = session.get(ChannelMessage, MESSAGE_ID)
            start.wait(timeout=5)
            event = settle_channel_comment_source_deleted(
                session, message,
                occurred_at=SCHEDULED_AT, evidence_hash="d" * 64,
            )[0]
            session.commit()
            return event.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            event_ids = list(pool.map(lambda _index: settle(), range(2)))
        with SessionLocal() as session:
            event_count = session.scalar(select(func.count(
                ChannelCommentPlanLifecycleEvent.id,
            )).where(ChannelCommentPlanLifecycleEvent.task_id == TASK_ID))
            obligations = list(session.scalars(select(
                CommentFulfillmentObligation,
            ).where(CommentFulfillmentObligation.id.in_(obligation_ids))))
        assert event_ids[0] == event_ids[1]
        assert event_count == 1
        assert all(row.status == "terminated" for row in obligations)
    finally:
        _cleanup()


def test_postgres_task_lock_cas_reuses_same_pause_event() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    obligation_ids = _seed_scope()
    start = Barrier(2)

    def pause() -> tuple[int, str]:
        with SessionLocal() as session:
            start.wait(timeout=5)
            task = pause_task(session, TENANT_ID, TASK_ID, "operator")
            event_id = session.scalar(select(ChannelCommentPlanLifecycleEvent.id).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "pause",
            ))
            return int(task.task_lifecycle_epoch), event_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: pause(), range(2)))
        with SessionLocal() as session:
            event_count = session.scalar(select(func.count(
                ChannelCommentPlanLifecycleEvent.id,
            )).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "pause",
            ))
            obligations = list(session.scalars(select(
                CommentFulfillmentObligation,
            ).where(CommentFulfillmentObligation.id.in_(obligation_ids))))
        assert outcomes[0] == outcomes[1]
        assert event_count == 1
        assert all(row.status == "paused_unallocated" for row in obligations)
    finally:
        _cleanup()


def test_postgres_task_lock_cas_reuses_same_resume_event() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    _seed_scope()
    with SessionLocal() as session:
        pause_task(session, TENANT_ID, TASK_ID, "operator")
    start = Barrier(2)

    def resume() -> tuple[int, str]:
        with SessionLocal() as session:
            start.wait(timeout=5)
            task = resume_task(session, TENANT_ID, TASK_ID, "operator")
            event_id = session.scalar(select(ChannelCommentPlanLifecycleEvent.id).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "resume",
            ))
            return int(task.task_lifecycle_epoch), event_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: resume(), range(2)))
        with SessionLocal() as session:
            event_count = session.scalar(select(func.count(
                ChannelCommentPlanLifecycleEvent.id,
            )).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "resume",
            ))
            task = session.get(Task, TASK_ID)
        assert outcomes[0] == outcomes[1]
        assert event_count == 1
        assert task.status == "running"
    finally:
        _cleanup()


def test_postgres_task_lock_cas_reuses_same_stop_event() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    obligation_ids = _seed_scope()
    start = Barrier(2)

    def stop() -> tuple[int, str]:
        with SessionLocal() as session:
            start.wait(timeout=5)
            task = stop_task(session, TENANT_ID, TASK_ID, "operator")
            event_id = session.scalar(select(ChannelCommentPlanLifecycleEvent.id).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "stop",
            ))
            return int(task.task_lifecycle_epoch), event_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: stop(), range(2)))
        with SessionLocal() as session:
            event_count = session.scalar(select(func.count(
                ChannelCommentPlanLifecycleEvent.id,
            )).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "stop",
            ))
            obligations = list(session.scalars(select(
                CommentFulfillmentObligation,
            ).where(CommentFulfillmentObligation.id.in_(obligation_ids))))
        assert outcomes[0] == outcomes[1]
        assert event_count == 1
        assert all(row.status == "terminated_by_operator" for row in obligations)
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


def _seed_source_edit_scope() -> None:
    _seed_scope()
    with SessionLocal() as session:
        session.add(ChannelMessageSourceRevision(
            id=EDIT_SOURCE_ID,
            tenant_id=TENANT_ID,
            channel_message_id=MESSAGE_ID,
            source_revision=2,
            source_remote_message_id=MESSAGE_ID,
            source_published_at=SCHEDULED_AT,
            source_observed_at=SCHEDULED_AT,
            source_text_snapshot="PG edited source",
            source_content_hash="e" * 64,
            observation_identity_hash="f" * 64,
            source_operation="edited",
        ))
        message = session.get(ChannelMessage, MESSAGE_ID)
        message.current_source_revision_id = EDIT_SOURCE_ID
        session.commit()


def _seed_plan(session, obligation_ids: list[str]) -> None:
    source = _source_revision()
    session.add(source)
    session.flush()
    plan = _plan_contract()
    session.add(plan)
    session.flush()
    _seed_obligations(session, obligation_ids)
    target = freeze_initial_quality_target(session, plan, source)
    component = target.component_targets_json[0]
    for ordinal, obligation_id in enumerate(obligation_ids, 1):
        assignment = ChannelCommentGroundingAssignment(
            id=f"pg-comment-grounding-{ordinal}",
            tenant_id=TENANT_ID, plan_contract_id=PLAN_ID,
            source_revision_id=source.id, target_ordinal=ordinal,
            assignment_version=1, quality_target_revision_id=target.id,
            quality_component_key=component["quality_component_key"],
            **quality_assignment_content(source, component, ordinal),
            assignment_state="active",
        )
        session.add(assignment)
        session.get(CommentFulfillmentObligation, obligation_id).grounding_assignment_id = assignment.id


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
        grounding_required_count=2,
        planned_fallback_count=0,
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
        session.execute(delete(TaskRuntimeSummary).where(
            TaskRuntimeSummary.task_id == TASK_ID,
        ))
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(ChannelMessage).where(ChannelMessage.id == MESSAGE_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.id == TENANT_ID))
        session.execute(delete(AuditLog).where(AuditLog.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
