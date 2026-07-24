from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    MessageTask,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    Task,
    TaskHardHourlyBucket,
    Tenant,
    TgGroup,
)
from app.schemas.ai_config import SchedulingSettingUpdate
from app.services.ai_config import update_scheduling_setting
from app.services.task_center.outbound_identity_reconcile import (
    outbound_identity_inventory,
    reconcile_outbound_identity,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_bound_target(session: Session) -> None:
    session.add_all(
        [
            Tenant(id=1, name="tenant"),
            OperationTarget(id=1, tenant_id=1, target_type="group", tg_peer_id="-1001", title="目标群"),
            OperationTarget(id=2, tenant_id=1, target_type="channel", tg_peer_id="-1002", title="目标频道"),
            TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="目标群"),
            Task(id="group-task", tenant_id=1, name="群任务", type="group_ai_chat", status="running"),
        ]
    )


def _seed_reconcile_rows(session: Session) -> None:
    _seed_bound_target(session)
    operation = OperationTask(
        id=1, tenant_id=1,
        task_type="CHANNEL_REPLY",
        channel_message_id=1,
        title="legacy reply",
        content="x",
        status="排队中",
    )
    session.add_all(
        [
            Action(
                id="action-1",
                tenant_id=1,
                task_id="group-task",
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=datetime(2026, 7, 24, 12),
                payload={"group_id": 1, "chat_id": "-1001", "message_text": "x"},
            ),
            MessageTask(
                id=1,
                tenant_id=1,
                group_id=1,
                content="legacy",
                target_type="group",
                target_peer_id="-1001",
                status="排队中",
                idempotency_key="message-1",
            ),
            ChannelMessage(id=1, tenant_id=1, channel_target_id=2, message_id=10),
            operation,
        ]
    )
    session.flush()
    session.add(
        OperationTaskAttempt(
            id=1,
            tenant_id=1,
            task_id=operation.id,
            action_type="CHANNEL_REPLY",
            content="x",
            status="排队中",
            idempotency_key="operation-1",
            scheduled_at=datetime(2026, 7, 24, 12),
        )
    )
    session.commit()


def _seed_sibling_gateway_attempts(session: Session) -> None:
    session.add_all(
        [
            Tenant(id=1, name="tenant"),
            OperationTarget(id=12, tenant_id=1, target_type="channel", tg_peer_id="-1012", title="频道"),
            ChannelMessage(id=12, tenant_id=1, channel_target_id=12, message_id=12),
            OperationTask(
                id=12,
                tenant_id=1,
                task_type="CHANNEL_REPLY",
                channel_message_id=12,
                title="存在已入网关 sibling",
                content="x",
                status="排队中",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            OperationTaskAttempt(
                id=121,
                tenant_id=1,
                task_id=12,
                action_type="CHANNEL_REPLY",
                content="x",
                status="gateway_call_started",
                idempotency_key="gateway-started-sibling",
                scheduled_at=datetime(2026, 7, 24, 12),
                gateway_call_started_at=datetime(2026, 7, 24, 12, 1),
            ),
            OperationTaskAttempt(
                id=122,
                tenant_id=1,
                task_id=12,
                action_type="CHANNEL_REPLY",
                content="x",
                status="排队中",
                idempotency_key="queued-sibling",
                scheduled_at=datetime(2026, 7, 24, 12, 2),
            ),
        ]
    )
    session.commit()


def test_reconcile_binds_exact_peer_rows_without_title_matching():
    with _session() as session:
        _seed_reconcile_rows(session)

        preview = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=False)
        assert preview.bound_action_count == 1
        assert preview.bound_message_task_count == 1
        assert preview.bound_operation_task_count == 1
        assert session.get(Action, "action-1").payload.get("target_operation_target_id") is None

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        action = session.get(Action, "action-1")
        message = session.get(MessageTask, 1)
        operation = session.get(OperationTask, 1)
        assert result.inventory.total == 0
        assert action.payload["target_operation_target_id"] == 1
        assert action.payload["target_reference_snapshot"]["tg_peer_id"] == "-1001"
        assert message.operation_target_id == 1
        assert operation.target_id == 2


def test_reconcile_repairs_zero_value_identity_placeholders_only_when_exact_peer_matches():
    with _session() as session:
        _seed_bound_target(session)
        session.add(
            Action(
                id="placeholder-action",
                tenant_id=1,
                task_id="group-task",
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=datetime(2026, 7, 24, 12),
                payload={
                    "group_id": 1,
                    "chat_id": "-1001",
                    "target_operation_target_id": 0,
                    "target_reference_revision": 0,
                    "target_reference_snapshot": {},
                },
            )
        )
        session.commit()

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        action = session.get(Action, "placeholder-action")
        assert result.bound_action_count == 1
        assert action is not None
        assert action.payload["target_operation_target_id"] == 1
        assert action.payload["target_reference_revision"] == 1
        assert action.payload["target_reference_snapshot"]["tg_peer_id"] == "-1001"


