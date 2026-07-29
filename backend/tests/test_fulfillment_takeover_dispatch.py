from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
)
from app.services._common import _now
from app.services.task_center.channel_fulfillment import (
    view_account_ids_for_messages,
    view_daily_counts,
)
from app.services.task_center import dispatcher, service
from app.services.task_center.comment_fulfillment_takeover import (
    ensure_comment_action_contract,
)
from app.services.task_center.fulfillment_takeover import (
    FULFILLMENT_CONTRACT_VERSION,
    UNIFIED_TASK_GATE_LIMIT,
    normalize_fulfillment_pacing,
    takeover_task,
)
from app.services.task_center.fulfillment_takeover_actions import (
    restore_terminal_search_attempts,
)
from app.services.task_center.stats import retry_failed_actions


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    "task_type",
    [
        "group_ai_chat",
        "channel_comment",
        "channel_like",
        "channel_view",
        "search_click",
    ],
)
def test_fulfillment_hourly_limit_is_a_system_gate(task_type: str) -> None:
    pacing = normalize_fulfillment_pacing(
        task_type,
        {"max_actions_per_hour": 1},
    )

    assert pacing["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    if task_type == "search_click":
        assert pacing["max_actions_per_day"] == UNIFIED_TASK_GATE_LIMIT
        assert pacing["per_account_daily_action_limit"] == UNIFIED_TASK_GATE_LIMIT
        assert pacing["per_account_hourly_action_limit"] == UNIFIED_TASK_GATE_LIMIT
        assert pacing["per_keyword_account_daily_limit"] == UNIFIED_TASK_GATE_LIMIT
        assert pacing["skip_probability_per_action"] == 0
        assert pacing["hourly_skip_probability"] == 0
        assert pacing["daily_skip_probability"] == 0


@pytest.mark.parametrize(
    "task_type",
    [
        "group_ai_chat",
        "channel_comment",
        "channel_like",
        "channel_view",
        "search_click",
    ],
)
def test_new_fulfillment_contract_uses_dispatcher_instead_of_backlog_gate(
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
) -> None:
    task = Task(
        id=f"{task_type}-dispatch-gate",
        tenant_id=1,
        name="新履约任务",
        type=task_type,
        status="running",
        stats={
            "fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION,
            "planner_backlog_blocked": True,
        },
    )

    monkeypatch.setattr(
        service,
        "planner_backlog_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("新履约任务不应再进入旧 backlog 数量门禁")
        ),
    )

    assert service._planning_backlog_blocked(object(), task) is False
    assert "planner_backlog_blocked" not in task.stats


def test_takeover_clears_obsolete_capacity_blockers_and_search_limits(
    session: Session,
) -> None:
    task = Task(
        id="legacy-search-capacity-blocker",
        tenant_id=1,
        name="旧搜索容量阻塞",
        type="search_click",
        status="stopped",
        pacing_config={
            "per_account_daily_action_limit": 2,
            "per_account_hourly_action_limit": 1,
            "per_keyword_account_daily_limit": 2,
            "skip_probability_per_action": 0.5,
        },
        stats={
            "search_join_stats": {"daily_fulfillment": {"status": "blocked"}},
            "planner_backlog_blocked": True,
        },
        last_error="daily_target_capacity_insufficient",
    )
    session.add(task)
    session.flush()

    takeover_task(session, task, now=_now())

    assert task.last_error == ""
    assert "search_join_stats" not in task.stats
    assert "planner_backlog_blocked" not in task.stats
    assert task.pacing_config["per_account_daily_action_limit"] == UNIFIED_TASK_GATE_LIMIT
    assert task.pacing_config["per_account_hourly_action_limit"] == UNIFIED_TASK_GATE_LIMIT
    assert task.pacing_config["per_keyword_account_daily_limit"] == UNIFIED_TASK_GATE_LIMIT
    assert task.pacing_config["skip_probability_per_action"] == 0


