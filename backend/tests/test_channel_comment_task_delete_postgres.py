from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    RemoteMutationTombstone,
    ChannelCommentPlanLifecycleEvent,
    Task,
    TaskContractActivationManifest,
    TaskContractRoute,
    TaskDeleteOperation,
)
from app.services.task_center.physical_task_deletion import (
    DeleteRequest,
    advance_task_deletion,
    prepare_task_deletion,
)
from app.services.task_center.service import delete_task
from test_channel_comment_capacity_postgres import (
    TASK_ID,
    TENANT_ID,
    _cleanup,
    _seed_scope,
)


RELEASE_TRAIN = "pg-comment-physical-delete"
pytestmark = pytest.mark.allow_missing_rule_binding


def test_postgres_physical_delete_preserves_comment_lifecycle_tombstone() -> None:
    Base.metadata.create_all(engine)
    _cleanup_delete_scope()
    _seed_scope()
    try:
        operation_id = _prepare_operation()
        _advance_all_stages(operation_id)
        with SessionLocal() as session:
            tombstones = list(session.scalars(select(RemoteMutationTombstone).where(
                RemoteMutationTombstone.original_task_id == TASK_ID,
                RemoteMutationTombstone.mutation_kind == "channel_comment_lifecycle",
            )))
            operation = session.get(TaskDeleteOperation, operation_id)
            assert session.get(Task, TASK_ID) is None
            assert operation.state == "committed"
            assert len(tombstones) == 1
            assert tombstones[0].terminal_state == "terminated_by_operator"
            assert tombstones[0].remote_fact_identity_hash
    finally:
        _cleanup_delete_scope()


def test_postgres_soft_delete_writers_create_one_comment_event() -> None:
    Base.metadata.create_all(engine)
    _cleanup_delete_scope()
    _seed_scope()
    start = Barrier(2)

    def delete_once() -> str:
        with SessionLocal() as session:
            start.wait(timeout=5)
            try:
                delete_task(session, TENANT_ID, TASK_ID, "operator")
            except ValueError as exc:
                assert str(exc) == "task not found"
                return "already_deleted"
            return "deleted"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: delete_once(), range(2)))
        with SessionLocal() as session:
            event_count = int(session.scalar(select(func.count(
                ChannelCommentPlanLifecycleEvent.id,
            )).where(
                ChannelCommentPlanLifecycleEvent.task_id == TASK_ID,
                ChannelCommentPlanLifecycleEvent.event_type == "delete",
            )) or 0)
        assert sorted(outcomes) == ["already_deleted", "deleted"]
        assert event_count == 1
    finally:
        _cleanup_delete_scope()


def test_postgres_delete_runtime_requires_tombstone_readback() -> None:
    Base.metadata.create_all(engine)
    _cleanup_delete_scope()
    _seed_scope()
    try:
        operation_id = _prepare_operation()
        for _stage in range(4):
            _advance_one_stage(operation_id)
        with SessionLocal() as session:
            session.execute(delete(RemoteMutationTombstone).where(
                RemoteMutationTombstone.original_task_id == TASK_ID,
            ))
            session.commit()
        with SessionLocal() as session:
            operation = session.get(TaskDeleteOperation, operation_id)
            with pytest.raises(ValueError, match="physical_delete_tombstone_missing"):
                advance_task_deletion(
                    session,
                    operation_id,
                    expected_stage_version=int(operation.stage_version),
                )
            assert session.get(Task, TASK_ID) is not None
    finally:
        _cleanup_delete_scope()


def _prepare_operation() -> str:
    with SessionLocal() as session:
        task = session.get(Task, TASK_ID)
        manifest = _manifest()
        session.add(manifest)
        session.flush()
        session.add(TaskContractRoute(
            manifest_id=manifest.id,
            task_id=TASK_ID,
            role="old",
            expected_lifecycle_epoch=int(task.task_lifecycle_epoch),
        ))
        session.flush()
        operation = prepare_task_deletion(session, DeleteRequest(
            task_id=TASK_ID,
            manifest_id=manifest.id,
            expected_manifest_hash=manifest.old_set_hash,
            actor="pytest",
            approval_ref="pytest",
        ))
        session.commit()
        return operation.id


def _manifest() -> TaskContractActivationManifest:
    return TaskContractActivationManifest(
        tenant_id=TENANT_ID,
        release_train=RELEASE_TRAIN,
        old_task_ids=[TASK_ID],
        new_task_ids=[],
        old_set_hash="1" * 64,
        new_config_set_hash="2" * 64,
        route_epoch=1,
        state="active",
        version=1,
        approval_ref="pytest",
    )


def _advance_all_stages(operation_id: str) -> None:
    for _stage in range(5):
        _advance_one_stage(operation_id)


def _advance_one_stage(operation_id: str) -> None:
    with SessionLocal() as session:
        operation = session.get(TaskDeleteOperation, operation_id)
        advance_task_deletion(
            session,
            operation_id,
            expected_stage_version=int(operation.stage_version),
        )
        session.commit()


def _cleanup_delete_scope() -> None:
    _cleanup()
    with SessionLocal() as session:
        session.execute(delete(RemoteMutationTombstone).where(
            RemoteMutationTombstone.original_task_id == TASK_ID,
        ))
        session.execute(delete(TaskDeleteOperation).where(
            TaskDeleteOperation.original_task_id == TASK_ID,
        ))
        session.execute(delete(TaskContractRoute).where(
            TaskContractRoute.task_id == TASK_ID,
        ))
        session.execute(delete(TaskContractActivationManifest).where(
            TaskContractActivationManifest.release_train == RELEASE_TRAIN,
        ))
        session.commit()
