from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.integrations.telegram import OperationResult, SendResult
from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ChannelMessageComment,
    ExecutionAttempt,
    GroupContextMessage,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import (
    ChannelCommentTaskCreate,
    GroupAIChatTaskConfigUpdate,
    GroupAIChatTaskCreate,
    TaskDetailOut,
    TaskPrecheckRequest,
    TaskSettingsUpdate,
)
from app.services.content_filters import ContentFilterResult
from app.services.task_center import dispatcher
from app.services.task_center.executors import channel_comment
from app.services.task_center.ai_generator import AiGenerationUnavailable, generate_group_reply_messages
from app.services.task_center.channel_membership import gate_channel_membership
from app.services.task_center.dispatcher import claim_actions, dispatch_action
from app.services.task_center.executors.channel_comment import build_plan as build_channel_comment_plan
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_chat_plan
from app.services.task_center.service import precheck_task_creation, reset_task, update_group_ai_chat_config


NOW = datetime(2026, 5, 30, 10, 0, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _add_tenant(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))


def _add_group(session: Session, account_count: int) -> None:
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="测试群", auth_status="已授权运营"))
    for account_id in range(101, 101 + account_count):
        session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
    session.add(
        GroupContextMessage(
            id=43,
            tenant_id=1,
            group_id=7,
            listener_account_id=101,
            sender_name="真人用户",
            content="今天群里有什么安排",
            remote_message_id="43",
            sent_at=NOW - timedelta(minutes=10),
        )
    )
    session.add(
        GroupContextMessage(
            id=44,
            tenant_id=1,
            group_id=7,
            listener_account_id=102 if account_count > 1 else 101,
            sender_name="另一个真人",
            content="晚点还有人一起吗",
            remote_message_id="44",
            sent_at=NOW - timedelta(minutes=8),
        )
    )


def _add_group_task(session: Session, type_config: dict) -> Task:
    task = Task(
        id="ai-limit-task",
        tenant_id=1,
        name="AI 活群数量",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 50, "cooldown_per_account_minutes": 0},
        pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
        type_config={"target_group_id": 7, "fact_anchor_required": False, **type_config},
        stats={},
    )
    session.add(task)
    return task


