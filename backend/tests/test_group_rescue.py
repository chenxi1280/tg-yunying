from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import ChannelMembershipResult, DeveloperAppCredentials, InviteLinkResult, OperationResult
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import AccountStatus, Action, FailureType, OperationTarget, Task, TaskMembershipAdmissionItem, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas import TenantGroupRescueSettingsUpdate
from app.services.task_center import dispatcher
from app.services.task_center.account_pool import select_task_accounts
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.group_rescue import permission_failure_count_for_send_action
from app.services.task_center.membership_admission import lock_membership_admission_snapshot, sync_membership_admission_items
from app.services.task_center.membership_admission import retry_membership_admission_rescue
from app.services.tenants import group_rescue_settings_payload, update_group_rescue_settings


NOW = datetime(2026, 6, 22, 10, 0, 0)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_rescue_target(session: Session, *, configured: bool = True) -> None:
    tenant = Tenant(id=1, name="默认运营空间")
    if configured:
        tenant.group_rescue_enabled = True
        tenant.group_rescue_admin_account_id = 99
    session.add(tenant)
    session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-10021", title="目标群"))
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-10021", title="目标群"))
    session.add(TgAccount(id=11, tenant_id=1, display_name="普通账号", username="normal_user", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"))
    session.add(TgAccount(id=99, tenant_id=1, display_name="救援账号", phone_masked="99", status=AccountStatus.ACTIVE.value, session_ciphertext="session-99"))
    session.add(Task(id="task-rescue", tenant_id=1, name="准入", type="group_membership_admission", status="running", type_config={"target_operation_target_id": 21}))
    session.add(TaskMembershipAdmissionItem(tenant_id=1, task_id="task-rescue", account_id=11, target_id=21, phase="joining"))
    session.commit()


def _patch_group_membership_denied(monkeypatch) -> None:
    denied = OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, "群无权限或账号不可发言")
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: denied)
    monkeypatch.setattr(dispatcher, "_recover_group_send_permission_with_linked_channel", lambda *_args, **_kwargs: denied)
    monkeypatch.setattr(dispatcher, "_auto_verify_and_apply_group_send", lambda *_args, **_kwargs: False)


def _group_ai_membership_action(action_id: str, task: Task) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="ensure_target_membership",
        account_id=11,
        scheduled_at=NOW,
        status="pending",
        payload={
            "channel_id": "-10021",
            "channel_target_id": 21,
            "target_type": "group",
            "target_display": "目标群",
            "require_send": True,
        },
    )


def test_group_rescue_settings_rejects_unavailable_admin_account() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=99, tenant_id=1, display_name="离线账号", phone_masked="99", status=AccountStatus.NEED_RELOGIN.value))
        session.commit()

        payload = TenantGroupRescueSettingsUpdate(
            group_rescue_enabled=True,
            group_rescue_admin_account_id=99,
        )

        with pytest.raises(ValueError, match="救援管理员账号必须是在线账号"):
            update_group_rescue_settings(session, 1, payload, "pytest")


def test_group_rescue_settings_payload_exposes_admin_account() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间", group_rescue_enabled=True, group_rescue_admin_account_id=99))
        session.add(TgAccount(id=99, tenant_id=1, display_name="救援账号", username="helper", phone_masked="99", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.commit()

        payload = group_rescue_settings_payload(session.get(Tenant, 1), session)

        assert payload["group_rescue_enabled"] is True
        assert payload["group_rescue_admin_account"]["id"] == 99
        assert payload["group_rescue_admin_account"]["display_name"] == "救援账号"


def test_group_rescue_admin_account_is_excluded_from_task_account_selection() -> None:
    with _session() as session:
        _seed_rescue_target(session)

        all_accounts = select_task_accounts(session, 1, {"selection_mode": "all", "max_concurrent": 10})
        manual_accounts = select_task_accounts(session, 1, {"selection_mode": "manual", "account_ids": [99, 11], "max_concurrent": 10})

        assert [account.id for account in all_accounts] == [11]
        assert [account.id for account in manual_accounts] == [11]


def test_group_membership_admission_snapshot_excludes_rescue_admin_account() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        for item in session.scalars(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == "task-rescue")):
            session.delete(item)
        session.get(TgAccount, 11).pool_id = 5
        session.get(TgAccount, 99).pool_id = 5
        task = session.get(Task, "task-rescue")
        task.type_config = {"target_operation_target_id": 21, "account_group_ids": [5]}
        session.commit()

        items = lock_membership_admission_snapshot(session, task, now=NOW)

        assert [item.account_id for item in items] == [11]


def test_dispatch_skips_normal_actions_bound_to_rescue_admin_account(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="rescue-admin-send",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={"group_id": 7, "message_text": "不应该发送"},
        )
        session.add(action)
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *_args, **_kwargs: pytest.fail("rescue admin must not send normal messages"))

        assert dispatch_action(session, action) is True

        assert action.status == "skipped"
        assert action.result["error_code"] == "rescue_admin_reserved"


