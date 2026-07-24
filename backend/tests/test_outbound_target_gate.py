from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ChannelMessage,
    ExecutionAttempt,
    MessageTask,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    SchedulingSetting,
    Task,
    TaskStatus,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from sqlalchemy import select
from app.integrations.telegram import OperationResult, SendResult
from app.services.outbound_target_gate import (
    evaluate_outbound_target_gate,
    evaluate_target_lifecycle,
    group_lifecycle_allows_outbound,
)
from app.services.task_center.target_lifecycle import (
    ERROR_TARGET_GROUP_DISSOLVED,
    ERROR_TARGET_REF_INVALID,
    mark_target_group_dissolved,
    mark_target_ref_invalid,
)
import app.services.task_center.dispatcher as dispatcher
import app.services.messages as messages_service
from app.services.task_center.dispatcher import GroupSendGatewayContext, _reserve_group_send_attempt
from app.services.task_center.payloads import EnsureChannelMembershipPayload, SendMessagePayload, ViewMessagePayload
from app.schemas.operations import ManualSendRequest
from app.services.messages import _mark_message_task_gateway_started
from app.services.operations import _reserve_operation_gateway_attempt, dispatch_operation_task, manual_send

pytestmark = pytest.mark.no_postgres


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _enable_canary_gate(session: Session, tenant_id: int = 1) -> None:
    session.add(SchedulingSetting(tenant_id=tenant_id, outbound_target_gate_mode="canary"))


def _seed_terminalized_gateway_rows(session: Session) -> OperationTarget:
    session.add(Tenant(id=1, name="t"))
    _enable_canary_gate(session)
    target = OperationTarget(id=7, tenant_id=1, target_type="group", tg_peer_id="-1007", title="G7")
    session.add_all(
        [
            target,
            TgAccount(id=7, tenant_id=1, display_name="发送号", phone_masked="+861***0007", status="在线"),
            MessageTask(
                id=7,
                tenant_id=1,
                content="hello",
                target_type="group",
                target_peer_id="-1007",
                operation_target_id=7,
                target_reference_revision=1,
                status=TaskStatus.QUEUED.value,
                idempotency_key="message-terminal-gate",
            ),
            OperationTask(
                id=7,
                tenant_id=1,
                task_type="MESSAGE_SEND",
                target_id=7,
                target_reference_revision=1,
                target_reference_snapshot={"tg_peer_id": "-1007"},
                content="hello",
                status=TaskStatus.QUEUED.value,
            ),
        ]
    )
    session.flush()
    session.add(
        OperationTaskAttempt(
            id=7,
            tenant_id=1,
            task_id=7,
            account_id=7,
            action_type="MESSAGE_SEND",
            content="hello",
            status=TaskStatus.QUEUED.value,
            idempotency_key="operation-terminal-gate",
        )
    )
    session.commit()
    return target


def test_dissolved_target_blocks_gate_and_group_send_reserve_path():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        _enable_canary_gate(session)
        target = OperationTarget(id=1, tenant_id=1, target_type="group", tg_peer_id="-1001", title="G")
        session.add(target)
        session.add(TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="G", active_window="00:00-23:59"))
        session.add(Task(id="task-1", tenant_id=1, name="t", type="group_ai_chat", status="running"))
        action = Action(
            id="a1",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12, 0, 0),
            payload={"target_operation_target_id": 1, "target_reference_revision": 1, "group_id": 1},
        )
        session.add(action)
        session.commit()

        mark_target_group_dissolved(
            session,
            target=target,
            actor="ops",
            reason="confirmed",
            evidence_ref="e1",
            expected_version=1,
        )
        session.commit()

        block = evaluate_outbound_target_gate(session, action=session.get(Action, "a1"), group=session.get(TgGroup, 1))
        assert block is not None
        assert block.code == ERROR_TARGET_GROUP_DISSOLVED


def test_target_ref_invalid_blocks_manual_style_lifecycle_gate():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        _enable_canary_gate(session)
        target = OperationTarget(id=2, tenant_id=1, target_type="group", tg_peer_id="x", title="X")
        session.add(target)
        session.commit()
        mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="username gone",
            evidence_ref="e",
            expected_version=1,
        )
        session.commit()
        block = evaluate_target_lifecycle(session.get(OperationTarget, 2), require_identity=True)
        assert block is not None
        assert block.code == ERROR_TARGET_REF_INVALID
        assert "解散" not in block.detail


