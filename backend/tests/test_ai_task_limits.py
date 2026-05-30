from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ExecutionAttempt,
    GroupContextMessage,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import GroupAIChatTaskCreate, TaskPrecheckRequest
from app.services.task_center.executors.channel_comment import build_plan as build_channel_comment_plan
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_chat_plan
from app.services.task_center.service import precheck_task_creation, reset_task


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
            remote_message_id="real-once",
            sent_at=NOW - timedelta(minutes=10),
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
        session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"评论账号{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, health_score=100))
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
        task.pacing_config = {"mode": "fixed", "max_actions_per_hour": 120, "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0}
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert generated_counts == [10]
    assert created == 10


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
