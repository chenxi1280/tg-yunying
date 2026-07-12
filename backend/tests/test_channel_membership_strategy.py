from __future__ import annotations

from datetime import timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult
from app.integrations.telegram.gateway import VERIFICATION_CONTEXT_DEFAULT_LIMIT
from app.models import AccountStatus, Action, AiProvider, AiProviderHealthStatus, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now
from app.services.membership_challenges import _image_verification_provider
from app.services.task_center import dispatcher
from app.services.task_center.channel_membership import (
    _create_membership_actions_for_accounts,
    _reactivate_auto_verification_memberships,
    channel_membership_summary,
    gate_channel_membership,
)
from app.services.task_center.membership_recovery import AUTO_RETRY_BUCKET, classify_membership_recovery
from app.services.task_center.payloads import EnsureChannelMembershipPayload
from app.services.task_center.targets import group_from_reference


def test_group_ai_membership_actions_default_to_four_hour_window(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.pacing.random.randint", lambda lo, hi: hi)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=920, tenant_id=1, target_type="group", tg_peer_id="-100920", title="四小时准入群", auth_status="只读", can_send=False))
        for account_id in range(1, 41):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext="session"))
        task = Task(
            id="task-four-hour-membership",
            tenant_id=1,
            name="四小时准入",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 920},
        )
        session.add(task)
        session.commit()

        result = gate_channel_membership(session, task, session.get(OperationTarget, 920), require_send=True)
        rows = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.scheduled_at.asc()).all()

    assert result.created == 40
    assert len(rows) == 40
    assert rows[-1].scheduled_at - rows[0].scheduled_at <= timedelta(hours=4)
    assert task.stats["membership_schedule_window_hours"] == 4


def test_group_ai_membership_strategy_can_disable_auto_join_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=901, tenant_id=1, target_type="group", tg_peer_id="-100901", title="准入群", auth_status="已授权运营", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status="在线", session_ciphertext="session"))
        task = Task(
            id="task-membership-off",
            tenant_id=1,
            name="关闭自动准入",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={"target_operation_target_id": 901, "auto_join_target": False},
        )
        session.add(task)
        session.commit()

        result = gate_channel_membership(session, task, session.get(OperationTarget, 901), require_send=True)
        action_count = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").count()

    assert result.blocked is True
    assert result.created == 0
    assert action_count == 0
    assert task.last_error == "准入策略已关闭自动入群"


def test_group_ai_membership_strategy_disables_auto_follow_and_verification_helpers() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-verification-off",
            tenant_id=1,
            name="关闭自动验证",
            type="group_ai_chat",
            status="running",
            type_config={"auto_follow_required_channel": False, "auto_resolve_verification": False},
        )
        session.add(task)
        action = Action(
            id="membership-verification-off",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=11,
            status="pending",
        )
        session.add(action)
        session.commit()

        assert dispatcher._auto_follow_required_channel_enabled(session, action) is False
        assert dispatcher._auto_verification_enabled(session, action) is False


@pytest.mark.no_postgres
def test_image_verification_provider_accepts_minimax_when_mimo_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com",
                model_name="deepseek-v4-flash",
                api_key_ciphertext="cipher",
                health_status=AiProviderHealthStatus.HEALTHY.value,
                is_active=True,
            )
        )
        session.add(
            AiProvider(
                id=2,
                provider_name="MiniMax",
                provider_type="openai_compatible",
                base_url="https://api.minimax.io/v1",
                model_name="MiniMax-M3",
                api_key_ciphertext="cipher",
                health_status=AiProviderHealthStatus.HEALTHY.value,
                is_active=True,
            )
        )
        session.commit()
        provider = _image_verification_provider(session)

        assert provider is not None
        assert provider.provider_name == "MiniMax"


def test_reactivate_memberships_requeues_recoverable_failures_for_group_ai() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    old_value = now_value - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        channel = OperationTarget(id=901, tenant_id=1, target_type="group", tg_peer_id="-100901", title="准入恢复群", auth_status="只读", can_send=False)
        group = TgGroup(id=801, tenant_id=1, tg_peer_id="-100901", title="准入恢复群", auth_status="只读", can_send=False)
        task = Task(id="task-recovery-reactivate", tenant_id=1, name="恢复重排", type="group_ai_chat", status="running")
        account = TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        action = Action(
            id="membership-required-channel-failed",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=11,
            status="skipped",
            scheduled_at=old_value,
            executed_at=old_value,
            payload={"channel_id": "-100901", "channel_target_id": 901, "target_type": "group", "target_display": "准入恢复群", "require_send": True},
            result={"membership_status": "permission_denied", "error_message": "需要关注我们的频道才能发言"},
        )
        session.add_all([channel, group, task, account, action])
        session.commit()

        created = _reactivate_auto_verification_memberships(session, task, channel, [account], require_send=True)
        retry_count = session.query(Action).filter(Action.task_id == task.id, Action.status == "pending").count()

    assert created == 1
    assert retry_count == 1


def test_reactivate_memberships_does_not_retry_account_unavailable() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    old_value = _now() - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        channel = OperationTarget(id=902, tenant_id=1, target_type="group", tg_peer_id="-100902", title="冻结群", auth_status="只读", can_send=False)
        group = TgGroup(id=802, tenant_id=1, tg_peer_id="-100902", title="冻结群", auth_status="只读", can_send=False)
        task = Task(id="task-no-retry-frozen", tenant_id=1, name="冻结不重排", type="group_ai_chat", status="running")
        account = TgAccount(id=12, tenant_id=1, display_name="账号12", phone_masked="12", status=AccountStatus.SUSPECTED_BANNED.value, session_ciphertext="session")
        action = Action(
            id="membership-frozen-failed",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=12,
            status="failed",
            scheduled_at=old_value,
            executed_at=old_value,
            payload={"channel_id": "-100902", "channel_target_id": 902, "target_type": "group", "target_display": "冻结群", "require_send": True},
            result={"error_code": "账号不可用", "error_message": "method is not available for frozen accounts"},
        )
        session.add_all([channel, group, task, account, action])
        session.commit()

        created = _reactivate_auto_verification_memberships(session, task, channel, [account], require_send=True)
        retry_count = session.query(Action).filter(Action.task_id == task.id, Action.status == "pending").count()

    assert created == 0
    assert retry_count == 0