def test_group_ai_config_update_preserves_unspecified_round_size() -> None:
    with _session() as session:
        _add_tenant(session)
        session.add(
            OperationTarget(
                id=7,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="测试群目标",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        _add_group(session, account_count=3)
        task = _add_group_task(
            session,
            {
                "target_operation_target_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 60,
                "reply_min_per_round": 5,
                "membership_max_concurrent": 5,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.commit()

        updated = update_group_ai_chat_config(
            session,
            1,
            task.id,
            GroupAIChatTaskConfigUpdate(
                target_group_id=7,
                target_operation_target_id=7,
                membership_max_concurrent=50,
            ),
            "tester",
        )

    assert updated.type_config["membership_max_concurrent"] == 50
    assert updated.type_config["messages_per_round_mode"] == "manual"
    assert updated.type_config["messages_per_round"] == 60
    assert updated.type_config["reply_min_per_round"] == 5


def _add_channel(session: Session, message_count: int, account_count: int, comment_flags: list[bool] | None = None) -> OperationTarget:
    channel = OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道目标", can_send=True, auth_status="已授权运营")
    session.add(channel)
    for index in range(message_count):
        comment_available = comment_flags[index] if comment_flags and index < len(comment_flags) else True
        session.add(
            ChannelMessage(
                id=41 + index,
                tenant_id=1,
                channel_target_id=31,
                message_id=9001 + index,
                content_preview=f"频道消息 {index + 1}",
                comment_available=comment_available,
            )
        )
    for account_id in range(101, 101 + account_count):
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"评论账号{account_id}",
                username=f"comment_user_{account_id}",
                tg_first_name=f"评论号{account_id}",
                avatar_object_key=f"avatars/{account_id}.jpg",
                profile_sync_status="已同步",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100,
            )
        )
    return channel


def _add_comment_task(session: Session) -> Task:
    task = Task(
        id="comment-hour-budget",
        tenant_id=1,
        name="AI 评论小时预算",
        type="channel_comment",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
        pacing_config={"mode": "fixed", "max_actions_per_hour": 5, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
        type_config={
            "target_channel_id": 31,
            "message_scope": "specific",
            "message_ids": [41, 42],
            "target_comments_per_message": 10,
            "comment_count_jitter": 0,
            "max_comments_per_account_per_hour": 500,
        },
        stats={},
    )
    session.add(task)
    return task


def test_group_ai_schema_allows_large_round_plan():
    payload = GroupAIChatTaskCreate(name="大轮次计划", target_group_id=7, messages_per_round_mode="manual", messages_per_round=30)

    assert payload.messages_per_round == 30


def test_reply_minimum_schema_fields_are_explicit_and_bounded():
    group_payload = GroupAIChatTaskCreate(
        name="引用回复活群",
        target_group_id=7,
        messages_per_round_mode="manual",
        messages_per_round=3,
        reply_min_per_round=2,
    )
    comment_payload = ChannelCommentTaskCreate(
        name="引用回复评论",
        target_channel_id=31,
        target_comments_per_message=4,
        reply_min_per_message=2,
    )
    settings = TaskSettingsUpdate(reply_min_per_round=1, reply_min_per_message=1)

    assert group_payload.reply_min_per_round == 2
    assert comment_payload.reply_min_per_message == 2
    assert settings.reply_min_per_round == 1
    assert settings.reply_min_per_message == 1

    for factory, kwargs, expected_field in [
        (GroupAIChatTaskCreate, {"name": "越界活群", "target_group_id": 7, "messages_per_round_mode": "manual", "messages_per_round": 1, "reply_min_per_round": 2}, "reply_min_per_round"),
        (ChannelCommentTaskCreate, {"name": "越界评论", "target_channel_id": 31, "target_comments_per_message": 1, "reply_min_per_message": 2}, "reply_min_per_message"),
    ]:
        try:
            factory(**kwargs)
        except Exception as exc:  # pydantic validation error
            assert expected_field in str(exc)
        else:
            raise AssertionError(f"{expected_field} should be bounded by the total count")


def test_channel_comment_schema_defaults_task_total_limit_with_jitter():
    payload = ChannelCommentTaskCreate(name="默认总上限评论", target_channel_id=31)

    assert payload.max_total_comments == 80
    assert payload.max_total_comments_jitter == 0.3


def test_channel_comment_legacy_config_uses_default_total_limit_jitter(monkeypatch):
    captured: dict[str, float] = {}

    def fake_quantity_with_jitter(quantity: int, jitter_ratio: float):
        captured["quantity"] = quantity
        captured["jitter_ratio"] = jitter_ratio
        return 91

    monkeypatch.setattr(channel_comment, "quantity_with_jitter", fake_quantity_with_jitter)
    task = Task(id="legacy-comment-limit", tenant_id=1, name="旧评论任务", type="channel_comment", status="running", stats={})

    resolved = channel_comment._resolved_total_comment_limit(task, {})

    assert resolved == 91
    assert captured == {"quantity": 80, "jitter_ratio": 0.3}
    assert task.stats["max_total_comments_resolved"] == 91


def test_channel_comment_planner_respects_task_total_comment_limit(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"{message_content} 新评论 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=3, account_count=120)
        task = _add_comment_task(session)
        task.pacing_config = {
            "mode": "fixed",
            "max_actions_per_hour": 100,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        }
        task.type_config = {
            **task.type_config,
            "message_ids": [41, 42, 43],
            "target_comments_per_message": 100,
            "max_total_comments": 80,
            "max_total_comments_jitter": 0,
        }
        session.add_all(
            Action(
                id=f"existing-total-comment-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                account_id=101 + index,
                status="success",
                scheduled_at=NOW,
                executed_at=NOW,
                payload={"channel_message_id": 41 + (index % 3), "message_id": 9001 + (index % 3)},
            )
            for index in range(78)
        )
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 2
    assert total_actions == 80


def test_group_reply_generation_does_not_fallback_without_ai_provider():
    with _session() as session:
        _add_tenant(session)

        try:
            generate_group_reply_messages(
                session,
                1,
                {"topic_hint": "测试"},
                reply_targets=[{"message_id": 43, "author": "真人用户", "preview": "今天群里有什么安排", "source": "human_context"}],
                target_label="测试群",
                history="真人用户: 今天群里有什么安排",
            )
        except AiGenerationUnavailable as exc:
            assert "AI 生成不可用" in str(exc)
        else:
            raise AssertionError("引用回复不能在 AI provider 不可用时走 fallback")


def test_reply_payload_config_error_is_visible_in_task_stats():
    with _session() as session:
        _add_tenant(session)
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status=AccountStatus.ACTIVE.value))
        task = Task(id="reply-payload-error-task", tenant_id=1, name="引用 payload 错误", type="group_ai_chat", status="running", stats={})
        action = Action(
            id="reply-payload-error-action",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=101,
            payload={
                "chat_id": "-1007",
                "group_id": 7,
                "message_text": "少了 reply id",
                "reply_target_author": "真人用户",
                "reply_target_preview": "今天群里有什么安排",
            },
            status="pending",
        )
        session.add_all([task, action])
        session.commit()

        handled = dispatch_action(session, action)
        session.flush()
        session.refresh(task)
        session.refresh(action)

    assert handled is True
    assert action.status == "failed"
    assert task.stats["reply_payload_error_count"] == 1
    assert "reply_to_message_id" in action.result["error_message"]


def test_group_ai_schema_exposes_membership_strategy_defaults():
    payload = GroupAIChatTaskCreate(name="准入策略", target_group_id=7)

    assert payload.auto_join_target is True
    assert payload.auto_follow_required_channel is True
    assert payload.auto_resolve_verification is True
    assert payload.ai_assisted_verification is True
    assert payload.captcha_failure_policy == "manual"
    assert payload.membership_max_concurrent == 5


def test_group_ai_settings_update_accepts_membership_strategy_fields():
    payload = TaskSettingsUpdate(
        auto_join_target=False,
        auto_follow_required_channel=False,
        auto_resolve_verification=False,
        ai_assisted_verification=False,
        captcha_failure_policy="manual",
        membership_max_concurrent=8,
    )

    assert payload.auto_join_target is False
    assert payload.auto_follow_required_channel is False
    assert payload.auto_resolve_verification is False
    assert payload.ai_assisted_verification is False
    assert payload.captcha_failure_policy == "manual"
    assert payload.membership_max_concurrent == 8


def test_regular_task_detail_does_not_default_to_empty_profile_batch():
    detail = TaskDetailOut(
        task={
            "id": "ai-limit-task",
            "tenant_id": 1,
            "name": "AI 活群数量",
            "type": "group_ai_chat",
            "status": "running",
            "priority": 3,
            "timezone": "Asia/Shanghai",
            "scheduled_start": None,
            "scheduled_end": None,
            "max_duration_hours": None,
            "next_run_at": NOW,
            "last_error": "",
            "account_config": {},
            "pacing_config": {},
            "failure_policy": {},
            "type_config": {},
            "stats": {},
            "created_at": NOW,
            "updated_at": NOW,
        },
        actions=[],
        stats={},
    )

    assert detail.profile_batch is None
    assert detail.model_dump()["profile_batch"] is None


def test_group_ai_manual_participation_does_not_raise_turn_count(monkeypatch):
    generated_counts: list[int] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        generated_counts.append(count)
        return [f"第 {index} 条" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=20)
        task = _add_group_task(session, {"messages_per_round_mode": "manual", "messages_per_round": 3, "participation_rate": 1, "participation_jitter": 0})
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert generated_counts == [3]
    assert created == 3


def test_group_ai_auto_turn_count_uses_hour_limit(monkeypatch):
    generated_counts: list[int] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        seeds = ["问安排", "补时间", "聊地点", "接天气", "问人数", "说交通", "提晚饭", "问作业", "聊活动", "接话题"]
        generated_counts.append(count)
        return [seeds[index % len(seeds)] for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=30)
        task = _add_group_task(session, {"messages_per_round_mode": "auto", "participation_rate": 1, "participation_jitter": 0})
        round_curve = [0] * 24
        round_curve[NOW.hour] = 12
        task.pacing_config = {
            "mode": "fixed",
            "max_actions_per_hour": 120,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
            "operation_profile": {"hourly_activity_curve": round_curve},
        }
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert generated_counts == [10]
    assert created == 10


def test_group_ai_plans_reply_turns_with_bound_targets(monkeypatch):
    normal_counts: list[int] = []
    captured_reply_targets: list[dict] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        normal_counts.append(count)
        return [f"普通发言 {index}" for index in range(count)], 0

    def fake_generate_group_reply_messages(_session, _tenant_id, _config, *, reply_targets: list[dict], target_label: str, history: str):
        return [f"回复 {index} {item['author']}：{item['preview']}" for index, item in enumerate(reply_targets)], 0

    def capture_reply_messages(session, tenant_id, config, *, reply_targets: list[dict], target_label: str, history: str):
        reply_targets_seen = [dict(item) for item in reply_targets]
        captured_reply_targets.extend(reply_targets_seen)
        return fake_generate_group_reply_messages(session, tenant_id, config, reply_targets=reply_targets_seen, target_label=target_label, history=history)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", capture_reply_messages, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = sorted(session.scalars(select(Action).where(Action.task_id == task.id)).all(), key=lambda action: action.payload["turn_index"])

    assert created == 3
    assert normal_counts == [1]
    assert len(captured_reply_targets) == 2
    assert [action.payload["reply_to_message_id"] for action in actions[:2]] == [44, 43]
    assert [action.payload["reply_target_author"] for action in actions[:2]] == ["另一个真人", "真人用户"]
    assert actions[1].payload["reply_target_preview"] == "今天群里有什么安排"
    assert actions[2].payload["reply_to_message_id"] is None


def test_group_ai_does_not_reuse_reply_targets_when_pool_is_short(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [f"普通发言 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        session.query(GroupContextMessage).filter(GroupContextMessage.id == 44).delete()
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "可引用消息不足" in task.last_error


def test_group_ai_ignores_other_task_history_for_reply_targets(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [f"普通发言 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        session.query(GroupContextMessage).delete()
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "reply_min_per_round": 1,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.add(
            Task(
                id="other-group-history-task",
                tenant_id=1,
                name="其他活群任务",
                type="group_ai_chat",
                status="running",
                stats={},
            )
        )
        session.add(
            Action(
                id="other-group-history-action",
                tenant_id=1,
                task_id="other-group-history-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                payload={"group_id": 7, "message_text": "其他任务发过的消息"},
                result={"remote_message_id": 777},
                executed_at=NOW,
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "可引用消息不足" in task.last_error


def test_group_ai_excludes_already_used_reply_targets_across_rounds(monkeypatch):
    captured_reply_targets: list[dict] = []

    def fake_generate_group_reply_messages(_session, _tenant_id, _config, *, reply_targets: list[dict], target_label: str, history: str):
        captured_reply_targets.extend(dict(item) for item in reply_targets)
        return [f"回复 {item['author']}：{item['preview']}" for item in reply_targets], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", fake_generate_group_reply_messages, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=1)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "reply_min_per_round": 1,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.add(
            Action(
                id="used-group-reply-action",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                payload={"group_id": 7, "message_text": "已回复过 44", "reply_to_message_id": 44},
                result={"remote_message_id": 90044},
                executed_at=NOW,
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id, Action.id != "used-group-reply-action")).all()

    assert created == 1
    assert [item["message_id"] for item in captured_reply_targets] == [43]
    assert [action.payload["reply_to_message_id"] for action in actions] == [43]


def test_group_ai_reply_target_check_does_not_scan_irrelevant_history(monkeypatch):
    captured_reply_targets: list[dict] = []

    def fake_generate_group_reply_messages(_session, _tenant_id, _config, *, reply_targets: list[dict], target_label: str, history: str):
        captured_reply_targets.extend(dict(item) for item in reply_targets)
        return [f"回复 {item['author']}：{item['preview']}" for item in reply_targets], 0

    def fail_on_irrelevant_history(action, key: str) -> int:
        if str(action.id).startswith("irrelevant-history-"):
            raise AssertionError("reply target lookup loaded irrelevant historical action payloads")
        payload = action.payload if isinstance(action.payload, dict) else {}
        raw = str(payload.get(key) or "").strip()
        return int(raw) if raw.isdigit() else 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", fake_generate_group_reply_messages, raising=False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._payload_int", fail_on_irrelevant_history)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=1)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "reply_min_per_round": 1,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        for index in range(250):
            session.add(
                Action(
                    id=f"irrelevant-history-{index}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=101,
                    status="success",
                    payload={"group_id": 7, "message_text": f"历史 {index}", "reply_to_message_id": 10_000 + index},
                    result={"remote_message_id": 20_000 + index},
                    executed_at=NOW - timedelta(minutes=index + 20),
                )
            )
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert created == 1
    assert [item["message_id"] for item in captured_reply_targets] == [44]


def test_group_ai_hard_hourly_membership_to_send_dispatch_closed_loop(monkeypatch):
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        seeds = ["晚点还有安排吗", "我看群里刚才挺热闹", "这个时间大家都在吧"]
        return [seeds[index % len(seeds)] for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.dispatcher._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.dispatcher.credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.ensure_channel_membership", lambda *_args, **_kwargs: OperationResult(True, "已处理", detail="joined"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.probe_target_capabilities", lambda *_args, **_kwargs: OperationResult(True, detail="可发言"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message", lambda *_args, **_kwargs: SendResult(True, remote_message_id="tg-ok"))
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message_to_target", lambda *_args, **_kwargs: SendResult(True, remote_message_id="tg-ok"))

    with _session() as session:
        _add_tenant(session)
        session.add(
            OperationTarget(
                id=7,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="测试群目标",
                can_send=False,
                auth_status="只读",
            )
        )
        _add_group(session, account_count=3)
        group = session.get(TgGroup, 7)
        group.can_send = False
        group.slowmode_seconds = None
        for link in session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == 7)):
            link.can_send = False
        task = _add_group_task(
            session,
            {
                "target_operation_target_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 0,
                "participation_rate": 1,
                "participation_jitter": 0,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.commit()

        first_created = build_group_ai_chat_plan(session, task)
        membership_actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "ensure_target_membership")))
        first_send_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id, Action.action_type == "send_message"))
        membership_results = [dispatch_action(session, action) for action in membership_actions]
        session.refresh(group)
        send_links = list(session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == 7)))

        second_created = build_group_ai_chat_plan(session, task)
        [send_action] = claim_actions(session, limit=1, worker_id="hard-hourly-test")
        send_handled = dispatch_action(session, send_action)
        group_can_send = group.can_send
        links_can_send = all(link.can_send for link in send_links)
        send_status = send_action.status
        send_result = send_action.result

    assert first_created >= 1
    assert first_send_count == 0
    assert membership_results == [True, True, True]
    assert group_can_send is True
    assert links_can_send is True
    assert second_created == 3
    assert send_handled is True
    assert send_status == "success", send_result