def test_invite_group_account_payload_requires_target_account_ref() -> None:
    with pytest.raises(ValidationError, match="target_account_ref"):
        dispatcher.validate_action_payload(
            "invite_group_account",
            {"group_id": 7, "operation_target_id": 21, "group_peer_id": "-10021", "target_account_id": 11, "target_account_ref": ""},
        )


def test_dispatch_invite_group_account_refreshes_stale_configured_rescue_account(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        session.add(TgAccount(id=100, tenant_id=1, display_name="新救援账号", phone_masked="100", status=AccountStatus.ACTIVE.value, session_ciphertext="session-100"))
        session.get(Tenant, 1).group_rescue_admin_account_id = 100
        action = Action(
            id="invite-account",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@old_user",
                "trigger_account_id": 11,
                "trigger_task_id": "task-rescue",
                "trigger_reason": "permission_denied",
            },
        )
        session.add(action)
        session.commit()

        calls: list[tuple[int, str, str]] = []
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())

        def fake_invite(account_id, group_peer_id, target_account_ref, *_args, **_kwargs):  # noqa: ANN001
            calls.append((account_id, group_peer_id, target_account_ref))
            return OperationResult(True, "已处理", detail="account_invited")

        monkeypatch.setattr(dispatcher.gateway, "invite_account_to_group", fake_invite)

        assert dispatch_action(session, action) is True

        assert calls == [(100, "-10021", "@normal_user")]
        assert action.account_id == 100
        assert action.status == "success"
        assert action.result["rescue_status"] == "invite_success"
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert link is not None
        assert link.can_send is True


def test_dispatch_invite_group_account_joins_with_admin_invite_link_for_non_mutual_contact(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="invite-link-account",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@normal_user",
                "trigger_account_id": 11,
                "trigger_task_id": "task-rescue",
                "trigger_reason": "permission_denied",
            },
        )
        session.add(action)
        session.commit()

        calls: list[tuple[str, int, str]] = []
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "invite_account_to_group",
            lambda *_args, **_kwargs: OperationResult(False, "失败", FailureType.UNKNOWN.value, "The provided user is not a mutual contact"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "export_group_invite_link",
            lambda account_id, group_peer_id, *_args, **_kwargs: calls.append(("export", account_id, group_peer_id)) or InviteLinkResult(True, "已处理", invite_link="https://t.me/+abc"),
        )

        def fake_join(account_id, group_peer_id, *_args, invite_link="", **_kwargs):  # noqa: ANN001
            calls.append(("join", account_id, invite_link))
            return ChannelMembershipResult(True, detail="joined", membership_status="joined")

        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)

        assert dispatch_action(session, action) is True

        assert calls == [("export", 99, "-10021"), ("join", 11, "https://t.me/+abc")]
        assert action.status == "success"
        assert action.result["rescue_status"] == "invite_success"
        assert action.result["rescue_detail"] == "invite_link_joined"
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        assert link is not None
        assert link.permission_label == "群聊救援已入群"