def test_hard_hourly_reactivation_repairs_auto_verification_failures_when_capacity_is_ready() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    old_value = _now() - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        channel = OperationTarget(id=904, tenant_id=1, target_type="group", tg_peer_id="-100904", title="天津", auth_status="已授权运营", can_send=True)
        group = TgGroup(id=804, tenant_id=1, tg_peer_id="-100904", title="天津", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-capacity-ready",
            tenant_id=1,
            name="天津",
            type="group_ai_chat",
            status="running",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 1},
        )
        ready = TgAccount(id=21, tenant_id=1, display_name="可发言", phone_masked="21", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        failed = TgAccount(id=22, tenant_id=1, display_name="失败", phone_masked="22", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        action = Action(
            id="membership-permission-denied",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=failed.id,
            status="skipped",
            scheduled_at=old_value,
            executed_at=old_value,
            payload={"channel_id": "-100904", "channel_target_id": channel.id, "target_type": "group", "target_display": "天津", "require_send": True},
            result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
        )
        verification = VerificationTask(
            id=9040,
            tenant_id=1,
            account_id=failed.id,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="群无权限或账号不可发言",
            suggested_action="识别图形验证码",
            status="失败",
            handled_at=old_value,
        )
        session.add_all([channel, group, task, ready, failed, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=ready.id, can_send=True), action, verification])
        session.commit()

        created = _reactivate_auto_verification_memberships(session, task, channel, [ready, failed], require_send=True)
        retry_count = session.query(Action).filter(Action.task_id == task.id, Action.status == "pending").count()

    assert created == 1
    assert retry_count == 1


@pytest.mark.no_postgres
def test_reactivate_memberships_waits_for_target_reference_change() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    old_value = _now() - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        channel = OperationTarget(
            id=903,
            tenant_id=1,
            target_type="group",
            tg_peer_id="qdsfxy",
            username="qdsfxy",
            title="青岛师范学院",
            auth_status="只读",
            can_send=False,
        )
        group = TgGroup(id=803, tenant_id=1, tg_peer_id="qdsfxy", title="青岛师范学院", auth_status="只读", can_send=False)
        task = Task(
            id="task-stale-target-ref",
            tenant_id=1,
            name="青岛师范学院",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_operation_target_id": channel.id,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
            },
        )
        account = TgAccount(id=13, tenant_id=1, display_name="账号13", phone_masked="13", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        action = Action(
            id="membership-stale-target-ref",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=13,
            status="failed",
            scheduled_at=old_value,
            executed_at=old_value,
            payload={"channel_id": "qdsfxy", "channel_target_id": 903, "target_type": "group", "target_display": "青岛师范学院", "target_username": "qdsfxy", "require_send": True},
            result={"error_code": "未知错误", "error_message": 'No user has "qdsfxy" as username'},
        )
        verification = VerificationTask(
            id=7003,
            tenant_id=1,
            account_id=account.id,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason='No user has "qdsfxy" as username',
            suggested_action="识别图形验证码",
            status="失败",
            handled_at=old_value,
        )
        session.add_all([channel, group, task, account, action, verification])
        session.commit()

        unchanged_created = _reactivate_auto_verification_memberships(session, task, channel, [account], require_send=True)
        channel.username = "https://t.me/+replacement"
        session.flush()
        changed_created = _reactivate_auto_verification_memberships(session, task, channel, [account], require_send=True)
        retry = session.scalar(select(Action).where(Action.task_id == task.id, Action.status == "pending"))

    assert unchanged_created == 0
    assert changed_created == 1
    assert retry is not None
    assert retry.result["reactivated_reason"] == "hard_hourly_target_ref_retry"
    assert retry.payload["target_username"] == "https://t.me/+replacement"
    assert retry.payload["invite_link"] == "https://t.me/+replacement"


@pytest.mark.no_postgres
def test_reactivate_memberships_accepts_legacy_payload_without_channel_id() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        channel = OperationTarget(
            id=904,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100904",
            title="历史准入群",
            auth_status="已授权运营",
            can_send=True,
        )
        group = TgGroup(id=804, tenant_id=1, tg_peer_id="-100904", title="历史准入群")
        task = Task(id="task-legacy-membership", tenant_id=1, name="历史准入", type="group_ai_chat", status="running")
        account = TgAccount(
            id=14,
            tenant_id=1,
            display_name="账号14",
            phone_masked="14",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
        action = Action(
            id="membership-legacy-payload",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="skipped",
            payload={"channel_target_id": channel.id},
            result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
        )
        session.add_all([channel, group, task, account, action])
        session.commit()

        created = _reactivate_auto_verification_memberships(session, task, channel, [account], require_send=True)

    assert created == 0


def test_group_send_verification_classifies_arithmetic_captcha_as_reply() -> None:
    assert dispatcher._group_send_verification_action("请输入 3 + 5 的结果后才能发言") == "发送验证回复"
    assert dispatcher._group_send_verification_action("加减验证码：9-4=?") == "发送验证回复"
    assert dispatcher._group_send_verification_action("请先关注 @alpha @beta 后输入 3+5") == "发送验证回复"


def test_group_send_verification_prioritizes_button_channel_follow() -> None:
    detail = "群无权限或账号不可发言：学院助手：您需要关注我们的频道才能发言。 [按钮：天津音乐学院车库备用 (https://t.me/qiyue201)]"

    assert dispatcher._group_send_verification_action(detail) == "关注频道"
    assert dispatcher._group_send_verification_action("群无权限或账号不可发言：需要点击按钮完成验证") == "点击按钮"
    assert dispatcher._group_send_verification_action("群无权限或账号不可发言") == "识别图形验证码"


def test_input_peer_user_cast_error_is_retryable_peer_ref_failure() -> None:
    result = OperationResult(False, "失败", "未知错误", "Cannot cast InputPeerUser to any kind of InputChannel.")

    assert dispatcher._membership_peer_ref_invalid(result) is True


def test_input_peer_user_cast_error_is_membership_target_ref_retry() -> None:
    recovery = classify_membership_recovery(
        phase="failed",
        account_status=AccountStatus.ACTIVE.value,
        action_status="failed",
        failure_type="未知错误",
        failure_detail="Cannot cast InputPeerUser to any kind of InputChannel.",
        verification_action="",
        verification_status="",
        can_auto_resolve=False,
    )

    assert recovery.bucket == AUTO_RETRY_BUCKET
    assert recovery.auto_retryable is True


def test_group_send_permission_follows_multiple_required_channels(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    probes: list[str] = []

    def fake_follow(_account_id, channel_peer_id, *_args, **_kwargs):
        followed.append(channel_peer_id)
        return OperationResult(True, "已处理", detail=f"followed:{channel_peer_id}")

    def fake_probe(_account_id, target_peer_id, _target_type, *_args, **_kwargs):
        probes.append(target_peer_id)
        return OperationResult(True, detail="可发言")

    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_follow)
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", fake_probe)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-multi-follow", tenant_id=1, name="多频道准入", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True}))
        action = Action(id="membership-multi-follow", tenant_id=1, task_id="task-multi-follow", task_type="group_ai_chat", action_type="ensure_target_membership", account_id=11)
        account = TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status="在线", session_ciphertext="session")
        session.add_all([action, account])
        session.commit()

        result = dispatcher._recover_group_send_permission_with_linked_channel(
            session,
            action,
            account,
            object(),
            EnsureChannelMembershipPayload(channel_id="-100999", channel_target_id=999, target_type="group", target_display="目标群", require_send=True),
            OperationResult(False, "失败", "group_permission_denied", "需要先关注 @alpha、https://t.me/beta_channel 和 t.me/+InviteHash123 才能发言"),
        )

    assert result.ok is True
    assert followed == ["alpha", "beta_channel", "https://t.me/+InviteHash123"]
    assert probes == ["-100999"]