def test_group_ai_hard_hourly_retries_stale_membership_actions(monkeypatch):
    monkeypatch.setattr("app.services.task_center.channel_membership._now", lambda: NOW)

    with _session() as session:
        _add_tenant(session)
        session.add(
            OperationTarget(
                id=7,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="测试群目标",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        _add_group(session, account_count=3)
        for link in session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == 7)):
            link.can_send = False
        task = _add_group_task(
            session,
            {
                "target_operation_target_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all(
            [
                Action(
                    id="old-membership-success",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=101,
                    status="success",
                    scheduled_at=NOW - timedelta(minutes=10),
                    executed_at=NOW - timedelta(minutes=10),
                    payload={"channel_target_id": 7, "channel_id": "-1007", "require_send": True},
                ),
                Action(
                    id="open-membership",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=103,
                    status="pending",
                    scheduled_at=NOW + timedelta(minutes=30),
                    payload={"channel_target_id": 7, "channel_id": "-1007", "require_send": True},
                ),
            ]
        )
        session.commit()

        result = gate_channel_membership(session, task, session.get(OperationTarget, 7), require_send=True)
        actions = list(
            session.scalars(
                select(Action)
                .where(Action.action_type == "ensure_target_membership")
                .order_by(Action.account_id.asc(), Action.created_at.asc())
            )
        )

    assert result.ready is False
    assert result.created == 2
    assert [action.account_id for action in actions] == [101, 101, 102, 103]
    assert [action.status for action in actions if action.account_id == 103] == ["pending"]


def test_group_ai_does_not_fill_reply_candidate_shortage_with_normal_turns(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [f"普通发言 {index}" for index in range(count)], 0

    def fake_generate_group_reply_messages(_session, _tenant_id, _config, *, reply_targets: list[dict], target_label: str, history: str):
        return ["只生成一条引用回复"], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", fake_generate_group_reply_messages, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "AI 引用回复候选不足" in task.last_error


def test_group_ai_hard_hourly_skips_reply_lookup_for_volume_planning(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        samples = [
            "今晚活动几点开始",
            "报名入口谁再发一下",
            "新来的可以先看群公告",
        ]
        return samples[:count], 0

    def fake_generate_group_reply_messages(*_args, **_kwargs):
        raise AssertionError("hard-hourly volume planning must not call reply generation")

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", fake_generate_group_reply_messages, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.created_at.asc())).all()

    assert created == 3
    assert [action.payload.get("reply_to_message_id") for action in actions] == [None, None, None]
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert not task.stats.get("reply_candidate_shortfall_count")
    assert not task.last_error


def test_group_ai_does_not_fill_filtered_reply_shortage_with_normal_turns(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return [f"普通发言 {index}" for index in range(count)], 0

    def fake_generate_group_reply_messages(_session, _tenant_id, _config, *, reply_targets: list[dict], target_label: str, history: str):
        return ["拦截这条引用回复", "这条引用回复保留"], 0

    def fake_filter(_session, *, tenant_id, group, content, reject_mentions, reject_replies):
        if "拦截" in content:
            return ContentFilterResult(False, content, "测试拦截")
        return ContentFilterResult(True, content)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_reply_messages", fake_generate_group_reply_messages, raising=False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.filter_outbound_content", fake_filter)
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=3)
        task = _add_group_task(
            session,
            {
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "reply_min_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
            },
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "AI 引用回复候选不足" in task.last_error


def test_channel_comment_planner_respects_current_hour_budget(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        seeds = [
            "18cm 收纳盒这个尺寸塞小柜子刚好",
            "图里那个透明盖子看着挺防尘",
            "如果能补一下承重数据就更直观",
            "小户型厨房应该会用得上",
            "这个边角设计会不会容易卡灰",
            "颜色如果有磨砂款可能更耐看",
            "抽屉高度低的柜子也能放吗",
            "叠放三层之后拿取方便不方便",
            "这类盒子最怕盖子太松",
            "有实测清洗后会不会变形吗",
        ]
        return [f"{message_content}：{seeds[index % len(seeds)]}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=2, account_count=20)
        task = _add_comment_task(session)
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))
        per_message = [
            session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id, Action.payload["channel_message_id"].as_integer() == message_id))
            for message_id in [41, 42]
        ]

    assert created == 5
    assert total_actions == 5
    assert sorted(per_message) == [2, 3]


def test_channel_comment_planner_uses_remaining_current_hour_budget(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        seeds = [
            "尺寸细节看着挺明确",
            "透明盖子这点比较实用",
            "承重信息如果补一下更好",
            "小厨房收纳应该能用上",
        ]
        return [f"{message_content}：{seeds[index % len(seeds)]}" for index in range(count)], 0

    now_value = NOW + timedelta(minutes=30)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment._now", lambda: now_value, raising=False)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=2, account_count=120)
        task = _add_comment_task(session)
        task.pacing_config = {
            "mode": "fixed",
            "max_actions_per_hour": 100,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        }
        task.type_config = {**task.type_config, "target_comments_per_message": 100, "max_total_comments": 1000, "max_total_comments_jitter": 0}
        existing_actions = [
            Action(
                id=f"existing-current-hour-comment-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                account_id=101 + index,
                status="success",
                scheduled_at=now_value - timedelta(minutes=10),
                executed_at=now_value - timedelta(minutes=5),
                payload={"channel_message_id": 41 if index % 2 == 0 else 42},
            )
            for index in range(96)
        ]
        session.add_all(existing_actions)
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 4
    assert total_actions == 100


def test_channel_comment_planner_stops_when_collected_comments_reach_target(monkeypatch):
    generated_counts: list[int] = []

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        generated_counts.append(count)
        return [f"新增评论 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=3)
        task = _add_comment_task(session)
        task.type_config = {**task.type_config, "message_ids": [41], "target_comments_per_message": 2}
        session.add_all(
            [
                ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="评论号101", author_username="comment_user_101"),
                ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="评论号102", author_username="comment_user_102"),
            ]
        )
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert generated_counts == []
    assert created == 0
    assert total_actions == 0


def test_channel_comment_planner_excludes_uninitialized_profile_accounts(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"评论 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=2)
        uninitialized = session.get(TgAccount, 101)
        uninitialized.username = "english_name"
        uninitialized.tg_first_name = "John"
        uninitialized.avatar_object_key = ""
        uninitialized.profile_sync_status = "未同步"
        task = _add_comment_task(session)
        task.type_config = {**task.type_config, "message_ids": [41], "target_comments_per_message": 2}
        session.commit()

        created = build_channel_comment_plan(session, task)
        action_accounts = session.scalars(select(Action.account_id).where(Action.task_id == task.id)).all()

    assert created == 2
    assert action_accounts == [102, 102]


def test_channel_comment_plans_minimum_auto_replies(monkeypatch):
    normal_counts: list[int] = []
    captured_reply_targets: list[dict] = []

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        seeds = ["普通评论 尺寸信息挺实用", "普通评论 想看更多实测"]
        normal_counts.append(count)
        return seeds[:count], 0

    def fake_generate_channel_reply_comments(_session, _tenant_id, _config, *, reply_targets: list[dict], message_content: str, target_label: str):
        reply_targets_seen = [dict(item) for item in reply_targets]
        captured_reply_targets.extend(reply_targets_seen)
        return [f"回复 {item['author']}：{item['preview']}" for item in reply_targets_seen], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_reply_comments", fake_generate_channel_reply_comments, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="读者 A", content_preview="这个尺寸多少"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="读者 B", content_preview="有实测吗"))
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 10, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 4,
            "reply_min_per_message": 2,
            "comment_mode": "mixed",
        }
        session.commit()

        created = build_channel_comment_plan(session, task)
        actions = sorted(session.scalars(select(Action).where(Action.task_id == task.id)).all(), key=lambda action: action.payload["comment_text"])

    assert created == 4
    assert normal_counts == [2]
    assert [item["message_id"] for item in captured_reply_targets] == [8101, 8102]
    reply_actions = [action for action in actions if action.payload["reply_to_message_id"]]
    assert [action.payload["reply_to_message_id"] for action in reply_actions] == [8101, 8102]
    assert reply_actions[0].payload["reply_target_author"] == "读者 A"
    assert reply_actions[0].payload["reply_target_preview"] == "这个尺寸多少"


def test_channel_comment_comment_mode_ignores_stale_reply_minimum(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        seeds = [
            "敲门前那段细节还挺有画面感",
            "蹦蹦跳跳这个反应写得很真实",
            "后面节奏如果再展开一点会更好看",
            "这种日常口吻比硬夸自然很多",
        ]
        return [f"{message_content}：{seeds[index % len(seeds)]}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 10, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 4,
            "comment_mode": "comment",
            "reply_min_per_message": 5,
        }
        session.commit()

        created = build_channel_comment_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    assert created == 4
    assert len(actions) == 4
    assert all(not action.payload["reply_to_message_id"] for action in actions)


def test_channel_comment_does_not_reuse_reply_targets_when_pool_is_short(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"普通评论 {index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="读者 A", content_preview="这个尺寸多少"))
        task = _add_comment_task(session)
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 4,
            "reply_min_per_message": 2,
            "comment_mode": "mixed",
        }
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "可引用评论不足" in task.last_error


def test_channel_comment_excludes_already_used_reply_targets_across_rounds(monkeypatch):
    captured_reply_targets: list[dict] = []

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"普通评论 {index}" for index in range(count)], 0

    def fake_generate_channel_reply_comments(_session, _tenant_id, _config, *, reply_targets: list[dict], message_content: str, target_label: str):
        captured_reply_targets.extend(dict(item) for item in reply_targets)
        return [f"回复 {item['author']}：{item['preview']}" for item in reply_targets], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_reply_comments", fake_generate_channel_reply_comments, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="读者 A", content_preview="这个尺寸多少"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="读者 B", content_preview="有实测吗"))
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 10, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 2,
            "comment_mode": "mixed",
            "reply_min_per_message": 1,
        }
        session.add(
            Action(
                id="used-channel-reply-action",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                account_id=101,
                status="success",
                payload={
                    "channel_target_id": 31,
                    "channel_message_id": 41,
                    "message_id": 9001,
                    "comment_text": "已回复过读者 A",
                    "reply_to_message_id": 8101,
                },
                result={"telegram_msg_id": 9101},
                executed_at=NOW,
            )
        )
        session.commit()

        created = build_channel_comment_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id, Action.id != "used-channel-reply-action")).all()

    assert created == 1
    assert [item["message_id"] for item in captured_reply_targets] == [8102]
    assert [action.payload["reply_to_message_id"] for action in actions] == [8102]