def test_dispatch_invite_group_account_classifies_unusable_invite_link(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="invite-link-expired-account",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@normal_user",
                "trigger_account_id": 11,
                "trigger_task_id": "task-rescue",
                "trigger_reason": "permission_denied",
            },
        )
        session.add(action)
        session.commit()

        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "invite_account_to_group",
            lambda *_args, **_kwargs: OperationResult(False, "失败", FailureType.UNKNOWN.value, "The provided user is not a mutual contact"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "export_group_invite_link",
            lambda *_args, **_kwargs: InviteLinkResult(True, "已处理", invite_link="https://t.me/+abc"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "lift_group_account_restrictions",
            lambda *_args, **_kwargs: OperationResult(True, "已处理", detail="account_restrictions_lifted"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "ensure_channel_membership",
            lambda *_args, **_kwargs: ChannelMembershipResult(
                False,
                "失败",
                FailureType.UNKNOWN.value,
                "The chat the user tried to join has expired and is not valid anymore",
                "failed",
            ),
        )

        assert dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "target_invite_link_unusable"
        assert "疑似账号被群限制" in action.result["rescue_detail"]


def test_dispatch_invite_group_account_lifts_restriction_then_joins(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="invite-link-restricted-account",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@normal_user",
                "trigger_account_id": 11,
                "trigger_task_id": "task-rescue",
                "trigger_reason": "permission_denied",
            },
        )
        session.add(action)
        session.commit()

        calls: list[tuple[str, int | str, str]] = []
        join_results = [
            ChannelMembershipResult(False, "失败", FailureType.UNKNOWN.value, "The chat the user tried to join has expired and is not valid anymore", "failed"),
            ChannelMembershipResult(True, detail="joined", membership_status="joined"),
        ]
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "invite_account_to_group",
            lambda *_args, **_kwargs: OperationResult(False, "失败", FailureType.UNKNOWN.value, "The provided user is not a mutual contact"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "export_group_invite_link",
            lambda *_args, **_kwargs: InviteLinkResult(True, "已处理", invite_link="https://t.me/+fresh"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "lift_group_account_restrictions",
            lambda account_id, group_peer_id, target_ref, *_args, **_kwargs: calls.append(("lift", account_id, target_ref)) or OperationResult(True, "已处理", detail="account_restrictions_lifted"),
        )

        def fake_join(account_id, _group_peer_id, *_args, invite_link="", **_kwargs):  # noqa: ANN001
            calls.append(("join", account_id, invite_link))
            return join_results.pop(0)

        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)

        assert dispatch_action(session, action) is True

        assert calls == [("join", 11, "https://t.me/+fresh"), ("lift", 99, "@normal_user"), ("join", 11, "https://t.me/+fresh")]
        assert action.status == "success"
        assert action.result["rescue_detail"] == "unban_invite_link_joined"


def test_dispatch_membership_waiting_approval_uses_rescue_admin(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = session.get(Task, "task-rescue")
        action = _group_ai_membership_action("membership-waiting-approval", task)
        session.add(action)
        session.commit()

        calls: list[tuple[str, int, str]] = []
        waiting = ChannelMembershipResult(
            False,
            "失败",
            FailureType.GROUP_PERMISSION_DENIED.value,
            "已提交入群申请，等待审批后才能发言",
            "failed",
        )
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: waiting)
        monkeypatch.setattr(dispatcher, "_recover_group_send_permission_with_linked_channel", lambda *_args, **_kwargs: OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, waiting.detail))
        monkeypatch.setattr(
            dispatcher.gateway,
            "approve_group_join_request",
            lambda account_id, group_peer_id, target_ref, *_args, **_kwargs: calls.append(("approve", account_id, target_ref)) or OperationResult(True, "已处理", detail="join_request_approved"),
        )
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, "已处理", detail="可发言"))

        assert dispatch_action(session, action) is True

        assert calls == [("approve", 99, "@normal_user")]
        assert action.status == "success"
        assert action.result["join_request_approved"] is True
        assert action.result["membership_status"] == "joined"