def test_group_send_permission_follows_tianjin_common_group_links(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    probes: list[str] = []

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.ensure_channel_membership", lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已加入前置群"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda _account_id, target_peer_id, *_args, **_kwargs: probes.append(target_peer_id) or OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Task(id="task-common-groups", tenant_id=1, name="共同群准入", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True}))
        action = Action(id="membership-common-groups", tenant_id=1, task_id="task-common-groups", task_type="group_ai_chat", action_type="ensure_target_membership", account_id=11)
        account = TgAccount(id=11, tenant_id=1, display_name="账号11", phone_masked="11", status="在线", session_ciphertext="session")
        session.add_all([Tenant(id=1, name="默认运营空间"), action, account])
        session.commit()

        result = dispatcher._recover_group_send_permission_with_linked_channel(
            session,
            action,
            account,
            object(),
            EnsureChannelMembershipPayload(channel_id="-1003583171851", channel_target_id=485, target_type="group", target_display="天津音乐学院", require_send=True),
            OperationResult(False, "失败", "group_permission_denied", "被邀请人须拥有2个及以上天津的共同✈️群 [按钮：学院工兵群200出击 (https://t.me/zztjxygbq) / 学院优质出击报告 (https://t.me/ttyyxybg)]"),
        )

    assert result.ok is True
    assert followed == ["zztjxygbq", "ttyyxybg"]
    assert probes == ["-1003583171851"]


def test_auto_follow_verification_uses_explicit_required_channel_links(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    probes: list[str] = []

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.ensure_channel_membership", lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已关注"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda _account_id, target_peer_id, *_args, **_kwargs: probes.append(target_peer_id) or OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=812, tenant_id=1, tg_peer_id="-100812", title="关注入口群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=912, tenant_id=1, target_type="group", tg_peer_id="-100812", title="关注入口群", auth_status="只读", can_send=False)
        task = Task(id="task-follow-action-verify", tenant_id=1, name="关注频道验证", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True})
        account = TgAccount(id=42, tenant_id=1, display_name="账号42", phone_masked="42", status="在线", session_ciphertext="session")
        action = Action(id="membership-follow-action-verify", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=42)
        verification = VerificationTask(
            tenant_id=1,
            account_id=42,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="您需要关注我们的频道才能发言。 [按钮：备用频道 (https://t.me/qiyue201)]",
            suggested_action="关注频道",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
            failure_detail="还需要关注 @second_channel",
        )
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(session, action, account, object(), EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True), None),
            verification,
        )

    assert result.ok is True
    assert followed == ["second_channel", "qiyue201", "-100812"]
    assert probes == ["-100812"]
    assert verification.status == "已处理"
    assert action.result["target_membership_retried_after_required_channel"] is True


def test_auto_follow_verification_reads_button_links_from_context(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []

    monkeypatch.setattr(
        "app.services.membership_challenges.gateway.fetch_verification_context",
        lambda *_args, **_kwargs: [{"message_id": 5, "sender": "学院助手", "text": "您需要关注我们的频道才能发言。 [按钮：天津音乐学院车库备用 (https://t.me/qiyue201)]", "sent_at": None}],
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已关注"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_linked_channel_membership",
        lambda *_args, **_kwargs: OperationResult(False, "失败", "linked_channel_missing", "未解析到群关联频道"),
        raising=False,
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=813, tenant_id=1, tg_peer_id="-100813", title="按钮关注群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=913, tenant_id=1, target_type="group", tg_peer_id="-100813", title="按钮关注群", auth_status="只读", can_send=False)
        task = Task(id="task-follow-context", tenant_id=1, name="关注上下文", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True})
        account = TgAccount(id=43, tenant_id=1, display_name="账号43", phone_masked="43", status="在线", session_ciphertext="session")
        action = Action(id="membership-follow-context", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=43)
        verification = VerificationTask(tenant_id=1, account_id=43, group_id=group.id, verification_type="群发言权限", detected_reason="您需要关注我们的频道才能发言", suggested_action="关注频道", target_peer_id=group.tg_peer_id, target_display=group.title, status="待处理")
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(session, action, account, object(), EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True), None),
            verification,
        )

    assert result.ok is True
    assert followed == ["qiyue201", "-100813"]
    assert verification.status == "已处理"
    assert action.result["target_membership_retried_after_required_channel"] is True


def test_auto_follow_verification_uses_action_error_button_links_before_linked_channel(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    linked_attempts: list[str] = []

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已关注"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_linked_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: linked_attempts.append(channel_ref) or OperationResult(False, "失败", "linked_channel_wrong_type", "Cannot cast InputPeerUser to any kind of InputChannel."),
        raising=False,
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=814, tenant_id=1, tg_peer_id="-100814", title="天津音乐学院", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=914, tenant_id=1, target_type="group", tg_peer_id="-100814", title="天津音乐学院", auth_status="只读", can_send=False)
        task = Task(id="task-follow-action-error", tenant_id=1, name="关注动作错误", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True})
        account = TgAccount(id=44, tenant_id=1, display_name="账号44", phone_masked="44", status="在线", session_ciphertext="session")
        action = Action(
            id="membership-follow-action-error",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=44,
            result={
                "error_message": "您需要关注我们的频道才能发言。 [按钮：天津音乐学院车库备用 (https://t.me/qiyue201)]",
            },
        )
        verification = VerificationTask(
            tenant_id=1,
            account_id=44,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="群无权限或账号不可发言",
            suggested_action="关注频道",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(
                session,
                action,
                account,
                object(),
                EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True),
                None,
            ),
            verification,
        )

    assert result.ok is True
    assert followed == ["qiyue201", "-100814"]
    assert linked_attempts == []
    assert verification.status == "已处理"
    assert action.result["target_membership_retried_after_required_channel"] is True


def test_button_verification_with_tme_links_auto_follows_links_not_ad_mentions(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已加入"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=815, tenant_id=1, tg_peer_id="-100815", title="天津音乐学院", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=915, tenant_id=1, target_type="group", tg_peer_id="-100815", title="天津音乐学院", auth_status="只读", can_send=False)
        task = Task(id="task-button-link-follow", tenant_id=1, name="按钮链接关注", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True})
        account = TgAccount(id=45, tenant_id=1, display_name="账号45", phone_masked="45", status="在线", session_ciphertext="session")
        action = Action(id="membership-button-link-follow", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=45)
        verification = VerificationTask(
            tenant_id=1,
            account_id=45,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="群无权限或账号不可发言：广告 @xiaodongli0733 [按钮：车库 (https://t.me/qiyue201) / 报告频道 (https://t.me/ttyyxybg)]",
            suggested_action="点击按钮",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(
                session,
                action,
                account,
                object(),
                EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True),
                None,
            ),
            verification,
        )

    assert result.ok is True
    assert followed == ["qiyue201", "ttyyxybg", "-100815"]
    assert verification.suggested_action == "关注频道"
    assert verification.status == "已处理"