def test_channel_comment_finds_unused_reply_target_beyond_initial_window(monkeypatch):
    captured_reply_targets: list[dict] = []

    def fake_generate_channel_reply_comments(_session, _tenant_id, _config, *, reply_targets: list[dict], message_content: str, target_label: str):
        captured_reply_targets.extend(dict(item) for item in reply_targets)
        return [f"回复 {item['author']}：{item['preview']}" for item in reply_targets], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", lambda *_args, **_kwargs: ([], 0))
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_reply_comments", fake_generate_channel_reply_comments, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        for index in range(21):
            comment_id = 8101 + index
            session.add(
                ChannelMessageComment(
                    tenant_id=1,
                    channel_target_id=31,
                    channel_message_id=41,
                    comment_message_id=comment_id,
                    author_name=f"读者 {index + 1}",
                    content_preview=f"评论 {index + 1}",
                )
            )
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 25, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 21,
            "comment_mode": "mixed",
            "reply_min_per_message": 1,
        }
        for index in range(20):
            comment_id = 8101 + index
            session.add(
                Action(
                    id=f"used-channel-reply-action-{comment_id}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="channel_comment",
                    action_type="post_comment",
                    account_id=101,
                    status="pending",
                    payload={
                        "channel_target_id": 31,
                        "channel_message_id": 41,
                        "message_id": 9001,
                        "comment_text": f"已回复过 {comment_id}",
                        "reply_to_message_id": comment_id,
                    },
                )
            )
        session.commit()

        created = build_channel_comment_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id, Action.id.not_like("used-channel-reply-action-%"))).all()

    assert created == 1
    assert [item["message_id"] for item in captured_reply_targets] == [8121]
    assert [action.payload["reply_to_message_id"] for action in actions] == [8121]