def test_full_gate_cannot_be_enabled_while_unresolved_rows_remain():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                Task(id="private-task", tenant_id=1, name="私聊", type="group_ai_chat", status="running"),
                Action(
                    id="private-action",
                    tenant_id=1,
                    task_id="private-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"chat_id": "private-peer", "message_text": "x"},
                ),
            ]
        )
        session.commit()

        assert outbound_identity_inventory(session, 1).total == 1
        with pytest.raises(ValueError, match="完成出站目标身份对账"):
            update_scheduling_setting(
                session,
                1,
                SchedulingSettingUpdate(outbound_target_gate_mode="full"),
                "ops",
            )


def test_inventory_does_not_treat_task_target_as_action_frozen_identity():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(
                    id=1,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-100-new",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(
                    id="task-current-target",
                    tenant_id=1,
                    name="当前目标任务",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 1},
                ),
                Action(
                    id="legacy-action-without-frozen-identity",
                    tenant_id=1,
                    task_id="task-current-target",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"chat_id": "-100-old", "message_text": "x"},
                ),
            ]
        )
        session.commit()

        inventory = outbound_identity_inventory(session, 1)

        assert inventory.unresolved_action_count == 1
        with pytest.raises(ValueError, match="完成出站目标身份对账"):
            update_scheduling_setting(
                session,
                1,
                SchedulingSettingUpdate(outbound_target_gate_mode="full"),
                "ops",
            )


def test_inventory_requires_complete_identity_when_action_uses_operation_target_id():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(
                    id=9,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1009-new",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(
                    id="task-operation-target-id",
                    tenant_id=1,
                    name="当前目标任务",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 9},
                ),
                Action(
                    id="action-missing-frozen-reference",
                    tenant_id=1,
                    task_id="task-operation-target-id",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"operation_target_id": 9, "chat_id": "-1009-old", "message_text": "x"},
                ),
            ]
        )
        session.commit()

        assert outbound_identity_inventory(session, 1).unresolved_action_count == 1


def test_inventory_includes_incomplete_channel_actions_before_full_gate_switch():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(id=18, tenant_id=1, target_type="channel", tg_peer_id="-10018", title="频道"),
                Task(id="channel-task", tenant_id=1, name="频道任务", type="channel_view", status="running"),
            ]
        )
        session.add_all(
            [
                Action(
                    id=f"legacy-{action_type}",
                    tenant_id=1,
                    task_id="channel-task",
                    task_type="channel_view",
                    action_type=action_type,
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"channel_target_id": 18, "channel_id": "-10018", "message_id": 1},
                )
                for action_type in ("view_message", "like_message", "post_comment")
            ]
        )
        session.commit()

        assert outbound_identity_inventory(session, 1).unresolved_action_count == 3


@pytest.mark.parametrize(
    ("target_id", "revision"),
    [(float("inf"), 1), (1, float("inf"))],
)
def test_inventory_treats_infinite_action_identity_values_as_unresolved(target_id, revision):
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(id=1, tenant_id=1, target_type="group", tg_peer_id="-1001", title="目标"),
                Task(id="infinite-task", tenant_id=1, name="任务", type="group_ai_chat", status="running"),
                Action(
                    id=f"infinite-identity-{target_id}-{revision}",
                    tenant_id=1,
                    task_id="infinite-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "target_operation_target_id": target_id,
                        "target_reference_revision": revision,
                        "target_reference_snapshot": {"tg_peer_id": "-1001"},
                    },
                ),
            ]
        )
        session.commit()

        assert outbound_identity_inventory(session, 1).unresolved_action_count == 1


def test_reconcile_does_not_bind_task_current_target_to_old_action_peer():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(
                    id=10,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1010-new",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(
                    id="task-current-target-only",
                    tenant_id=1,
                    name="当前目标任务",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 10},
                ),
                Action(
                    id="legacy-action-with-old-peer",
                    tenant_id=1,
                    task_id="task-current-target-only",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"chat_id": "-1010-old", "message_text": "x"},
                ),
            ]
        )
        session.commit()

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        action = session.get(Action, "legacy-action-with-old-peer")
        assert result.bound_action_count == 0
        assert result.inventory.unresolved_action_count == 1
        assert action is not None
        assert action.payload.get("target_operation_target_id") is None
        assert outbound_identity_inventory(session, 1).unresolved_action_count == 1