@pytest.mark.no_postgres
def test_auto_follow_verification_clicks_confirmation_button_after_required_channels(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []
    resolved: list[str] = []

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.ensure_channel_membership",
        lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已加入"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.resolve_verification_task",
        lambda _account_id, action, *_args, **_kwargs: resolved.append(action) or OperationResult(True, "已处理", detail="已点击确认按钮"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=816, tenant_id=1, tg_peer_id="-100816", title="郑州楼凤阁", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=916, tenant_id=1, target_type="group", tg_peer_id="-100816", title="郑州楼凤阁", auth_status="只读", can_send=False)
        task = Task(id="task-required-channel-confirm", tenant_id=1, name="关注后确认", type="group_ai_chat", status="running", type_config={"auto_follow_required_channel": True})
        account = TgAccount(id=46, tenant_id=1, display_name="账号46", phone_masked="46", status="在线", session_ciphertext="session")
        action = Action(id="membership-required-channel-confirm", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=46)
        verification = VerificationTask(
            tenant_id=1,
            account_id=46,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="山竹开小差，您需要关注我们的频道才能发言。 [按钮：郑州楼凤阁车库 (https://t.me/zz_lfg_garage) / 郑州楼凤报告收录 (https://t.me/zz_lfg_report) / ✅ 我已加入]",
            suggested_action="点击按钮",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(
                session,
                action,
                account,
                object(),
                EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True),
                None,
            ),
            verification,
        )

    assert result.ok is True
    assert followed == ["zz_lfg_garage", "zz_lfg_report", "-100816"]
    assert resolved == ["点击按钮"]
    assert verification.suggested_action == "关注频道"
    assert verification.status == "已处理"


def test_required_channel_follow_skips_invalid_mentions_and_continues(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []

    def fake_follow(_account_id, channel_ref, *_args, **_kwargs):
        if channel_ref == "ad_user":
            return OperationResult(False, "失败", "目标无效", "Cannot cast InputPeerUser to any kind of InputChannel.")
        followed.append(channel_ref)
        return OperationResult(True, "已处理", detail="已加入")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.ensure_channel_membership", fake_follow)
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(id="task-skip-invalid-required", tenant_id=1, name="跳过无效引用", type="group_ai_chat", status="running")
        account = TgAccount(id=46, tenant_id=1, display_name="账号46", phone_masked="46", status="在线", session_ciphertext="session")
        action = Action(id="membership-skip-invalid-required", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=46)
        session.add_all([task, account, action])
        session.commit()

        result = dispatcher._follow_required_channels_and_reprobe(
            session,
            action,
            account,
            object(),
            EnsureChannelMembershipPayload(channel_id="-100816", channel_target_id=916, target_type="group", target_display="天津音乐学院", require_send=True),
            OperationResult(False, "失败", "群无权限", "需要关注"),
            ["ad_user", "qiyue201"],
        )

    assert result.ok is True
    assert followed == ["qiyue201"]
    assert action.result["required_channels_followed"] == ["qiyue201"]
    assert action.result["required_channels_skipped"][0]["ref"] == "ad_user"


def test_membership_summary_uses_send_ready_title_group_when_target_peer_is_stale() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=910, tenant_id=1, target_type="group", tg_peer_id="-1002766", title="青岛师范学院", auth_status="只读", can_send=False)
        stale_group = TgGroup(id=2766, tenant_id=1, tg_peer_id="-1002766", title="青岛师范学院", group_type="supergroup", can_send=False)
        live_group = TgGroup(id=2149, tenant_id=1, tg_peer_id="-1002149", title="青岛师范学院", group_type="supergroup", auth_status="已授权运营", can_send=True)
        account = TgAccount(id=31, tenant_id=1, display_name="账号31", phone_masked="31", status="在线", session_ciphertext="session")
        session.add_all([target, stale_group, live_group, account, TgGroupAccount(tenant_id=1, group_id=live_group.id, account_id=account.id, can_send=True)])
        session.commit()

        summary = channel_membership_summary(session, 1, target, {"selection_mode": "all"}, candidates=[account], require_send=True)

    assert summary["joined_account_count"] == 1
    assert summary["need_join_account_count"] == 0


def test_group_reference_prefers_send_ready_title_group_when_exact_peer_is_stale() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=912, tenant_id=1, target_type="group", tg_peer_id="-1002766", title="青岛师范学院", auth_status="只读", can_send=False))
        session.add(TgGroup(id=2766, tenant_id=1, tg_peer_id="-1002766", title="青岛师范学院", group_type="supergroup", can_send=False))
        session.add(TgGroup(id=2149, tenant_id=1, tg_peer_id="-1002149", title="青岛师范学院", group_type="supergroup", auth_status="已授权运营", can_send=True))
        session.commit()

        group = group_from_reference(session, 1, operation_target_id=912, require_authorized=False)

    assert group.id == 2149