def test_channel_comment_does_not_fill_reply_candidate_shortage_with_normal_comments(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"普通评论 {index}" for index in range(count)], 0

    def fake_generate_channel_reply_comments(_session, _tenant_id, _config, *, reply_targets: list[dict], message_content: str, target_label: str):
        return ["只生成一条引用评论"], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_reply_comments", fake_generate_channel_reply_comments, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="读者 A", content_preview="这个尺寸多少"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="读者 B", content_preview="有实测吗"))
        task = _add_comment_task(session)
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 4,
            "reply_min_per_message": 2,
            "comment_mode": "mixed",
        }
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "AI 引用评论候选不足" in task.last_error


def test_channel_comment_does_not_fill_filtered_reply_shortage_with_normal_comments(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        return [f"普通评论 {index}" for index in range(count)], 0

    def fake_generate_channel_reply_comments(_session, _tenant_id, _config, *, reply_targets: list[dict], message_content: str, target_label: str):
        return ["拦截这条引用评论", "这条引用评论保留"], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_reply_comments", fake_generate_channel_reply_comments, raising=False)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=4)
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8101, author_name="读者 A", content_preview="这个尺寸多少"))
        session.add(ChannelMessageComment(tenant_id=1, channel_target_id=31, channel_message_id=41, comment_message_id=8102, author_name="读者 B", content_preview="有实测吗"))
        session.add(RuleSet(id=91, tenant_id=1, name="引用过滤", status="active", task_types=["channel_comment"], active_version_id=92))
        session.add(RuleSetVersion(id=92, tenant_id=1, rule_set_id=91, version=1, status="published", output_checks={"forbidden_keywords": ["拦截"]}))
        task = _add_comment_task(session)
        task.type_config = {
            **task.type_config,
            "message_ids": [41],
            "target_comments_per_message": 4,
            "comment_mode": "mixed",
            "reply_min_per_message": 2,
            "rule_set_version_id": 92,
        }
        session.commit()

        created = build_channel_comment_plan(session, task)
        total_actions = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert created == 0
    assert total_actions == 0
    assert "AI 引用评论候选不足" in task.last_error