def test_reconcile_does_not_mutate_operation_task_after_gateway_started():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(id=11, tenant_id=1, target_type="channel", tg_peer_id="-1011", title="频道"),
                ChannelMessage(id=11, tenant_id=1, channel_target_id=11, message_id=11),
                OperationTask(
                    id=11,
                    tenant_id=1,
                    task_type="CHANNEL_REPLY",
                    channel_message_id=11,
                    title="已进入网关",
                    content="x",
                    status="排队中",
                ),
            ]
        )
        session.flush()
        session.add(
            OperationTaskAttempt(
                id=11,
                tenant_id=1,
                task_id=11,
                action_type="CHANNEL_REPLY",
                content="x",
                status="排队中",
                idempotency_key="gateway-started-operation",
                scheduled_at=datetime(2026, 7, 24, 12),
                gateway_call_started_at=datetime(2026, 7, 24, 12, 1),
            )
        )
        session.commit()

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        operation = session.get(OperationTask, 11)
        assert result.bound_operation_task_count == 0
        assert operation is not None
        assert operation.target_id is None


def test_reconcile_does_not_mutate_task_when_a_sibling_attempt_started_gateway():
    with _session() as session:
        _seed_sibling_gateway_attempts(session)

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        operation = session.get(OperationTask, 12)
        assert result.bound_operation_task_count == 0
        assert operation is not None
        assert operation.target_id is None


def test_reconcile_anchors_open_hard_hourly_action_to_its_planned_bucket():
    with _session() as session:
        _seed_bound_target(session)
        task = session.get(Task, "group-task")
        assert task is not None
        task.type_config = {"hard_hourly_target_enabled": True, "hourly_min_messages": 3}
        task.config_revision = 4
        session.add(
            Action(
                id="hard-action",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=datetime(2026, 7, 24, 14, 55),
                payload={"group_id": 1, "chat_id": "-1001", "hard_hourly_target": True, "message_text": "x"},
            )
        )
        session.commit()

        reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        action = session.get(Action, "hard-action")
        bucket = session.scalar(select(TaskHardHourlyBucket).where(TaskHardHourlyBucket.task_id == task.id))
        assert action.payload["hard_hourly_bucket"].startswith("2026-07-24T14:00:00")
        assert action.payload["hard_hourly_goal_at_plan"] == 3
        assert action.payload["task_config_revision"] == 4
        assert bucket is not None
        assert bucket.goal == 3


def test_reconcile_preview_uses_channel_identity_without_parsing_malformed_group_id():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(id=20, tenant_id=1, target_type="channel", tg_peer_id="-10020", title="频道"),
                Task(id="channel-task-20", tenant_id=1, name="频道任务", type="channel_view", status="running"),
                Action(
                    id="channel-malformed-group",
                    tenant_id=1,
                    task_id="channel-task-20",
                    task_type="channel_view",
                    action_type="view_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "channel_target_id": 20,
                        "channel_id": "-10020",
                        "message_id": 1,
                        "group_id": float("inf"),
                    },
                ),
            ]
        )
        session.commit()

        result = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=False)

        action = session.get(Action, "channel-malformed-group")
        assert result.bound_action_count == 1
        assert result.inventory.unresolved_action_count == 1
        assert action is not None
        assert action.payload["channel_target_id"] == 20
        assert "target_reference_revision" not in action.payload

        applied = reconcile_outbound_identity(session, tenant_id=1, actor="ops", apply=True)
        session.commit()

        action = session.get(Action, "channel-malformed-group")
        assert applied.inventory.unresolved_action_count == 0
        assert action is not None
        assert action.payload["channel_target_id"] == 20
        assert "target_operation_target_id" not in action.payload
        assert action.payload["target_reference_snapshot"]["tg_peer_id"] == "-10020"


def test_inventory_requires_frozen_identity_for_message_and_channel_view_attempts():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant"),
                OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="群"),
                OperationTarget(id=22, tenant_id=1, target_type="channel", tg_peer_id="-10022", title="频道"),
                MessageTask(
                    id=21,
                    tenant_id=1,
                    content="legacy",
                    target_type="group",
                    target_peer_id="-10021",
                    operation_target_id=21,
                    status="排队中",
                    idempotency_key="legacy-message-21",
                ),
                ChannelMessage(id=22, tenant_id=1, channel_target_id=22, message_id=1),
                OperationTask(
                    id=22,
                    tenant_id=1,
                    task_type="CHANNEL_VIEW",
                    target_id=22,
                    channel_message_id=22,
                    title="legacy view",
                    status="排队中",
                ),
            ]
        )
        session.flush()
        session.add(
            OperationTaskAttempt(
                id=22,
                tenant_id=1,
                task_id=22,
                action_type="CHANNEL_VIEW",
                status="排队中",
                idempotency_key="legacy-view-22",
                scheduled_at=datetime(2026, 7, 24, 12),
            )
        )
        session.commit()

        inventory = outbound_identity_inventory(session, tenant_id=1)

        assert inventory.unresolved_message_task_count == 1
        assert inventory.unresolved_operation_attempt_count == 1