def test_permission_denied_verification_reads_selected_group_ref(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: OperationResult(True, detail="joined"))
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(False, "失败", "群无权限", "群无权限或账号不可发言"))
    monkeypatch.setattr(dispatcher, "_auto_verify_and_apply_group_send", lambda *_args, **_kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=911, tenant_id=1, target_type="group", tg_peer_id="-1002766", username="qdsfxy", title="青岛师范学院", auth_status="只读", can_send=False))
        session.add(TgGroup(id=2766, tenant_id=1, tg_peer_id="-1002766", title="青岛师范学院", group_type="supergroup", can_send=False))
        session.add(TgAccount(id=32, tenant_id=1, display_name="账号32", phone_masked="32", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-username-verification", tenant_id=1, name="用户名验证", type="group_ai_chat", status="running", type_config={"auto_resolve_verification": True}))
        session.add(
            Action(
                id="membership-username-verification",
                tenant_id=1,
                task_id="task-username-verification",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=32,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-1002766", "channel_target_id": 911, "target_type": "group", "target_display": "青岛师范学院", "target_username": "qdsfxy", "require_send": True},
            )
        )
        session.commit()

        action = session.get(Action, "membership-username-verification")
        assert dispatcher.dispatch_action(session, action) is True
        verification = session.query(VerificationTask).one()
        resolved_group = session.query(TgGroup).filter(TgGroup.tg_peer_id == "-1002766").one()

    assert verification.group_id == resolved_group.id
    assert verification.target_peer_id == "-1002766"


def test_target_membership_prefers_verified_peer_over_stale_username(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[str] = []

    def fake_join(_account_id, target_peer_id, *_args, **_kwargs):
        calls.append(target_peer_id)
        if target_peer_id == "-1003426646531":
            return OperationResult(True, detail="joined")
        return OperationResult(False, "失败", "peer_invalid", f'No user has "{target_peer_id}" as username')

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", fake_join)
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=914, tenant_id=1, target_type="group", tg_peer_id="qdsfxy", username="qdsfxy", title="青岛师范学院", auth_status="只读", can_send=False)
        task = Task(id="task-stale-username", tenant_id=1, name="青岛师范学院", type="group_ai_chat", status="running")
        account = TgAccount(id=44, tenant_id=1, display_name="账号44", phone_masked="44", status="在线", session_ciphertext="session")
        stale_group = TgGroup(id=3426, tenant_id=1, title="青岛师范学院", tg_peer_id="qdsfxy", can_send=False)
        verification = VerificationTask(tenant_id=1, account_id=44, verification_type="群发言权限", target_peer_id="-1003426646531", target_display="青岛师范学院", status="需人工处理")
        action = Action(id="membership-stale-username", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=44, payload={"channel_id": "qdsfxy", "channel_target_id": 914, "target_type": "group", "target_display": "青岛师范学院", "target_username": "qdsfxy", "invite_link": "https://t.me/qdsfxy", "require_send": True})
        session.add_all([target, task, account, stale_group, verification, action])
        session.commit()

        assert dispatcher.dispatch_action(session, action) is True

    assert calls[0] == "-1003426646531"
    assert "qdsfxy" not in calls[:1]


def test_failed_membership_keeps_attempted_peer_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *_args, **_kwargs: OperationResult(False, "失败", "peer_invalid", "目标实体无法解析"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=915, tenant_id=1, target_type="group", tg_peer_id="-100915", title="诊断群", auth_status="只读", can_send=False))
        session.add(TgAccount(id=45, tenant_id=1, display_name="账号45", phone_masked="45", status="在线", session_ciphertext="session"))
        session.add(Task(id="task-peer-ref", tenant_id=1, name="诊断群", type="group_ai_chat", status="running"))
        action = Action(
            id="membership-peer-ref",
            tenant_id=1,
            task_id="task-peer-ref",
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=45,
            status="pending",
            scheduled_at=now_value,
            payload={"channel_id": "-100915", "channel_target_id": 915, "target_type": "group", "target_display": "诊断群", "require_send": True},
        )
        session.add(action)
        session.commit()

        assert dispatcher.dispatch_action(session, action) is True

    assert action.status == "failed"
    assert action.result["error_code"] == "peer_invalid"
    assert action.result["membership_peer_ref"] == "-100915"


def test_permission_denied_verification_prefers_send_ready_title_group_for_reader_fallback(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *args, **kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "ensure_channel_membership", lambda *_args, **_kwargs: OperationResult(True, detail="joined"))
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(False, "失败", "群无权限", "群无权限或账号不可发言"))
    monkeypatch.setattr(dispatcher, "_auto_verify_and_apply_group_send", lambda *_args, **_kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=912, tenant_id=1, target_type="group", tg_peer_id="-1002766", username="qdsfxy", title="青岛师范学院", auth_status="只读", can_send=False))
        session.add(TgGroup(id=2766, tenant_id=1, tg_peer_id="-1002766", title="青岛师范学院", group_type="supergroup", can_send=False))
        live_group = TgGroup(id=2149, tenant_id=1, tg_peer_id="-1002149", title="青岛师范学院", group_type="supergroup", auth_status="已授权运营", can_send=True)
        session.add(live_group)
        session.add_all([
            TgAccount(id=32, tenant_id=1, display_name="加入账号", phone_masked="32", status="在线", session_ciphertext="session-32"),
            TgAccount(id=33, tenant_id=1, display_name="可读账号", phone_masked="33", status="在线", session_ciphertext="session-33"),
            TgGroupAccount(tenant_id=1, group_id=2149, account_id=33, can_send=True),
            Task(id="task-title-reader-verification", tenant_id=1, name="同名 reader 验证", type="group_ai_chat", status="running"),
            Action(
                id="membership-title-reader-verification",
                tenant_id=1,
                task_id="task-title-reader-verification",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=32,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_id": "-1002766", "channel_target_id": 912, "target_type": "group", "target_display": "青岛师范学院", "target_username": "qdsfxy", "require_send": True},
            ),
        ])
        session.commit()

        action = session.get(Action, "membership-title-reader-verification")
        assert dispatcher.dispatch_action(session, action) is True
        verification = session.query(VerificationTask).one()
        readers = dispatcher._image_verification_reader_candidates(session, verification, session.get(TgAccount, 32))

    assert verification.group_id == live_group.id
    assert verification.target_peer_id == "-1002149"
    assert [account.id for account, _credentials in readers] == [33]


def test_auto_text_verification_extracts_arithmetic_answer_and_rechecks(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    submitted: list[str] = []

    def fake_context(_account_id, *_args, **_kwargs):
        return [{"message_id": 5, "sender": "验证机器人", "text": "入群验证：3 + 5 = ?", "sent_at": None}]

    def fake_submit(_account_id, _peer, response_text, *_args, **_kwargs):
        submitted.append(response_text)
        return OperationResult(True, "已处理", detail="答案已提交")

    def fake_probe(_account_id, _peer, _target_type, *_args, **_kwargs):
        return OperationResult(True, detail="复检可发言")

    monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_context", fake_context)
    monkeypatch.setattr("app.services.membership_challenges.gateway.submit_verification_response", fake_submit)
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", fake_probe)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=801, tenant_id=1, tg_peer_id="-100801", title="验证群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=901, tenant_id=1, target_type="group", tg_peer_id="-100801", title="验证群", auth_status="只读", can_send=False)
        task = Task(id="task-text-verify", tenant_id=1, name="文本验证", type="group_ai_chat", status="running", type_config={"auto_resolve_verification": True})
        account = TgAccount(id=31, tenant_id=1, display_name="账号31", phone_masked="31", status="在线", session_ciphertext="session")
        action = Action(id="membership-text-verify", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=31)
        verification = VerificationTask(
            tenant_id=1,
            account_id=31,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="入群验证：3 + 5 = ?",
            suggested_action="发送验证回复",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all([group, target, task, account, action, verification])
        session.add(TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False))
        session.commit()

        ctx = dispatcher.MembershipDispatchContext(
            session,
            action,
            account,
            object(),
            EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True),
            None,
        )
        result = dispatcher._try_auto_group_send_verification(ctx, verification)

    assert result.ok is True
    assert submitted == ["8"]


def test_auto_text_verification_extracts_chinese_arithmetic_answer(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    submitted: list[str] = []

    monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_context", lambda *_args, **_kwargs: [{"message_id": 5, "sender": "验证机器人", "text": "入群验证：三加五等于多少", "sent_at": None}])
    monkeypatch.setattr("app.services.membership_challenges.gateway.submit_verification_response", lambda _account_id, _peer, response_text, *_args, **_kwargs: submitted.append(response_text) or OperationResult(True, "已处理", detail="答案已提交"))
    monkeypatch.setattr(dispatcher.gateway, "probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=802, tenant_id=1, tg_peer_id="-100802", title="中文验证群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=902, tenant_id=1, target_type="group", tg_peer_id="-100802", title="中文验证群", auth_status="只读", can_send=False)
        task = Task(id="task-cn-text-verify", tenant_id=1, name="中文文本验证", type="group_ai_chat", status="running", type_config={"auto_resolve_verification": True})
        account = TgAccount(id=32, tenant_id=1, display_name="账号32", phone_masked="32", status="在线", session_ciphertext="session")
        action = Action(id="membership-cn-text-verify", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=32)
        verification = VerificationTask(
            tenant_id=1,
            account_id=32,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="入群验证：三加五等于多少",
            suggested_action="发送验证回复",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all([group, target, task, account, action, verification, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False)])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(
                session,
                action,
                account,
                object(),
                EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True),
                None,
            ),
            verification,
        )

    assert result.ok is True
    assert submitted == ["8"]