def test_channel_comment_caps_single_message_generation_batch(monkeypatch):
    generated_counts: list[int] = []
    seeds = ["河东区位置挺具体", "对象编号这个信息清楚", "报告入口可以再看看", "积分优惠这个点有人用过吗"]

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        generated_counts.append(count)
        return seeds[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=1, account_count=20)
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 20, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {**task.type_config, "message_ids": [41], "target_comments_per_message": 80}
        session.commit()

        created = build_channel_comment_plan(session, task)

    assert generated_counts == [4]
    assert created == 4


def test_channel_comment_skips_messages_without_comment_thread(monkeypatch):
    generated_message_texts: list[str] = []

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        generated_message_texts.append(message_content)
        return [f"{message_content} 可评论"], 0

    monkeypatch.setattr("app.services.task_center.executors.channel_comment.generate_channel_comments", fake_generate_channel_comments)
    with _session() as session:
        _add_tenant(session)
        _add_channel(session, message_count=2, account_count=2, comment_flags=[False, True])
        task = _add_comment_task(session)
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 10, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        task.type_config = {**task.type_config, "message_ids": [41, 42], "target_comments_per_message": 1}
        session.commit()

        created = build_channel_comment_plan(session, task)
        payloads = session.scalars(select(Action.payload).where(Action.task_id == task.id)).all()

    assert generated_message_texts == ["频道消息 2"]
    assert created == 1
    assert [payload["channel_message_id"] for payload in payloads] == [42]