def test_dispatch_membership_private_group_lifts_restriction_and_joins(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = session.get(Task, "task-rescue")
        action = _group_ai_membership_action("membership-private-banned", task)
        session.add(action)
        session.commit()

        calls: list[tuple[str, int, str]] = []
        private = ChannelMembershipResult(
            False,
            "失败",
            FailureType.UNKNOWN.value,
            "The channel specified is private and you lack permission to access it. Another reason may be that you were banned from it (caused by GetChannelsRequest)",
            "failed",
        )
        joined = ChannelMembershipResult(True, "已处理", "", "joined", "joined")
        join_results = [private, joined]
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "lift_group_account_restrictions",
            lambda account_id, group_peer_id, target_ref, *_args, **_kwargs: calls.append(("lift", account_id, target_ref)) or OperationResult(True, "已处理", detail="account_restrictions_lifted"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "export_group_invite_link",
            lambda *_args, **_kwargs: InviteLinkResult(True, detail="https://t.me/+fresh", invite_link="https://t.me/+fresh"),
        )

        def fake_join(account_id, group_peer_id, *_args, invite_link=None, **_kwargs):  # noqa: ANN001
            calls.append(("join", account_id, invite_link or group_peer_id))
            return join_results.pop(0)

        monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)
        monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, "已处理", detail="可发言"))

        assert dispatch_action(session, action) is True

        assert calls == [("join", 11, "-10021"), ("lift", 99, "@normal_user"), ("join", 11, "https://t.me/+fresh")]
        assert action.status == "success"
        assert action.result["admin_restriction_lifted"] is True
        assert action.result["membership_status"] == "joined"


def test_dispatch_deprecated_group_rescue_action_migrates_to_account_invite(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="old-invite",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_bot",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "bot_username": "@old_guard",
                "trigger_account_id": 11,
                "trigger_task_id": "task-rescue",
                "trigger_reason": "permission_denied",
            },
        )
        session.add(action)
        session.commit()

        calls: list[tuple[int, str, str]] = []
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())

        def fake_invite(account_id, group_peer_id, target_account_ref, *_args, **_kwargs):  # noqa: ANN001
            calls.append((account_id, group_peer_id, target_account_ref))
            return OperationResult(True, "已处理", detail="account_invited")

        monkeypatch.setattr(dispatcher.gateway, "invite_account_to_group", fake_invite)

        assert dispatch_action(session, action) is True

        assert action.action_type == "invite_group_account"
        assert action.payload["target_account_ref"] == "@normal_user"
        assert calls == [(99, "-10021", "@normal_user")]
        assert action.status == "success"
        assert "bot" not in str(action.result).lower()


def test_dispatch_deprecated_group_rescue_without_trigger_account_fails_cleanly() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        action = Action(
            id="old-invite-missing-trigger",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_bot",
            account_id=99,
            scheduled_at=NOW,
            status="pending",
            payload={"group_id": 7, "operation_target_id": 21, "group_peer_id": "-10021", "bot_username": "@old_guard"},
        )
        session.add(action)
        session.commit()

        assert dispatch_action(session, action) is True

        assert action.status == "failed"
        assert action.result["error_message"] == "旧群聊救援动作缺少触发账号，无法迁移为账号邀请救援"
        assert "机器人" not in str(action.result)


def test_membership_permission_denied_over_threshold_creates_one_rescue_action() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = session.get(Task, "task-rescue")
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id))

        for index in range(4):
            action = Action(
                id=f"membership-denied-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="ensure_target_membership",
                account_id=11,
                scheduled_at=NOW,
                status="skipped",
                result={"membership_status": "permission_denied", "error_message": "群黑名单，无法发言"},
            )
            session.add(action)
            item.membership_action_id = action.id
            item.phase = "joining"
            session.commit()
            sync_membership_admission_items(session, task, now=NOW)

        rescue_actions = session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all()
        session.refresh(item)

        assert len(rescue_actions) == 1
        assert rescue_actions[0].account_id == 99
        assert rescue_actions[0].payload["target_account_id"] == 11
        assert rescue_actions[0].payload["target_account_ref"] == "@normal_user"
        assert item.permission_failure_count == 4
        assert item.rescue_action_id == rescue_actions[0].id
        assert item.rescue_status == "pending"


