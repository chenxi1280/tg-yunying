from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    MessageTask,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgGroup,
    TgGroupAccount,
)
from app.services.task_center.target_lifecycle import (
    ERROR_TARGET_GROUP_DISSOLVED,
    ERROR_TARGET_REF_INVALID,
    LIFECYCLE_GROUP_DISSOLVED,
    LIFECYCLE_TARGET_REF_INVALID,
    MSG_GROUP_DISSOLVED,
    MSG_TARGET_REF_INVALID,
    mark_target_group_dissolved,
    mark_target_ref_invalid,
    auto_mark_target_ref_invalid,
    reactivate_target,
    terminal_target_block,
)
from app.services.task_center.hard_hourly_ledger import ensure_bucket
from app.schemas.operations import OperationTargetReactivateRequest, OperationTargetUpdate
from app.services.operations import update_operation_target
from app.services.messages import retry_task as retry_message_task
import app.services.operations as operations_service

pytestmark = pytest.mark.no_postgres


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _channel_action(action_id: str, action_type: str, status: str, payload: dict, result: dict | None = None) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="channel-task",
        task_type="channel_view",
        action_type=action_type,
        status=status,
        scheduled_at=datetime(2026, 7, 24, 10),
        payload=payload,
        result=result,
    )


def test_peer_invalid_style_mark_is_target_ref_invalid_not_group_dissolved():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(
            id=1,
            tenant_id=1,
            target_type="group",
            tg_peer_id="qdsfxy",
            title="青岛师范学院",
            username="qdsfxy",
        )
        session.add(target)
        session.commit()

        result = mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="No user has qdsfxy as username",
            evidence_ref="action-1",
            expected_version=1,
        )
        session.commit()

        assert result.target.lifecycle_status == LIFECYCLE_TARGET_REF_INVALID
        assert result.target.lifecycle_status != LIFECYCLE_GROUP_DISSOLVED
        block = terminal_target_block(result.target)
        assert block is not None
        assert block.code == ERROR_TARGET_REF_INVALID
        assert block.message == MSG_TARGET_REF_INVALID
        assert MSG_GROUP_DISSOLVED not in block.message


def test_mark_dissolved_skips_only_unstarted_exact_target_actions():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(id=10, tenant_id=1, target_type="group", tg_peer_id="g1", title="G1")
        other = OperationTarget(id=11, tenant_id=1, target_type="group", tg_peer_id="g2", title="G2")
        session.add_all([target, other])
        session.add(Task(id="task-1", tenant_id=1, name="t1", type="group_ai_chat", status="running", type_config={"target_operation_target_id": 10}))
        session.add_all(
            [
                Action(
                    id="a-pending",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 10, 0, 0),
                    payload={"target_operation_target_id": 10, "target_reference_revision": 1},
                ),
                Action(
                    id="a-unknown",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="unknown_after_send",
                    scheduled_at=datetime(2026, 7, 24, 10, 0, 0),
                    payload={"target_operation_target_id": 10, "target_reference_revision": 1},
                    result={"gateway_call_started_at": "2026-07-24T10:00:00"},
                ),
                Action(
                    id="a-other",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 10, 0, 0),
                    payload={"target_operation_target_id": 11, "target_reference_revision": 1},
                ),
            ]
        )
        session.commit()

        result = mark_target_group_dissolved(
            session,
            target=target,
            actor="ops",
            reason="admin confirmed",
            evidence_ref="ticket-1",
            expected_version=1,
        )
        session.commit()

        pending = session.get(Action, "a-pending")
        unknown = session.get(Action, "a-unknown")
        other_action = session.get(Action, "a-other")
        assert result.skipped_actions == 1
        assert pending.status == "skipped"
        assert pending.result["error_code"] == ERROR_TARGET_GROUP_DISSOLVED
        assert pending.result["error_message"] == MSG_GROUP_DISSOLVED
        assert unknown.status == "unknown_after_send"
        assert other_action.status == "pending"