def test_image_verification_falls_back_to_text_answer_when_context_has_no_image(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    submitted: list[str] = []

    monkeypatch.setattr(
        "app.services.membership_challenges.gateway.fetch_verification_context",
        lambda *_args, **_kwargs: [
            {"message_id": 5, "sender": "验证机器人", "text": "入群验证：4 + 6 = ?", "sent_at": None}
        ],
    )
    monkeypatch.setattr(
        "app.services.membership_challenges.gateway.submit_verification_response",
        lambda _account_id, _peer, response_text, *_args, **_kwargs: submitted.append(response_text)
        or OperationResult(True, "已处理", detail="答案已提交"),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(
            id=803,
            tenant_id=1,
            tg_peer_id="-100803",
            title="误判验证群",
            group_type="supergroup",
            auth_status="已授权运营",
            can_send=False,
        )
        target = OperationTarget(
            id=903,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100803",
            title="误判验证群",
            auth_status="只读",
            can_send=False,
        )
        task = Task(
            id="task-image-text-fallback",
            tenant_id=1,
            name="图形误判文本验证",
            type="group_ai_chat",
            status="running",
            type_config={"auto_resolve_verification": True},
        )
        account = TgAccount(
            id=33,
            tenant_id=1,
            display_name="账号33",
            phone_masked="33",
            status="在线",
            session_ciphertext="session",
        )
        action = Action(
            id="membership-image-text-fallback",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=33,
        )
        verification = VerificationTask(
            tenant_id=1,
            account_id=33,
            group_id=group.id,
            verification_type="群发言权限",
            detected_reason="未解析到群关联频道",
            suggested_action="识别图形验证码",
            target_peer_id=group.tg_peer_id,
            target_display=group.title,
            status="待处理",
        )
        session.add_all(
            [
                group,
                target,
                task,
                account,
                action,
                verification,
                TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=False),
            ]
        )
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(
                session,
                action,
                account,
                object(),
                EnsureChannelMembershipPayload(
                    channel_id=group.tg_peer_id,
                    channel_target_id=target.id,
                    target_type="group",
                    target_display=group.title,
                    require_send=True,
                ),
                None,
            ),
            verification,
        )

    assert result.ok is True
    assert submitted == ["10"]


def test_image_verification_missing_image_detail_includes_context_summary(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.membership_challenges.gateway.fetch_verification_context",
        lambda *_args, **_kwargs: [{"message_id": 5, "sender": "验证机器人", "text": "欢迎入群", "sent_at": None, "has_media": False}],
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=806, tenant_id=1, tg_peer_id="-100806", title="无图验证群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=906, tenant_id=1, target_type="group", tg_peer_id="-100806", title="无图验证群", auth_status="只读", can_send=False)
        task = Task(id="task-image-missing-detail", tenant_id=1, name="图形无图详情", type="group_ai_chat", status="running")
        account = TgAccount(id=36, tenant_id=1, display_name="账号36", phone_masked="36", status="在线", session_ciphertext="session")
        action = Action(id="membership-image-missing-detail", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=36)
        verification = VerificationTask(tenant_id=1, account_id=36, group_id=group.id, verification_type="群发言权限", detected_reason="群无权限或账号不可发言", suggested_action="识别图形验证码", target_peer_id=group.tg_peer_id, target_display=group.title, status="待处理")
        session.add_all([group, target, task, account, action, verification])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(session, action, account, object(), EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True), None),
            verification,
        )

    assert result.ok is False
    assert "context_status=ok" in verification.failure_detail
    assert "messages=1" in verification.failure_detail


def test_image_verification_falls_back_to_required_channel_links(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    followed: list[str] = []

    monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_context", lambda *_args, **_kwargs: [{"message_id": 5, "sender": "验证机器人", "text": "请先关注 [按钮：报告频道 (https://t.me/qdsf_report)]", "sent_at": None}])
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.ensure_channel_membership", lambda _account_id, channel_ref, *_args, **_kwargs: followed.append(channel_ref) or OperationResult(True, "已处理", detail="已关注"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=804, tenant_id=1, tg_peer_id="-100804", title="关注验证群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=904, tenant_id=1, target_type="group", tg_peer_id="-100804", title="关注验证群", auth_status="只读", can_send=False)
        task = Task(id="task-image-follow-fallback", tenant_id=1, name="图形转关注", type="group_ai_chat", status="running")
        account = TgAccount(id=34, tenant_id=1, display_name="账号34", phone_masked="34", status="在线", session_ciphertext="session")
        action = Action(id="membership-image-follow-fallback", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=34)
        verification = VerificationTask(tenant_id=1, account_id=34, group_id=group.id, verification_type="群发言权限", detected_reason="群无权限或账号不可发言", suggested_action="识别图形验证码", target_peer_id=group.tg_peer_id, target_display=group.title, status="待处理")
        session.add_all([group, target, task, account, action, verification])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(session, action, account, object(), EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True), None),
            verification,
        )

    assert result.ok is True
    assert followed == ["qdsf_report", "-100804"]
    assert verification.suggested_action == "关注频道"
    assert action.result["target_membership_retried_after_required_channel"] is True


def test_image_verification_falls_back_to_button_click_when_context_has_buttons(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    resolved: list[str] = []

    monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_context", lambda *_args, **_kwargs: [{"message_id": 5, "sender": "验证机器人", "text": "请点击下方按钮完成验证 [按钮：开始验证]", "sent_at": None}])
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.resolve_verification_task", lambda _account_id, action, *_args, **_kwargs: resolved.append(action) or OperationResult(True, "已处理", detail="已点击首个验证按钮"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="复检可发言"))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=805, tenant_id=1, tg_peer_id="-100805", title="按钮验证群", group_type="supergroup", auth_status="已授权运营", can_send=False)
        target = OperationTarget(id=905, tenant_id=1, target_type="group", tg_peer_id="-100805", title="按钮验证群", auth_status="只读", can_send=False)
        task = Task(id="task-image-button-fallback", tenant_id=1, name="图形转按钮", type="group_ai_chat", status="running")
        account = TgAccount(id=35, tenant_id=1, display_name="账号35", phone_masked="35", status="在线", session_ciphertext="session")
        action = Action(id="membership-image-button-fallback", tenant_id=1, task_id=task.id, task_type="group_ai_chat", action_type="ensure_target_membership", account_id=35)
        verification = VerificationTask(tenant_id=1, account_id=35, group_id=group.id, verification_type="群发言权限", detected_reason="群无权限或账号不可发言", suggested_action="识别图形验证码", target_peer_id=group.tg_peer_id, target_display=group.title, status="待处理")
        session.add_all([group, target, task, account, action, verification])
        session.commit()

        result = dispatcher._try_auto_group_send_verification(
            dispatcher.MembershipDispatchContext(session, action, account, object(), EnsureChannelMembershipPayload(channel_id=group.tg_peer_id, channel_target_id=target.id, target_type="group", target_display=group.title, require_send=True), None),
            verification,
        )

    assert result.ok is True
    assert resolved == ["点击按钮"]
    assert verification.suggested_action == "点击按钮"


def test_verification_context_reads_deep_enough_for_active_group_history() -> None:
    assert VERIFICATION_CONTEXT_DEFAULT_LIMIT >= 120


def test_membership_permission_denied_skip_counts_as_failed() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=902, tenant_id=1, target_type="group", tg_peer_id="-100902", title="准入群", auth_status="已授权运营", can_send=True)
        account = TgAccount(id=12, tenant_id=1, display_name="账号12", phone_masked="12", status="在线", session_ciphertext="session")
        task = Task(id="task-permission-denied", tenant_id=1, name="权限失败", type="group_ai_chat", status="running", account_config={"selection_mode": "all"})
        session.add_all([target, account, task])
        session.add(
            Action(
                id="membership-permission-denied",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=account.id,
                status="skipped",
                payload={"channel_target_id": target.id},
                result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
            )
        )
        session.commit()

        summary = channel_membership_summary(session, 1, target, task.account_config, task_id=task.id, require_send=True)

    assert summary["failed_account_ids"] == [12]
    assert summary["failed_account_count"] == 1
    assert summary["need_join_account_count"] == 0