def test_group_ai_membership_permission_denied_creates_rescue_action(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = Task(id="task-ai-membership-rescue", tenant_id=1, name="AI 群聊", type="group_ai_chat", status="running")
        action = _group_ai_membership_action("ai-membership-denied", task)
        session.add(task)
        session.add(action)
        session.commit()
        _patch_group_membership_denied(monkeypatch)

        assert dispatch_action(session, action) is True

        rescue_actions = session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all()
        assert action.status == "skipped"
        assert action.result["group_rescue_status"] == "pending"
        assert len(rescue_actions) == 1
        assert rescue_actions[0].account_id == 99
        assert rescue_actions[0].payload["target_account_id"] == 11
        assert rescue_actions[0].payload["target_account_ref"] == "@normal_user"
        assert rescue_actions[0].payload["trigger_reason"] == "群无权限或账号不可发言"


def test_group_ai_membership_permission_denied_refreshes_stale_failed_rescue_action(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        session.add(TgAccount(id=100, tenant_id=1, display_name="新救援账号", phone_masked="100", status=AccountStatus.ACTIVE.value, session_ciphertext="session-100"))
        tenant = session.get(Tenant, 1)
        tenant.group_rescue_admin_account_id = 100
        task = Task(id="task-ai-refresh-rescue", tenant_id=1, name="AI 群聊", type="group_ai_chat", status="running")
        action = _group_ai_membership_action("ai-membership-denied-refresh", task)
        old_rescue = Action(
            id="old-ai-rescue",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW - timedelta(minutes=10),
            status="failed",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@old_user",
                "trigger_account_id": 11,
            },
            result={"rescue_status": "invite_failed", "error_message": "旧救援账号无权限"},
        )
        session.add_all([task, old_rescue, action])
        session.commit()
        _patch_group_membership_denied(monkeypatch)

        assert dispatch_action(session, action) is True

        rescue_actions = session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all()
        session.refresh(old_rescue)
        assert len(rescue_actions) == 1
        assert old_rescue.status == "pending"
        assert old_rescue.account_id == 100
        assert old_rescue.payload["target_account_ref"] == "@normal_user"
        assert old_rescue.result["rescue_status"] == "pending"
        assert action.result["group_rescue_status"] == "pending"
        assert action.result["group_rescue_action_id"] == "old-ai-rescue"


def test_group_ai_membership_permission_denied_refreshes_legacy_non_mutual_rescue_action(monkeypatch) -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = Task(id="task-ai-refresh-non-mutual", tenant_id=1, name="AI 群聊", type="group_ai_chat", status="running")
        action = _group_ai_membership_action("ai-membership-denied-non-mutual", task)
        old_rescue = Action(
            id="old-non-mutual-rescue",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW - timedelta(minutes=10),
            status="failed",
            payload={
                "group_id": 7,
                "operation_target_id": 21,
                "group_peer_id": "-10021",
                "target_account_id": 11,
                "target_account_ref": "@normal_user",
                "trigger_account_id": 11,
            },
            result={"rescue_status": "invite_failed", "error_message": "The provided user is not a mutual contact"},
        )
        session.add_all([task, old_rescue, action])
        session.commit()
        _patch_group_membership_denied(monkeypatch)

        assert dispatch_action(session, action) is True

        session.commit()
        session.refresh(old_rescue)
        assert old_rescue.status == "pending"
        assert old_rescue.account_id == 99
        assert old_rescue.payload["target_account_ref"] == "@normal_user"
        assert old_rescue.result["rescue_status"] == "pending"
        assert action.result["group_rescue_action_id"] == "old-non-mutual-rescue"


def test_membership_sync_counts_same_permission_action_once() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        task = session.get(Task, "task-rescue")
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id))
        action = Action(
            id="membership-denied-once",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=11,
            scheduled_at=NOW,
            status="skipped",
            result={"membership_status": "permission_denied", "error_message": "群黑名单，无法发言"},
        )
        session.add(action)
        item.membership_action_id = action.id
        item.phase = "joining"
        session.commit()

        for _ in range(4):
            sync_membership_admission_items(session, task, now=NOW)
            session.commit()

        session.refresh(item)
        rescue_actions = session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all()
        assert item.permission_failure_count == 1
        assert rescue_actions == []