def test_target_ref_invalid_blocks_coverage_and_pauses_single_target_task():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(TgGroup(id=5, tenant_id=1, tg_peer_id="g1", title="G1"))
        target = OperationTarget(id=20, tenant_id=1, target_type="group", tg_peer_id="g1", title="G1")
        session.add(target)
        task = Task(
            id="task-single",
            tenant_id=1,
            name="single",
            type="group_ai_chat",
            status="running",
            type_config={"target_operation_target_id": 20},
        )
        session.add(task)
        session.add(
            TaskAccountDailyCoverage(
                id="cov-1",
                tenant_id=1,
                task_id="task-single",
                group_id=5,
                account_id=1,
                coverage_date=datetime(2026, 7, 24).date(),
                state="ready",
            )
        )
        # account_id FK may fail without account - use raw if needed
        session.commit()

        result = mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="username gone",
            evidence_ref="err",
            expected_version=1,
        )
        session.commit()

        task = session.get(Task, "task-single")
        cov = session.get(TaskAccountDailyCoverage, "cov-1")
        assert result.paused_tasks == 1
        assert task.status == "paused"
        assert "引用无效" in task.last_error or "无效" in task.last_error
        assert cov.state == "blocked"
        assert cov.blocker_code == ERROR_TARGET_REF_INVALID
        assert cov.next_eligible_at is None


def test_lifecycle_version_conflict_returns_lookup_error():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(id=3, tenant_id=1, target_type="group", tg_peer_id="x", title="x", lifecycle_version=2)
        session.add(target)
        session.commit()
        try:
            mark_target_group_dissolved(
                session,
                target=target,
                actor="ops",
                reason="r",
                evidence_ref="e",
                expected_version=1,
            )
            assert False, "expected LookupError"
        except LookupError as exc:
            assert "lifecycle_version_conflict" in str(exc)


def test_reactivation_increments_reference_revision():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(
            id=4,
            tenant_id=1,
            target_type="group",
            tg_peer_id="old",
            title="T",
            lifecycle_status=LIFECYCLE_GROUP_DISSOLVED,
            lifecycle_version=3,
            reference_revision=2,
        )
        session.add(target)
        session.add(TgGroup(id=4, tenant_id=1, tg_peer_id="old", title="T", can_send=True))
        session.add(TgGroupAccount(tenant_id=1, group_id=4, account_id=1, can_send=True))
        session.commit()

        updated = reactivate_target(
            session,
            target=target,
            actor="ops",
            reason="new link",
            evidence_ref="ev",
            expected_version=3,
            new_peer_id="newpeer",
            new_username="newuser",
        )
        session.commit()
        assert updated.lifecycle_status == "active"
        assert updated.reference_revision == 3
        assert updated.lifecycle_version == 4
        assert updated.tg_peer_id == "newpeer"


def test_reactivation_requires_a_reverified_reference():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(
            id=44,
            tenant_id=1,
            target_type="group",
            tg_peer_id="old-peer",
            title="需要重新核验",
            lifecycle_status=LIFECYCLE_TARGET_REF_INVALID,
            lifecycle_version=2,
            reference_revision=1,
        )
        session.add(target)
        session.commit()

        with pytest.raises(ValueError, match="引用"):
            reactivate_target(
                session,
                target=target,
                actor="ops",
                reason="恢复",
                evidence_ref="ticket-1",
                expected_version=2,
            )


def test_reactivate_request_schema_requires_a_reverified_reference():
    with pytest.raises(ValueError, match="重新核验"):
        OperationTargetReactivateRequest(
            reason="恢复",
            evidence_ref="ticket-1",
            expected_lifecycle_version=1,
        )


def test_generic_target_patch_cannot_rewrite_send_reference():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(id=46, tenant_id=1, target_type="group", tg_peer_id="before", title="不可改引用")
        session.add(target)
        session.commit()

        with pytest.raises(ValueError, match="引用修复"):
            update_operation_target(
                session,
                1,
                target.id,
                OperationTargetUpdate(tg_peer_id="after"),
                "ops",
            )

        assert session.get(OperationTarget, target.id).tg_peer_id == "before"