def test_membership_unknown_after_send_stays_out_of_retry_candidates() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now() - timedelta(hours=1)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=903, tenant_id=1, target_type="group", tg_peer_id="-100903", title="结果未知群", auth_status="已授权运营", can_send=True)
        account = TgAccount(id=13, tenant_id=1, display_name="账号13", phone_masked="13", status="在线", session_ciphertext="session")
        task = Task(
            id="task-membership-unknown-summary",
            tenant_id=1,
            name="结果未知汇总",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 60},
        )
        session.add_all([target, account, task])
        session.add(
            Action(
                id="membership-unknown-summary",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=account.id,
                status="unknown_after_send",
                executed_at=now_value,
                payload={"channel_target_id": target.id},
                result={"error_code": "unknown_after_send"},
            )
        )
        session.commit()

        summary = channel_membership_summary(session, 1, target, task.account_config, task_id=task.id, require_send=True)
        gate = gate_channel_membership(session, task, target, require_send=True)
        created_count = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").count()

    assert summary["unknown_after_send_account_ids"] == [13]
    assert summary["unknown_after_send_count"] == 1
    assert summary["need_join_account_count"] == 0
    assert summary["failed_account_count"] == 0
    assert summary["estimated_membership_actions"] == 0
    assert gate.blocked is True
    assert created_count == 1


def test_hard_hourly_reactivates_auto_verification_membership_failures() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=904, tenant_id=1, target_type="group", tg_peer_id="-100904", title="验证群", auth_status="已授权运营", can_send=True)
        group = TgGroup(id=804, tenant_id=1, tg_peer_id="-100904", title="验证群", group_type="supergroup", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-verification",
            tenant_id=1,
            name="硬目标验证重试",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 904, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        session.add_all(
            [
                target,
                group,
                TgAccount(id=31, tenant_id=1, display_name="账号31", phone_masked="31", status="在线", session_ciphertext="session"),
                task,
                Action(
                    id="membership-denied-31",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=31,
                    status="skipped",
                    scheduled_at=now_value - timedelta(minutes=10),
                    executed_at=now_value - timedelta(minutes=10),
                    payload={
                        "channel_id": "-100904",
                        "channel_target_id": target.id,
                        "target_type": "group",
                        "target_display": target.title,
                        "require_send": True,
                    },
                    result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
                ),
                VerificationTask(
                    id=7001,
                    tenant_id=1,
                    account_id=31,
                    group_id=group.id,
                    verification_type="群发言权限",
                    detected_reason="需要图形验证码",
                    suggested_action="识别图形验证码",
                    status="失败",
                    handled_at=now_value - timedelta(minutes=10),
                ),
            ]
        )
        session.commit()

        result = gate_channel_membership(session, task, target, require_send=True)
        rows = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.scheduled_at.asc()).all()

    assert result.waiting is True
    assert result.created == 1
    assert [row.status for row in rows] == ["skipped", "pending"]
    assert rows[1].result["reactivated_reason"] == "hard_hourly_auto_verification_retry"
    assert rows[1].result["verification_task_id"] == 7001
    assert task.stats["membership_reactivated_verification_actions"] == 1
    assert task.stats["membership_failed_count"] == 0
    assert task.stats["membership_need_join_count"] == 1


def test_hard_hourly_reactivates_required_channel_membership_failures() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=907, tenant_id=1, target_type="group", tg_peer_id="-100907", title="关注群", auth_status="已授权运营", can_send=True)
        group = TgGroup(id=807, tenant_id=1, tg_peer_id="-100907", title="关注群", group_type="supergroup", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-required-channel",
            tenant_id=1,
            name="硬目标关注频道重试",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 907, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        account = TgAccount(id=32, tenant_id=1, display_name="账号32", phone_masked="32", status="在线", session_ciphertext="session")
        session.add_all([target, group, task, account])
        session.flush()
        payload = EnsureChannelMembershipPayload(channel_id="-100907", channel_target_id=target.id, target_type="group", target_display=target.title, require_send=True)
        old_action = Action(
            id="membership-required-channel-32",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="ensure_target_membership",
            account_id=account.id,
            status="skipped",
            scheduled_at=now_value - timedelta(minutes=10),
            executed_at=now_value - timedelta(minutes=10),
            payload=payload.model_dump(mode="json"),
            result={"error_code": "membership_permission_denied", "error_message": "您需要关注我们的频道才能发言 https://t.me/qiyue201"},
        )
        session.add(old_action)
        session.commit()

        result = gate_channel_membership(session, task, target, require_send=True)
        rows = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.scheduled_at.asc()).all()

    assert result.waiting is True
    assert result.created == 1
    assert [row.status for row in rows] == ["skipped", "pending"]
    assert rows[1].result["reactivated_reason"] == "hard_hourly_required_channel_retry"
    assert task.stats["membership_failed_count"] == 0
    assert task.stats["membership_need_join_count"] == 1