def test_group_lifecycle_allows_outbound_when_linked_target_active():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        session.add(OperationTarget(id=3, tenant_id=1, target_type="group", tg_peer_id="-1003", title="G3"))
        group = TgGroup(id=3, tenant_id=1, tg_peer_id="-1003", title="G3")
        session.add(group)
        session.commit()
        assert group_lifecycle_allows_outbound(session, group) is None


def test_frozen_action_snapshot_cannot_send_to_a_different_group_peer():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        _enable_canary_gate(session)
        session.add(OperationTarget(id=4, tenant_id=1, target_type="group", tg_peer_id="-1004", title="G4"))
        group = TgGroup(id=4, tenant_id=1, tg_peer_id="-1004", title="G4", active_window="00:00-23:59")
        action = Action(
            id="a4",
            tenant_id=1,
            task_id="task-4",
            task_type="group_ai_chat",
            action_type="send_message",
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12, 0, 0),
            payload={
                "target_operation_target_id": 4,
                "target_reference_revision": 1,
                "target_reference_snapshot": {"tg_peer_id": "-1004-old"},
                "group_id": 4,
            },
        )
        session.add_all([group, Task(id="task-4", tenant_id=1, name="t", type="group_ai_chat", status="running"), action])
        session.commit()

        block = evaluate_outbound_target_gate(session, action=action, group=group, require_identity=True)

        assert block is not None
        assert block.code == "target_reference_superseded"


def test_full_gate_blocks_action_without_frozen_identity_even_if_task_target_resolves():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="full"),
                OperationTarget(
                    id=8,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1008-new",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(
                    id="task-8",
                    tenant_id=1,
                    name="t",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 8},
                ),
                Action(
                    id="a8",
                    tenant_id=1,
                    task_id="task-8",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"chat_id": "-1008-old", "message_text": "x"},
                ),
            ]
        )
        session.commit()

        block = evaluate_outbound_target_gate(
            session,
            action=session.get(Action, "a8"),
            outbound_peer="-1008-old",
        )

        assert block is not None
        assert block.code == "target_identity_unresolved"


def test_full_gate_blocks_action_missing_frozen_snapshot_with_payload_target_id():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="full"),
                OperationTarget(
                    id=9,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1009-new",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(
                    id="task-9",
                    tenant_id=1,
                    name="t",
                    type="group_ai_chat",
                    status="running",
                    type_config={"target_operation_target_id": 9},
                ),
                Action(
                    id="a9",
                    tenant_id=1,
                    task_id="task-9",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "target_operation_target_id": 9,
                        "target_reference_revision": 2,
                        "chat_id": "-1009-old",
                        "message_text": "x",
                    },
                ),
            ]
        )
        session.commit()

        block = evaluate_outbound_target_gate(session, action=session.get(Action, "a9"), outbound_peer="-1009-old")

        assert block is not None
        assert block.code == "target_identity_unresolved"


def test_full_gate_blocks_action_with_blank_frozen_snapshot_peer():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="full"),
                OperationTarget(
                    id=10,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1010",
                    title="当前目标",
                    reference_revision=2,
                ),
                Task(id="task-10", tenant_id=1, name="t", type="group_ai_chat", status="running"),
                Action(
                    id="a10",
                    tenant_id=1,
                    task_id="task-10",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "target_operation_target_id": 10,
                        "target_reference_revision": 2,
                        "target_reference_snapshot": {"tg_peer_id": "   "},
                        "chat_id": "-1010",
                        "message_text": "x",
                    },
                ),
            ]
        )
        session.commit()

        block = evaluate_outbound_target_gate(session, action=session.get(Action, "a10"), outbound_peer="-1010")

        assert block is not None
        assert block.code == "target_identity_unresolved"


@pytest.mark.parametrize(
    ("mode", "expect_block", "target_id", "revision"),
    [
        ("full", True, 11, "not-an-int"),
        ("dual_read", False, 11, "not-an-int"),
        ("full", True, 11, float("inf")),
        ("dual_read", False, 11, float("inf")),
        ("full", True, float("inf"), 1),
        ("dual_read", False, float("inf"), 1),
    ],
)
def test_gate_handles_malformed_action_reference_revision_by_mode(mode, expect_block, target_id, revision):
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode=mode),
                OperationTarget(id=11, tenant_id=1, target_type="group", tg_peer_id="-1011", title="当前目标"),
                Task(id="task-11", tenant_id=1, name="t", type="group_ai_chat", status="running"),
                Action(
                    id=f"a11-{mode}",
                    tenant_id=1,
                    task_id="task-11",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "target_operation_target_id": target_id,
                        "target_reference_revision": revision,
                        "target_reference_snapshot": {"tg_peer_id": "-1011"},
                        "chat_id": "-1011",
                        "message_text": "x",
                    },
                ),
            ]
        )
        session.commit()

        action = session.get(Action, f"a11-{mode}")
        block = evaluate_outbound_target_gate(session, action=action, outbound_peer="-1011")

        if expect_block:
            assert block is not None
            assert block.code == "target_identity_unresolved"
        else:
            assert block is None
            assert action.result["outbound_target_gate_diagnostic"]["code"] == "target_identity_unresolved"
            if mode == "dual_read":
                audit_rows = list(session.scalars(select(AuditLog).where(AuditLog.action == "出站目标门禁诊断")))
                assert len(audit_rows) == 1
                assert "mode=dual_read" in (audit_rows[0].detail or "")
                assert "target_identity_unresolved" in (audit_rows[0].detail or "")