def test_terminal_lifecycle_closes_same_epoch_hard_hourly_debt():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(id=45, tenant_id=1, target_type="group", tg_peer_id="g45", title="终态账本")
        task = Task(
            id="task-terminal-ledger",
            tenant_id=1,
            name="terminal ledger",
            type="group_ai_chat",
            status="running",
            type_config={"target_operation_target_id": 45},
        )
        session.add_all([target, task])
        bucket = ensure_bucket(
            session,
            task=task,
            operation_target_id=45,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 14, 0, 0),
            goal=10,
        )
        session.commit()

        mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="username gone",
            evidence_ref="action-1",
            expected_version=1,
        )
        session.commit()

        assert bucket.terminal_blocker_code == ERROR_TARGET_REF_INVALID


def test_terminal_lifecycle_skips_unstarted_legacy_message_and_operation_tasks():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        target = OperationTarget(id=50, tenant_id=1, target_type="group", tg_peer_id="g50", title="旧任务目标")
        session.add(target)
        queued_message = MessageTask(
            id=50,
            tenant_id=1,
            content="queued",
            target_type="group",
            target_peer_id="g50",
            operation_target_id=50,
            target_reference_revision=1,
            status="排队中",
            idempotency_key="queued-message",
        )
        started_message = MessageTask(
            id=51,
            tenant_id=1,
            content="started",
            target_type="group",
            target_peer_id="g50",
            operation_target_id=50,
            target_reference_revision=1,
            status="发送中",
            gateway_call_started_at=datetime(2026, 7, 24, 12),
            idempotency_key="started-message",
        )
        operation_task = OperationTask(
            id=50,
            tenant_id=1,
            task_type="MESSAGE_SEND",
            target_id=50,
            target_reference_revision=1,
            title="旧运营任务",
            content="x",
            status="排队中",
        )
        session.add_all([queued_message, started_message, operation_task])
        session.flush()
        operation_attempt = OperationTaskAttempt(
            id=50,
            tenant_id=1,
            task_id=operation_task.id,
            action_type="MESSAGE_SEND",
            content="x",
            status="排队中",
            idempotency_key="queued-operation",
            scheduled_at=datetime(2026, 7, 24, 12),
        )
        session.add(operation_attempt)
        session.commit()

        result = mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="username gone",
            evidence_ref="err-50",
            expected_version=1,
        )
        session.commit()

        assert result.skipped_message_tasks == 1
        assert result.skipped_operation_tasks == 1
        assert session.get(MessageTask, queued_message.id).failure_type == ERROR_TARGET_REF_INVALID
        assert session.get(MessageTask, started_message.id).status == "发送中"
        assert session.get(OperationTaskAttempt, operation_attempt.id).failure_type == ERROR_TARGET_REF_INVALID
        assert session.get(OperationTask, operation_task.id).status == "失败"


def test_retry_clears_gateway_markers_before_the_next_lifecycle_gate(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add(
            MessageTask(
                id=70,
                tenant_id=1,
                content="retry",
                target_type="group",
                target_peer_id="g70",
                status="失败",
                gateway_call_started_at=datetime(2026, 7, 24, 12),
                idempotency_key="retry-message",
            )
        )
        operation_task = OperationTask(
            id=70,
            tenant_id=1,
            task_type="MESSAGE_SEND",
            title="retry operation",
            content="retry",
            status="失败",
        )
        session.add(operation_task)
        session.flush()
        session.add(
            OperationTaskAttempt(
                id=70,
                tenant_id=1,
                task_id=operation_task.id,
                action_type="MESSAGE_SEND",
                content="retry",
                status="失败",
                gateway_call_started_at=datetime(2026, 7, 24, 12),
                idempotency_key="retry-operation",
            )
        )
        session.commit()

    retried_message = retry_message_task(lambda: Session(engine), 70, "ops", False)
    assert retried_message.gateway_call_started_at is None

    monkeypatch.setattr(
        operations_service,
        "dispatch_operation_task",
        lambda session, task_id, _actor: session.get(OperationTask, task_id),
    )
    with Session(engine) as session:
        operations_service.retry_operation_task(session, 70, "ops")
        attempt = session.get(OperationTaskAttempt, 70)
        assert attempt is not None
        assert attempt.gateway_call_started_at is None


def test_auto_target_ref_invalid_requires_exact_username_evidence_and_no_other_sender():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                OperationTarget(
                    id=60,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-10060",
                    title="青岛师范学院",
                    username="qdsfxy",
                ),
            ]
        )
        session.commit()
        target = session.get(OperationTarget, 60)
        assert target is not None

        result = auto_mark_target_ref_invalid(
            session,
            target=target,
            reference_revision=1,
            account_id=1,
            failure_detail='UsernameNotOccupiedError: No user has "qdsfxy" as username',
            source_ref="action=a60",
        )
        session.commit()

        assert result is not None
        assert result.target.lifecycle_status == LIFECYCLE_TARGET_REF_INVALID