def test_membership_rescue_missing_config_is_explicit_without_action() -> None:
    with _session() as session:
        _seed_rescue_target(session, configured=False)
        task = session.get(Task, "task-rescue")
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == task.id))

        for index in range(4):
            action = Action(
                id=f"membership-denied-missing-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="ensure_target_membership",
                account_id=11,
                scheduled_at=NOW,
                status="skipped",
                result={"membership_status": "permission_denied", "error_message": "群黑名单，无法发言"},
            )
            session.add(action)
            item.membership_action_id = action.id
            item.phase = "joining"
            session.commit()
            sync_membership_admission_items(session, task, now=NOW)

        session.refresh(item)

        assert session.scalars(select(Action).where(Action.action_type == "invite_group_account")).all() == []
        assert item.permission_failure_count == 4
        assert item.rescue_status == "unconfigured"
        assert "救援配置缺失" in item.rescue_failure_detail


def test_group_ai_permission_failure_count_uses_latest_consecutive_streak() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-ai-rescue", tenant_id=1, name="ai rescue", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="普通账号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session-11"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群"))
        for index in range(3):
            session.add(
                Action(
                    id=f"old-denied-{index}",
                    tenant_id=1,
                    task_id="task-ai-rescue",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=11,
                    scheduled_at=NOW - timedelta(minutes=10 + index),
                    executed_at=NOW - timedelta(minutes=10 + index),
                    status="failed",
                    payload={"group_id": 7, "message_text": "old"},
                    result={"error_code": FailureType.GROUP_PERMISSION_DENIED.value, "error_message": "群无权限"},
                )
            )
        session.add(
            Action(
                id="success-breaks-streak",
                tenant_id=1,
                task_id="task-ai-rescue",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                scheduled_at=NOW - timedelta(minutes=1),
                executed_at=NOW - timedelta(minutes=1),
                status="success",
                payload={"group_id": 7, "message_text": "ok"},
                result={"success": True},
            )
        )
        current = Action(
            id="current-denied",
            tenant_id=1,
            task_id="task-ai-rescue",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            scheduled_at=NOW,
            executed_at=NOW,
            status="failed",
            payload={"group_id": 7, "message_text": "current"},
            result={"error_code": FailureType.GROUP_PERMISSION_DENIED.value, "error_message": "群无权限"},
        )
        session.add(current)
        session.commit()

        assert permission_failure_count_for_send_action(session, current) == 1


def test_retry_membership_rescue_requeues_failed_rescue_action() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == "task-rescue"))
        action = Action(
            id="failed-rescue",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="failed",
            payload={"group_id": 7, "operation_target_id": 21, "group_peer_id": "-10021", "target_account_id": 11, "target_account_ref": "@normal_user"},
            result={"rescue_status": "invite_failed", "error_message": "救援账号不是目标群管理员或没有邀请权限"},
        )
        session.add(action)
        item.rescue_action_id = action.id
        item.rescue_status = "invite_failed"
        item.rescue_failure_detail = "救援账号不是目标群管理员或没有邀请权限"
        session.commit()

        retry_membership_admission_rescue(session, 1, "task-rescue", item.id)

        session.refresh(action)
        session.refresh(item)
        assert action.status == "pending"
        assert action.result["rescue_status"] == "pending"
        assert item.rescue_status == "pending"
        assert item.rescue_failure_detail == ""


