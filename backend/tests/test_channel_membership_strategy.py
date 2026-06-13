from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult
from app.models import Action, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.channel_membership import (
    _create_membership_actions_for_accounts,
    _reactivate_auto_verification_memberships,
    channel_membership_summary,
    gate_channel_membership,
)
from app.services.task_center.payloads import EnsureChannelMembershipPayload


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


def test_group_send_verification_classifies_arithmetic_captcha_as_reply() -> None:
    assert dispatcher._group_send_verification_action("请输入 3 + 5 的结果后才能发言") == "发送验证回复"
    assert dispatcher._group_send_verification_action("加减验证码：9-4=?") == "发送验证回复"
    assert dispatcher._group_send_verification_action("请先关注 @alpha @beta 后输入 3+5") == "发送验证回复"


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