def test_dual_read_records_action_diagnostic_and_audit_without_blocking():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="dual_read"),
                OperationTarget(
                    id=21,
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id="-1021",
                    title="terminal",
                    lifecycle_status="target_ref_invalid",
                    reference_revision=1,
                ),
                Task(id="task-dual-read", tenant_id=1, name="t", type="group_ai_chat", status="running"),
                Action(
                    id="a-dual-read",
                    tenant_id=1,
                    task_id="task-dual-read",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "target_operation_target_id": 21,
                        "target_reference_revision": 1,
                        "target_reference_snapshot": {"tg_peer_id": "-1021"},
                        "chat_id": "-1021",
                        "message_text": "x",
                    },
                ),
            ]
        )
        session.commit()

        action = session.get(Action, "a-dual-read")
        block = evaluate_outbound_target_gate(session, action=action, outbound_peer="-1021")
        session.commit()

        assert block is None
        assert action.result["outbound_target_gate_diagnostic"]["code"] == "target_ref_invalid"
        audits = list(session.scalars(select(AuditLog).where(AuditLog.target_id == "a-dual-read")))
        assert len(audits) == 1
        assert audits[0].action == "出站目标门禁诊断"
        assert "mode=dual_read" in (audits[0].detail or "")


def test_group_attempt_marks_gateway_started_before_releasing_target_lock():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        account = TgAccount(id=5, tenant_id=1, display_name="发送号", phone_masked="+861***0005", status="在线")
        target = OperationTarget(id=5, tenant_id=1, target_type="group", tg_peer_id="-1005", title="G5")
        group = TgGroup(id=5, tenant_id=1, tg_peer_id="-1005", title="G5", active_window="00:00-23:59")
        action = Action(
            id="a5",
            tenant_id=1,
            task_id="task-5",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=5,
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12, 0, 0),
            payload={
                "group_id": 5,
                "target_operation_target_id": 5,
                "target_reference_revision": 1,
                "target_reference_snapshot": {"tg_peer_id": "-1005"},
            },
        )
        link = TgGroupAccount(tenant_id=1, group_id=5, account_id=5, can_send=True)
        session.add_all([account, target, group, link, Task(id="task-5", tenant_id=1, name="t", type="group_ai_chat", status="running"), action])
        session.commit()

        payload = SendMessagePayload(
            group_id=5,
            target_operation_target_id=5,
            target_reference_revision=1,
            target_reference_snapshot={"tg_peer_id": "-1005"},
            message_text="hello",
        )
        attempt = _reserve_group_send_attempt(
            session,
            action,
            GroupSendGatewayContext(account, object(), group, link, payload, "hello"),
        )

        assert attempt is not None
        stored = session.get(ExecutionAttempt, attempt.id)
        assert stored is not None
        assert stored.status == "gateway_call_started"
        assert stored.gateway_call_started_at is not None
        assert session.get(Action, action.id).status == "executing"


def test_manual_send_rejects_cross_tenant_target_before_gateway():
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="tenant-1"),
                Tenant(id=2, name="tenant-2"),
                TgAccount(id=6, tenant_id=1, display_name="发送号", phone_masked="+861***0006", status="在线"),
                OperationTarget(id=6, tenant_id=2, target_type="group", tg_peer_id="-1006", title="跨租户目标"),
            ]
        )
        session.commit()

        with pytest.raises(ValueError, match="target not found"):
            manual_send(session, 6, ManualSendRequest(target_id=6, content="hello"), "ops")