def test_retry_membership_rescue_uses_latest_global_config() -> None:
    with _session() as session:
        _seed_rescue_target(session)
        session.add(TgAccount(id=100, tenant_id=1, display_name="新救援账号", phone_masked="100", status=AccountStatus.ACTIVE.value, session_ciphertext="session-100"))
        item = session.scalar(select(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.task_id == "task-rescue"))
        action = Action(
            id="old-failed-rescue",
            tenant_id=1,
            task_id="task-rescue",
            task_type="group_membership_admission",
            action_type="invite_group_account",
            account_id=99,
            scheduled_at=NOW,
            status="failed",
            payload={"group_id": 7, "operation_target_id": 21, "group_peer_id": "-10021", "target_account_id": 11, "target_account_ref": "@old_user", "trigger_account_id": 11},
            result={"rescue_status": "invite_failed", "error_message": "旧配置失败"},
        )
        session.add(action)
        item.rescue_action_id = action.id
        item.rescue_status = "invite_failed"
        tenant = session.get(Tenant, 1)
        tenant.group_rescue_admin_account_id = 100
        session.commit()

        retry_membership_admission_rescue(session, 1, "task-rescue", item.id)

        session.refresh(action)
        assert action.status == "pending"
        assert action.account_id == 100
        assert action.payload["target_account_ref"] == "@normal_user"
        assert action.payload["trigger_account_id"] == 11


def test_invite_account_to_group_reports_unresolvable_account(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_entity(self, username: str):  # noqa: ANN001
            assert username == "missing_user"
            raise ValueError("Could not find the input entity")

        async def __call__(self, _request):  # noqa: ANN001
            return object()

    async def fake_client(_credentials, _raw_session):  # noqa: ANN001
        return FakeClient()

    async def fake_target(_client, _peer_id, *, group_id=0):  # noqa: ANN001
        return SimpleNamespace(id=7)

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_target)

    result = gateway._run(
        gateway._invite_account_to_group_async(
            "raw-session",
            "-1007",
            "@missing_user",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is False
    assert result.detail == "被救援账号无法解析或目标群不可访问"


def test_export_group_invite_link_creates_rescue_titled_link(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()
    seen_requests: list[object] = []

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_entity(self, peer_id: int):  # noqa: ANN001
            assert peer_id == -1007
            return SimpleNamespace(id=7)

        async def __call__(self, request):  # noqa: ANN001
            seen_requests.append(request)
            return SimpleNamespace(link="https://t.me/+freshInvite")

    async def fake_client(_credentials, _raw_session):  # noqa: ANN001
        return FakeClient()

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)

    result = gateway._run(
        gateway._export_group_invite_link_async(
            515,
            "raw-session",
            "-1007",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is True
    assert result.invite_link == "https://t.me/+freshInvite"
    assert getattr(seen_requests[0], "title") == "tg-yunying-rescue-515"


def test_lift_group_account_restrictions_uses_admin_edit_banned_request(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()
    seen_requests: list[object] = []

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_entity(self, username: str):  # noqa: ANN001
            assert username == "normal_user"
            return SimpleNamespace(id=11)

        async def __call__(self, request):  # noqa: ANN001
            seen_requests.append(request)
            return object()

    async def fake_client(_credentials, _raw_session):  # noqa: ANN001
        return FakeClient()

    async def fake_target(_client, _peer_id, *, group_id=0):  # noqa: ANN001
        return SimpleNamespace(id=7)

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_target)

    result = gateway._run(
        gateway._lift_group_account_restrictions_async(
            "raw-session",
            "-1007",
            "@normal_user",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is True
    assert seen_requests
    assert seen_requests[0].banned_rights.view_messages is False
    assert seen_requests[0].banned_rights.send_messages is False


def test_approve_group_join_request_uses_approved_join_request(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()
    seen_requests: list[object] = []

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_entity(self, username: str):  # noqa: ANN001
            assert username == "normal_user"
            return SimpleNamespace(id=11)

        async def __call__(self, request):  # noqa: ANN001
            seen_requests.append(request)
            return object()

    async def fake_client(_credentials, _raw_session):  # noqa: ANN001
        return FakeClient()

    async def fake_target(_client, _peer_id, *, group_id=0):  # noqa: ANN001
        return SimpleNamespace(id=7)

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_target)

    result = gateway._run(
        gateway._approve_group_join_request_async(
            "raw-session",
            "-1007",
            "@normal_user",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is True
    assert seen_requests
    assert seen_requests[0].approved is True