def test_auto_target_ref_invalid_preserves_active_when_another_account_can_send():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                TgGroup(id=61, tenant_id=1, tg_peer_id="-10061", title="仍可发"),
                OperationTarget(
                    id=61,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-10061",
                    title="仍可发",
                    username="still-send",
                ),
                TgGroupAccount(tenant_id=1, group_id=61, account_id=2, can_send=True),
            ]
        )
        session.commit()
        target = session.get(OperationTarget, 61)
        assert target is not None

        result = auto_mark_target_ref_invalid(
            session,
            target=target,
            reference_revision=1,
            account_id=1,
            failure_detail='No user has "still-send" as username',
            source_ref="action=a61",
        )

        assert result is None
        assert session.get(OperationTarget, 61).lifecycle_status == "active"


def test_ref_invalid_skips_unstarted_channel_actions_for_current_or_legacy_reference():
    with _session() as session:
        target = OperationTarget(id=70, tenant_id=1, target_type="channel", tg_peer_id="-10070", title="频道")
        frozen = {"channel_target_id": 70, "target_reference_revision": 1, "target_reference_snapshot": {"tg_peer_id": "-10070"}}
        session.add_all(
            [
                Tenant(id=1, name="t"),
                Task(id="channel-task", tenant_id=1, name="频道任务", type="channel_view", status="running"),
                target,
                _channel_action("view", "view_message", "pending", frozen),
                _channel_action("like", "like_message", "claiming", frozen),
                _channel_action("comment", "post_comment", "executing", frozen),
                _channel_action("legacy", "view_message", "pending", {"channel_target_id": 70}),
                _channel_action("started", "like_message", "pending", frozen, {"gateway_call_started_at": "2026-07-24T10:00:00"}),
                _channel_action("unknown", "post_comment", "unknown_after_send", frozen),
                _channel_action("new-epoch", "view_message", "pending", {**frozen, "target_reference_revision": 2}),
            ]
        )
        session.commit()

        result = mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="频道引用失效",
            evidence_ref="action=70",
            expected_version=1,
        )

        assert result.skipped_actions == 4
        for action_id in ("view", "like", "comment", "legacy"):
            action = session.get(Action, action_id)
            assert action.status == "skipped"
            assert action.result["error_code"] == ERROR_TARGET_REF_INVALID
        assert session.get(Action, "started").status == "pending"
        assert session.get(Action, "unknown").status == "unknown_after_send"
        assert session.get(Action, "new-epoch").status == "pending"


def test_target_lifecycle_ignores_malformed_unrelated_task_target_config():
    with _session() as session:
        target = OperationTarget(id=71, tenant_id=1, target_type="group", tg_peer_id="-10071", title="目标群")
        session.add_all(
            [
                Tenant(id=1, name="t"),
                target,
                Task(
                    id="target-task-71",
                    tenant_id=1,
                    name="目标任务",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 71},
                ),
                Task(
                    id="malformed-task-71",
                    tenant_id=1,
                    name="异常配置任务",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": float("inf")},
                ),
            ]
        )
        session.commit()

        result = mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="目标引用失效",
            evidence_ref="target-71",
            expected_version=1,
        )

        assert result.paused_tasks == 1
        assert session.get(Task, "target-task-71").status == "paused"
        assert session.get(Task, "malformed-task-71").status == "running"