def test_legacy_message_and_operation_attempts_cannot_enter_gateway_after_terminalization():
    with _session() as session:
        target = _seed_terminalized_gateway_rows(session)

        mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="username gone",
            evidence_ref="e7",
            expected_version=1,
        )
        session.commit()

        message = session.get(MessageTask, 7)
        assert message is not None
        message_block = _mark_message_task_gateway_started(session, message, account_id=7)
        assert message_block is not None
        assert message_block.code == "message_task_not_queued"
        assert message.status == TaskStatus.FAILED.value
        assert session.get(MessageTask, 7).gateway_call_started_at is None

        task = session.get(OperationTask, 7)
        stored_attempt = session.get(OperationTaskAttempt, 7)
        assert task is not None and stored_attempt is not None
        _target, _attempt, code, _detail = _reserve_operation_gateway_attempt(session, task, stored_attempt, session.get(OperationTarget, 7))
        assert code == "operation_attempt_not_queued"
        assert stored_attempt.status == TaskStatus.FAILED.value
        assert session.get(OperationTaskAttempt, 7).gateway_call_started_at is None


def test_full_gate_uses_frozen_channel_identity_and_blocks_legacy_channel_action():
    with _session() as session:
        target = OperationTarget(id=12, tenant_id=1, target_type="channel", tg_peer_id="-10012", title="频道")
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="full"),
                Task(id="channel-task", tenant_id=1, name="频道任务", type="channel_like", status="running"),
                target,
                Action(
                    id="legacy-channel",
                    tenant_id=1,
                    task_id="channel-task",
                    task_type="channel_like",
                    action_type="like_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={"channel_target_id": 12, "channel_id": "-10012", "message_id": 1},
                ),
                Action(
                    id="frozen-channel",
                    tenant_id=1,
                    task_id="channel-task",
                    task_type="channel_like",
                    action_type="like_message",
                    status="pending",
                    scheduled_at=datetime(2026, 7, 24, 12),
                    payload={
                        "channel_target_id": 12,
                        "channel_id": "-10012",
                        "message_id": 1,
                        "target_reference_revision": 1,
                        "target_reference_snapshot": {"tg_peer_id": "-10012", "username": "", "title": "频道"},
                    },
                ),
            ]
        )
        session.commit()

        legacy = evaluate_outbound_target_gate(session, action=session.get(Action, "legacy-channel"), outbound_peer="-10012")
        frozen = evaluate_outbound_target_gate(session, action=session.get(Action, "frozen-channel"), outbound_peer="-10012")

        assert legacy is not None
        assert legacy.code == "target_identity_unresolved"
        assert frozen is None


def test_channel_view_target_ref_invalid_does_not_call_gateway(monkeypatch):
    with _session() as session:
        target = OperationTarget(id=13, tenant_id=1, target_type="channel", tg_peer_id="-10013", title="频道")
        account = TgAccount(id=13, tenant_id=1, display_name="账号", phone_masked="+861***0013", status="在线")
        action = Action(
            id="view-target-invalid",
            tenant_id=1,
            task_id="channel-view-task",
            task_type="channel_view",
            action_type="view_message",
            account_id=13,
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12),
            payload={"channel_id": "-10013", "channel_target_id": 13, "message_id": 1},
        )
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="canary"),
                Task(id="channel-view-task", tenant_id=1, name="频道浏览", type="channel_view", status="running"),
                target,
                account,
                action,
            ]
        )
        session.commit()
        mark_target_ref_invalid(session, target=target, actor="ops", reason="引用失效", evidence_ref="a13", expected_version=1)
        monkeypatch.setattr(dispatcher, "_ensure_channel_action_membership", lambda *_args: True)
        monkeypatch.setattr(
            dispatcher.gateway,
            "view_channel_message",
            lambda *_args: (_ for _ in ()).throw(AssertionError("invalid target must not reach gateway")),
        )

        assert dispatcher._dispatch_view(action, account, object(), session, ViewMessagePayload(**action.payload)) is True

        assert action.result["error_code"] == ERROR_TARGET_REF_INVALID
        assert action.result.get("gateway_call_started_at") is None
        assert session.query(ExecutionAttempt).filter(ExecutionAttempt.action_id == action.id).count() == 0