def test_hard_hourly_reactivation_batches_membership_action_flush(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=905, tenant_id=1, target_type="group", tg_peer_id="-100905", title="批量验证群", auth_status="已授权运营", can_send=True)
        group = TgGroup(id=805, tenant_id=1, tg_peer_id="-100905", title="批量验证群", group_type="supergroup", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-verification-batch",
            tenant_id=1,
            name="硬目标验证批量重试",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 905, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        session.add_all([target, group, task])
        for account_id in [41, 42]:
            session.add_all(
                [
                    TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext="session"),
                    Action(
                        id=f"membership-denied-{account_id}",
                        tenant_id=1,
                        task_id=task.id,
                        task_type="group_ai_chat",
                        action_type="ensure_target_membership",
                        account_id=account_id,
                        status="skipped",
                        scheduled_at=now_value - timedelta(minutes=10),
                        executed_at=now_value - timedelta(minutes=10),
                        payload={
                            "channel_id": "-100905",
                            "channel_target_id": target.id,
                            "target_type": "group",
                            "target_display": target.title,
                            "require_send": True,
                        },
                        result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
                    ),
                    VerificationTask(
                        id=7100 + account_id,
                        tenant_id=1,
                        account_id=account_id,
                        group_id=group.id,
                        verification_type="群发言权限",
                        detected_reason="需要图形验证码",
                        suggested_action="识别图形验证码",
                        status="失败",
                        handled_at=now_value - timedelta(minutes=10),
                    ),
                ]
        )
        session.commit()
        candidates = session.query(TgAccount).filter(TgAccount.id.in_([41, 42])).all()
        original_flush = session.flush

        def fail_pending_flush(*args, **kwargs):  # noqa: ANN002, ANN003
            if session.new:
                raise AssertionError("reactivation should bulk insert without pending ORM action flush")
            return original_flush(*args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(session, "flush", fail_pending_flush)
            created = _reactivate_auto_verification_memberships(
                session,
                task,
                target,
                candidates,
                require_send=True,
            )
        retry_count = session.query(Action).filter(Action.task_id == task.id, Action.status == "pending").count()

    assert created == 2
    assert retry_count == 2


def test_hard_hourly_reactivation_creates_fresh_retry_action_with_fixed_batch_key() -> None:
    from app.services.task_center import channel_membership as membership_service

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=906, tenant_id=1, tg_peer_id="-100906", title="固定批次群", auth_status="已授权运营")
        target = OperationTarget(id=906, tenant_id=1, target_type="group", tg_peer_id="-100906", title="固定批次群", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-reactivation-fixed-batch",
            tenant_id=1,
            name="固定批次自动验证重试",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 906, "hard_hourly_target_enabled": True, "hourly_min_messages": 300, "auto_resolve_verification": True},
            stats={"current_plan_batch_key": "fixed-hard-hourly-batch"},
        )
        account = TgAccount(id=61, tenant_id=1, display_name="账号61", phone_masked="61", status="在线", session_ciphertext="session")
        session.add_all([group, target, task, account])
        session.flush()
        payload = EnsureChannelMembershipPayload(channel_id="-100906", channel_target_id=target.id, target_type="group", target_display=target.title, require_send=True)
        old_action = membership_service.create_membership_action(session, task, account.id, now_value - timedelta(minutes=10), payload)
        old_action.status = "skipped"
        old_action.executed_at = now_value - timedelta(minutes=10)
        old_action.result = {"error_code": "membership_permission_denied", "membership_status": "permission_denied"}
        session.add(
            VerificationTask(
                id=7200,
                tenant_id=1,
                account_id=account.id,
                group_id=group.id,
                verification_type="群发言权限",
                detected_reason="需要验证码",
                suggested_action="发送验证回复",
                status="失败",
                handled_at=now_value - timedelta(minutes=10),
            )
        )
        session.commit()

        created = _reactivate_auto_verification_memberships(session, task, target, [account], require_send=True)
        actions = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").all()
        old_result = old_action.result
        has_retry = any(action.status == "pending" and action.result.get("reactivated_reason") == "hard_hourly_auto_verification_retry" for action in actions)

    assert created == 1
    assert len(actions) == 2
    assert old_result == {"error_code": "membership_permission_denied", "membership_status": "permission_denied"}
    assert has_retry is True


def test_hard_hourly_missing_membership_batches_action_flush(monkeypatch) -> None:
    from app.services.task_center import channel_membership as membership_service

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=906, tenant_id=1, target_type="group", tg_peer_id="-100906", title="缺口群", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-missing-membership-batch",
            tenant_id=1,
            name="硬目标准入批量创建",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 906, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        accounts = [
            TgAccount(id=51, tenant_id=1, display_name="账号51", phone_masked="51", status="在线", session_ciphertext="session"),
            TgAccount(id=52, tenant_id=1, display_name="账号52", phone_masked="52", status="在线", session_ciphertext="session"),
        ]
        session.add_all([target, task, *accounts])
        session.commit()

        flush_flags: list[bool] = []
        original_create = membership_service.create_membership_action

        def spy_create(*args, **kwargs):  # noqa: ANN002, ANN003
            flush_flags.append(bool(kwargs.get("flush", True)))
            return original_create(*args, **kwargs)

        monkeypatch.setattr(membership_service, "create_membership_action", spy_create)
        created = _create_membership_actions_for_accounts(
            session,
            task,
            target,
            set(),
            accounts,
            now_value,
            require_send=True,
        )

    assert created == 2
    assert flush_flags == [False, False]


def test_hard_hourly_group_ai_fast_tracks_future_membership_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=903, tenant_id=1, target_type="group", tg_peer_id="-100903", title="硬目标群", auth_status="已授权运营", can_send=True)
        task = Task(
            id="task-hard-hourly-membership",
            tenant_id=1,
            name="硬目标 AI 群",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 903, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        session.add_all(
            [
                target,
                TgAccount(id=21, tenant_id=1, display_name="账号21", phone_masked="21", status="在线", session_ciphertext="session"),
                TgAccount(id=22, tenant_id=1, display_name="账号22", phone_masked="22", status="在线", session_ciphertext="session"),
                task,
                Action(
                    id="membership-future-21",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=21,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=8),
                    payload={"channel_target_id": target.id},
                ),
                Action(
                    id="membership-future-22",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=22,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=9),
                    payload={"channel_target_id": target.id},
                ),
            ]
        )
        session.commit()

        result = gate_channel_membership(session, task, target, require_send=True)
        rows = session.query(Action).filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership").order_by(Action.scheduled_at.asc()).all()

    assert result.waiting is True
    assert [row.account_id for row in rows] == [21, 22]
    assert rows[0].scheduled_at <= now_value + timedelta(seconds=5)
    assert rows[1].scheduled_at <= now_value + timedelta(seconds=10)


def test_hard_hourly_retries_terminal_future_scheduled_membership_action() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(
            id=908,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100908",
            title="青岛师范学院",
            auth_status="已授权运营",
            can_send=True,
        )
        task = Task(
            id="task-hard-hourly-terminal-future-membership",
            tenant_id=1,
            name="硬目标终态准入恢复",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 908, "hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        )
        account = TgAccount(id=31, tenant_id=1, display_name="账号31", phone_masked="31", status="在线", session_ciphertext="session")
        stale_action = Action(
            id="membership-terminal-future",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=31,
            status="skipped",
            scheduled_at=now_value + timedelta(hours=1),
            created_at=now_value - timedelta(minutes=10),
            result={"membership_status": "not_joined"},
            payload={"channel_target_id": target.id, "target_type": "group", "target_display": target.title},
        )
        session.add_all([target, task, account, stale_action])
        session.commit()

        result = gate_channel_membership(session, task, target, require_send=True)
        rows = (
            session.query(Action)
            .filter(Action.task_id == task.id, Action.action_type == "ensure_target_membership")
            .order_by(Action.created_at.asc())
            .all()
        )

    assert result.created == 1
    assert len(rows) == 2
    assert rows[-1].account_id == 31
    assert rows[-1].status == "pending"
    assert rows[-1].scheduled_at <= now_value + timedelta(seconds=5)


def test_group_rescue_admin_rate_limit_accepts_aware_stats_datetime() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    retry_at = (_now() + timedelta(minutes=30)).replace(tzinfo=timezone(timedelta(hours=8)))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-aware-group-rescue-limit",
            tenant_id=1,
            name="救援限流时区",
            type="target_admission_retry",
            status="running",
            stats={
                "group_rescue_admin_rate_limited_until": retry_at.isoformat(),
                "group_rescue_admin_rate_limit_detail": "FloodWait 1800 秒",
            },
        )
        action = Action(
            id="aware-group-rescue-action",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="invite_group_account",
            account_id=515,
            status="pending",
            scheduled_at=_now(),
            payload={"group_id": 484, "target_account_id": 84},
        )
        session.add_all([task, action])
        session.commit()

        deferred = dispatcher._defer_existing_group_rescue_admin_rate_limit(session, action)

        assert deferred is True
        assert action.status == "pending"
        assert action.scheduled_at.tzinfo is None
        assert action.result["error_code"] == "FloodWait"