def test_takeover_clears_obsolete_view_cap_error(session: Session) -> None:
    task = Task(
        id="legacy-view-capacity-blocker",
        tenant_id=1,
        name="旧浏览容量阻塞",
        type="channel_view",
        status="stopped",
        last_error="任务今日浏览安全上限已用完，等待下一日继续规划",
    )
    session.add(task)
    session.flush()

    takeover_task(session, task, now=_now())

    assert task.last_error == ""
    assert task.type_config["task_daily_view_safety_cap"] == UNIFIED_TASK_GATE_LIMIT


def test_takeover_clears_obsolete_shared_dispatch_error(session: Session) -> None:
    task = Task(
        id="legacy-shared-dispatch-blocker",
        tenant_id=1,
        name="旧共享容量错误",
        type="channel_like",
        status="stopped",
        stats={"planner_backlog_blocked": True},
        last_error="shared_dispatch_capacity_insufficient",
    )
    session.add(task)
    session.flush()

    result = takeover_task(session, task, now=_now())

    assert result.changed is True
    assert task.last_error == ""
    assert "planner_backlog_blocked" not in task.stats


def test_planner_takeover_precedes_retry_and_backlog(
    session: Session,
) -> None:
    task = Task(
        id="planner-takeover-search",
        tenant_id=1,
        name="旧搜索重试清退",
        type="search_click",
        status="running",
        failure_policy={"max_retries": 3},
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
    )
    pending = Action(
        id="legacy-search-pending",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="pending",
        payload={},
    )
    failed = Action(
        id="legacy-search-failed",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="failed",
        payload={},
    )
    session.add_all([task, pending, failed])
    session.flush()

    remaining = service._takeover_before_retry(
        session,
        task,
        current_global_pending=1,
    )

    assert remaining == 0
    assert pending.status == "skipped"
    assert retry_failed_actions(session, task) == 0
    assert failed.status == "failed"


def test_bound_search_click_failure_is_rebuilt_instead_of_retried(
    session: Session,
) -> None:
    task = Task(
        id="search-click-terminal-attempt",
        tenant_id=1,
        name="热搜页换路径重建",
        type="search_click",
        status="running",
        failure_policy={"max_retries": 3},
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
    )
    action = Action(
        id="search-click-hot-list-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="failed",
        retry_count=0,
        payload={
            "search_execution_mode": "click_only",
            "search_click_obligation_id": "click-obligation-1",
            "search_click_assignment_id": "click-assignment-1",
        },
        result={
            "success": False,
            "error_code": "jisou_hot_list_page",
            "gateway_call_state": "started",
        },
    )
    session.add_all([task, action])
    session.flush()

    assert retry_failed_actions(session, task) == 0
    assert action.status == "failed"
    assert action.retry_count == 0
    assert action.result["error_code"] == "jisou_hot_list_page"


@pytest.mark.parametrize(
    ("action_status", "action_error"),
    [
        ("pending", "global_account_policy"),
        ("failed", "search_click_obligation_binding_invalid"),
    ],
)
def test_takeover_restores_search_failure_overwritten_by_old_retry(
    session: Session,
    action_status: str,
    action_error: str,
) -> None:
    task = Task(
        id="search-click-overwritten-failure",
        tenant_id=1,
        name="恢复被覆盖的极搜失败",
        type="search_click",
        status="running",
    )
    action = Action(
        id=f"search-click-overwritten-action-{action_status}",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status=action_status,
        payload={
            "search_click_obligation_id": "click-obligation-2",
            "search_click_assignment_id": "click-assignment-2",
        },
        result={"error_code": action_error},
    )
    attempt = ExecutionAttempt(
        id=f"search-click-hot-list-attempt-{action_status}",
        tenant_id=1,
        action_id=action.id,
        account_id=151,
        status="failed",
        gateway_call_started_at=_now(),
        after_call_at=_now(),
        failure_type="jisou_hot_list_page",
        result_snapshot={
            "success": False,
            "error_code": "jisou_hot_list_page",
        },
    )
    session.add_all([task, action, attempt])
    session.flush()

    assert restore_terminal_search_attempts(session, task) == 1
    assert action.status == "failed"
    assert action.executed_at == attempt.after_call_at
    assert action.result["error_code"] == "jisou_hot_list_page"
    assert restore_terminal_search_attempts(session, task) == 0


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def test_dispatcher_records_invalid_comment_contract_without_crashing(
    session: Session,
) -> None:
    task = Task(
        id="invalid-comment-task",
        tenant_id=1,
        name="缺消息的评论任务",
        type="channel_comment",
        status="running",
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
    )
    action = Action(
        id="invalid-comment-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        status="executing",
        payload={},
    )
    session.add_all([task, action])
    session.flush()

    processed = _dispatch(session, action)

    assert processed is True
    assert action.status == "failed"
    assert action.result["error_code"] == "task_fulfillment_contract_invalid"
    assert task.stats["fulfillment_takeover_status"] == "blocked"
    assert "message_missing" in task.stats["fulfillment_takeover_error"]