def test_full_gate_legacy_message_task_never_reaches_gateway(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="full"),
                OperationTarget(id=14, tenant_id=1, target_type="group", tg_peer_id="-10014", title="目标群"),
                TgGroup(id=14, tenant_id=1, tg_peer_id="-10014", title="目标群", auth_status="已授权运营", can_send=True),
                TgAccount(id=14, tenant_id=1, display_name="账号", phone_masked="+861***0014", status="在线"),
                TgGroupAccount(tenant_id=1, group_id=14, account_id=14, can_send=True),
                MessageTask(
                    id=14,
                    tenant_id=1,
                    group_id=14,
                    preferred_account_id=14,
                    content="legacy",
                    target_type="group",
                    target_peer_id="-10014",
                    operation_target_id=14,
                    scheduled_at=datetime(2020, 1, 1),
                    status=TaskStatus.QUEUED.value,
                    idempotency_key="legacy-full-message",
                ),
            ]
        )
        session.commit()

    calls: list[tuple] = []
    monkeypatch.setattr(messages_service, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(
        messages_service.gateway,
        "send_message",
        lambda *args: calls.append(args) or SendResult(True, remote_message_id="message-14"),
    )

    result = messages_service.dispatch_task(lambda: Session(engine), 14)

    assert calls == []
    assert result.failure_type == "target_identity_unresolved"
    assert result.gateway_call_started_at is None


@pytest.mark.parametrize(
    ("mode", "terminalize", "expected_failure"),
    [
        ("full", False, "target_identity_unresolved"),
        ("canary", True, ERROR_TARGET_REF_INVALID),
    ],
)
def test_legacy_channel_view_never_reaches_gateway_when_gate_blocks(
    monkeypatch,
    mode: str,
    terminalize: bool,
    expected_failure: str,
):
    with _session() as session:
        target = OperationTarget(id=15, tenant_id=1, target_type="channel", tg_peer_id="-10015", title="频道")
        task = OperationTask(
            id=15,
            tenant_id=1,
            task_type="CHANNEL_VIEW",
            channel_message_id=15,
            title="legacy view",
            status=TaskStatus.QUEUED.value,
        )
        attempt = OperationTaskAttempt(
            id=15,
            tenant_id=1,
            task_id=15,
            account_id=15,
            action_type="CHANNEL_VIEW",
            status=TaskStatus.QUEUED.value,
            idempotency_key=f"legacy-channel-view-{mode}",
            scheduled_at=datetime(2020, 1, 1),
        )
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode=mode),
                target,
                ChannelMessage(id=15, tenant_id=1, channel_target_id=15, message_id=101),
                TgGroup(id=15, tenant_id=1, tg_peer_id="-10015", title="频道群"),
                TgAccount(id=15, tenant_id=1, display_name="账号", phone_masked="+861***0015", status="在线"),
                TgGroupAccount(tenant_id=1, group_id=15, account_id=15, can_send=True),
                task,
                attempt,
            ]
        )
        session.commit()
        if terminalize:
            mark_target_ref_invalid(
                session,
                target=target,
                actor="ops",
                reason="频道引用失效",
                evidence_ref="view-15",
                expected_version=1,
            )
            session.commit()

        calls: list[tuple] = []
        monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            "app.services.operations.gateway.view_channel_message",
            lambda *args: calls.append(args) or OperationResult(True, "成功"),
        )

        dispatch_operation_task(session, task.id, "ops")

        stored_attempt = session.get(OperationTaskAttempt, attempt.id)
        assert calls == []
        assert stored_attempt is not None
        assert stored_attempt.failure_type == expected_failure
        assert stored_attempt.gateway_call_started_at is None


def test_invalid_membership_action_never_reaches_gateway(monkeypatch):
    with _session() as session:
        target = OperationTarget(id=16, tenant_id=1, target_type="group", tg_peer_id="-10016", title="准入群")
        action = Action(
            id="invalid-membership",
            tenant_id=1,
            task_id="membership-task",
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=16,
            status="pending",
            scheduled_at=datetime(2026, 7, 24, 12),
            payload={
                "channel_id": "-10016",
                "channel_target_id": 16,
                "target_type": "group",
                "target_display": "准入群",
                "require_send": True,
            },
        )
        account = TgAccount(id=16, tenant_id=1, display_name="账号", phone_masked="+861***0016", status="在线")
        session.add_all(
            [
                Tenant(id=1, name="t"),
                SchedulingSetting(tenant_id=1, outbound_target_gate_mode="canary"),
                Task(id="membership-task", tenant_id=1, name="准入", type="group_ai_chat", status="running"),
                target,
                account,
                action,
            ]
        )
        session.commit()
        mark_target_ref_invalid(
            session,
            target=target,
            actor="ops",
            reason="准入群引用失效",
            evidence_ref="membership-16",
            expected_version=1,
        )

        calls: list[tuple] = []
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda *args, **kwargs: calls.append(args) or OperationResult(True, "成功"),
        )

        assert dispatcher._dispatch_channel_membership(
            session,
            action,
            account,
            object(),
            EnsureChannelMembershipPayload(**action.payload),
        ) is True

        assert calls == []
        assert action.result["error_code"] == ERROR_TARGET_REF_INVALID