def test_reset_task_preserves_pending_actions_with_execution_attempts():
    with _session() as session:
        _add_tenant(session)
        task = Task(id="reset-attempt-task", tenant_id=1, name="重置已有执行记录", type="channel_comment", status="running", stats={})
        action = Action(
            id="attempted-pending-action",
            tenant_id=1,
            task_id=task.id,
            task_type="channel_comment",
            action_type="post_comment",
            scheduled_at=NOW,
            status="pending",
            payload={"message_id": 74},
        )
        session.add_all([task, action, ExecutionAttempt(tenant_id=1, action_id=action.id, attempt_no=1)])
        session.commit()

        reset_task(session, 1, task.id, "pytest", reason="重新规划")
        preserved = session.get(Action, action.id)

    assert preserved is not None
    assert preserved.status == "skipped"
    assert preserved.executed_at is not None
    assert preserved.result["error_code"] == "plan_superseded"


def test_precheck_returns_dynamic_ai_limit_recommendations():
    with _session() as session:
        _add_tenant(session)
        _add_group(session, account_count=10)
        session.commit()

        result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_ai_chat",
                payload={
                    "name": "AI 推荐",
                    "target_group_id": 7,
                    "account_config": {"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                    "pacing_config": {"max_actions_per_hour": None},
                    "messages_per_round_mode": "auto",
                },
            ),
        )

    recommendations = result["capacity_summary"]["recommended_limits"]
    assert recommendations["max_actions_per_hour"] == 60
    assert recommendations["messages_per_round"] > 1
    assert recommendations["basis"]["ready_account_count"] == 10