def test_retrying_comment_rebinds_its_existing_obligation(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=41,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10041",
        title="评论频道",
    )
    message = ChannelMessage(
        id=51,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=901,
    )
    task = Task(
        id="retry-comment-task",
        tenant_id=1,
        name="重试评论",
        type="channel_comment",
        status="running",
    )
    obligation = CommentFulfillmentObligation(
        id="retry-comment-obligation",
        tenant_id=1,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=1,
        target_ordinal=1,
        relation_kind="direct",
        action_attempt_no=1,
        status="replan_required",
    )
    action = Action(
        id="retry-comment-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        status="executing",
        payload={
            "channel_message_id": message.id,
            "comment_fulfillment_obligation_id": obligation.id,
        },
    )
    session.add_all([channel, message, task, obligation, action])
    session.flush()

    ensure_comment_action_contract(session, action, now=action.created_at)

    assert obligation.current_action_id == action.id
    assert obligation.status == "pending"
    assert obligation.action_attempt_no == 2


def test_takeover_preserves_unknown_view_as_held(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=42,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10042",
        title="浏览频道",
    )
    message = ChannelMessage(
        id=52,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=902,
    )
    account = TgAccount(
        id=102,
        tenant_id=1,
        display_name="浏览账号",
        phone_masked="102",
    )
    task = Task(
        id="unknown-view-task",
        tenant_id=1,
        name="未知浏览",
        type="channel_view",
        status="running",
    )
    action = Action(
        id="unknown-view-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=account.id,
        status="unknown_after_send",
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
        },
    )
    session.add_all([channel, message, account, task, action])
    session.flush()

    takeover_task(session, task, now=_now())
    session.flush()

    obligation = session.query(ViewFulfillmentObligation).one()
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id)
    assert obligation.status == "unknown"
    assert obligation.current_action_id == action.id
    assert view_account_ids_for_messages(
        session,
        task,
        ledger,
        [message],
    ) == {message.id: {account.id}}
    daily = view_daily_counts(session, ledger)
    assert daily.total == 1
    assert daily.by_account == {account.id: 1}


def test_dispatch_finalizer_marks_view_obligation_unknown(
    session: Session,
) -> None:
    channel = OperationTarget(
        id=43,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10043",
        title="未知结果频道",
    )
    message = ChannelMessage(
        id=53,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=903,
    )
    account = TgAccount(
        id=103,
        tenant_id=1,
        display_name="未知结果账号",
        phone_masked="103",
    )
    task = Task(
        id="unknown-finalizer-task",
        tenant_id=1,
        name="未知结果浏览",
        type="channel_view",
        status="running",
    )
    session.add_all([channel, message, account, task])
    session.flush()
    takeover_task(session, task, now=_now())
    ledger = session.query(TaskDayLedger).one()
    obligation = ViewFulfillmentObligation(
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=account.id,
        status="pending",
    )
    session.add(obligation)
    session.flush()
    action = Action(
        id="unknown-finalizer-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=account.id,
        status="unknown_after_send",
        payload={"view_fulfillment_obligation_id": obligation.id},
    )
    obligation.current_action_id = action.id
    session.add(action)
    session.flush()

    dispatcher._sync_channel_fulfillment_state(session, action)

    assert obligation.status == "unknown"
    assert obligation.current_action_id == action.id


def _dispatch(session: Session, action: Action) -> bool:
    return dispatcher._dispatch_action(
        session,
        action,
        generation_dependencies=dispatcher.PRODUCTION_GENERATION_DEPENDENCIES,
        comment_generation_dependencies=(
            dispatcher.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES
        ),
    )
