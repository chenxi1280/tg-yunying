from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    OperationTarget,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    SearchClickFulfillmentObligation,
    SchedulingSetting,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.schemas.ai_config import SchedulingSettingUpdate
from app.schemas.risk_control import RiskControlGlobalPolicyUpdate
from app.services._common import _now
from app.services.task_center.fulfillment_takeover import (
    FULFILLMENT_CONTRACT_VERSION,
    RETIRED_AI_QUANTITY_GATE_FIELDS,
    UNIFIED_TASK_GATE_LIMIT,
    normalize_fulfillment_scheduling_settings,
    takeover_task,
)


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.add(
            OperationTarget(
                id=31,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10031",
                username="target_group",
                title="目标群",
            )
        )
        current.commit()
        yield current


def test_running_legacy_search_is_taken_over_as_pure_click_idempotently(
    session: Session,
) -> None:
    now_value = _now()
    task = Task(
        id="legacy-search",
        tenant_id=1,
        name="旧混合搜索",
        type="search_join_group",
        status="running",
        scheduled_end=now_value + timedelta(days=2),
        next_run_at=now_value + timedelta(hours=3),
        account_config={"selection_mode": "manual", "account_ids": [101]},
        pacing_config={"max_actions_per_day": 500},
        type_config={
            "target_operation_target_id": 31,
            "target_input": "https://t.me/target_group",
            "target_title": "目标群",
            "target_link": "https://t.me/target_group",
            "daily_click_target_count": 3,
            "daily_target_count": 2,
            "search_bots": [{"username": "jisou", "display_name": "极搜"}],
            "keyword_hashes": ["a" * 64],
            "keyword_text_ciphertexts": ["ciphertext"],
        },
    )
    child = Action(
        id="legacy-child",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join_membership",
        status="executing",
        lease_owner="worker:legacy",
        lease_expires_at=now_value + timedelta(minutes=5),
    )
    started_child = Action(
        id="legacy-started-child",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join_membership",
        status="executing",
    )
    source = Action(
        id="legacy-source",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="pending",
        payload={"target_input": "https://t.me/target_group"},
    )
    session.add_all([task, child, started_child, source])
    session.flush()
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=started_child.id,
        attempt_no=1,
        status="gateway_call_started",
        gateway_call_started_at=now_value,
    ))
    session.commit()

    first = takeover_task(session, task, now=now_value)
    session.commit()
    second = takeover_task(session, task, now=now_value)
    session.commit()

    assert first.changed is True
    assert second.changed is False
    assert task.type == "search_click"
    assert task.type_config["search_execution_mode"] == "click_only"
    assert "daily_target_count" not in task.type_config
    assert task.account_config["cooldown_per_account_minutes"] == 0
    assert task.pacing_config["max_actions_per_day"] == UNIFIED_TASK_GATE_LIMIT
    assert task.next_run_at == now_value
    assert child.status == "skipped"
    assert child.result["error_code"] == "legacy_membership_retired_by_click_takeover"
    assert child.lease_owner == ""
    assert child.lease_expires_at is None
    assert started_child.status == "executing"
    assert source.status == "skipped"
    assert source.result["error_code"] == "legacy_action_retired_by_fulfillment_takeover"
    assert session.scalar(
        select(func.count(TaskDayLedger.id)).where(TaskDayLedger.task_id == task.id)
    ) == 1
    assert session.scalar(select(func.count(SearchClickFulfillmentObligation.id))) == 3


def test_single_user_scheduling_limits_and_cooldown_are_normalized_idempotently(
    session: Session,
) -> None:
    platform_setting = SchedulingSetting(
        tenant_id=None,
        default_account_hour_limit=20,
        default_account_day_limit=200,
        default_account_cooldown_seconds=180,
    )
    tenant_setting = SchedulingSetting(
        tenant_id=1,
        default_account_hour_limit=50,
        default_account_day_limit=500,
        default_account_cooldown_seconds=30,
    )
    session.add_all([platform_setting, tenant_setting])
    session.flush()

    first = normalize_fulfillment_scheduling_settings(
        session,
        write_audit=True,
    )
    session.flush()
    second = normalize_fulfillment_scheduling_settings(
        session,
        write_audit=True,
    )

    assert first == [
        {"tenant_id": 0, "changed": True, "account_cooldown_seconds": 0},
        {"tenant_id": 1, "changed": True, "account_cooldown_seconds": 0},
    ]
    assert second == [
        {"tenant_id": 0, "changed": False, "account_cooldown_seconds": 0},
        {"tenant_id": 1, "changed": False, "account_cooldown_seconds": 0},
    ]
    for setting in (platform_setting, tenant_setting):
        assert setting.default_account_hour_limit == UNIFIED_TASK_GATE_LIMIT
        assert setting.default_account_day_limit == UNIFIED_TASK_GATE_LIMIT
        assert setting.default_account_cooldown_seconds == 0
    audits = list(session.scalars(
        select(AuditLog).where(
            AuditLog.target_type == "scheduling_setting",
        )
    ))
    assert len(audits) == 2
    assert all(audit.action == "归一单用户履约门禁" for audit in audits)
    assert all("account_cooldown_seconds=0" in audit.detail for audit in audits)


@pytest.mark.parametrize(
    "schema",
    [SchedulingSettingUpdate, RiskControlGlobalPolicyUpdate],
)
def test_global_account_cooldown_cannot_be_reenabled(schema: type) -> None:
    with pytest.raises(ValidationError):
        schema(default_account_cooldown_seconds=1)


def test_partially_stamped_legacy_search_still_retires_old_source(
    session: Session,
) -> None:
    task = Task(
        id="partially-stamped-search",
        tenant_id=1,
        name="半接管旧搜索",
        type="search_join_group",
        status="paused",
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
        type_config={
            "target_operation_target_id": 31,
            "target_input": "https://t.me/target_group",
            "target_title": "目标群",
            "target_link": "https://t.me/target_group",
            "daily_click_target_count": 1,
            "search_bots": [{"username": "jisou", "display_name": "极搜"}],
            "keyword_hashes": ["a" * 64],
            "keyword_text_ciphertexts": ["ciphertext"],
        },
    )
    source = Action(
        id="partially-stamped-source",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="pending",
    )
    session.add_all([task, source])
    session.flush()

    result = takeover_task(session, task)

    assert result.changed is True
    assert task.type == "search_click"
    assert source.status == "skipped"
    assert source.result["error_code"] == (
        "legacy_action_retired_by_fulfillment_takeover"
    )


def test_stamped_search_retires_unbound_failed_gateway_action(
    session: Session,
) -> None:
    now_value = _now()
    task = Task(
        id="stamped-search-with-legacy-retry",
        tenant_id=1,
        name="已接管纯搜索",
        type="search_click",
        status="paused",
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
        type_config={
            "daily_click_target_count": 1,
            "target_operation_target_id": 31,
        },
    )
    action = Action(
        id="legacy-retry-source",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        status="pending",
        payload={},
    )
    session.add_all([task, action])
    session.flush()
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        attempt_no=2,
        status="failed",
        gateway_call_started_at=now_value - timedelta(seconds=2),
        after_call_at=now_value,
        failure_detail="remote failed",
    ))
    session.flush()

    result = takeover_task(session, task, now=now_value)

    assert result.changed is True
    assert action.status == "failed"
    assert action.result["error_code"] == "legacy_action_retired_after_gateway_failed"
    assert action.claim_owner == ""
    assert action.claim_token == ""


def test_task_gate_limits_are_normalized_without_starting_paused_tasks(
    session: Session,
) -> None:
    view = Task(
        id="view-task",
        tenant_id=1,
        name="浏览",
        type="channel_view",
        status="paused",
        pacing_config={"max_actions_per_hour": 2},
        type_config={"target_channel_id": 41, "task_daily_view_safety_cap": 500},
    )
    comment = Task(
        id="comment-task",
        tenant_id=1,
        name="评论",
        type="channel_comment",
        status="stopped",
        pacing_config={"max_actions_per_hour": 3},
        type_config={"target_channel_id": 41, "max_total_comments": 80},
    )
    session.add_all([view, comment])
    session.flush()

    view_result = takeover_task(session, view, now=datetime(2026, 7, 29, 12))
    comment_result = takeover_task(session, comment, now=datetime(2026, 7, 29, 12))

    assert view_result.changed is True
    assert comment_result.changed is True
    assert view.type_config["task_daily_view_safety_cap"] == UNIFIED_TASK_GATE_LIMIT
    assert comment.type_config["max_total_comments"] == UNIFIED_TASK_GATE_LIMIT
    assert view.pacing_config["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    assert comment.pacing_config["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    assert view.status == "paused"
    assert comment.status == "stopped"
    assert session.scalar(select(func.count(TaskDayLedger.id))) == 0


def test_search_takeover_clears_legacy_task_local_cooldown_idempotently(
    session: Session,
) -> None:
    task = Task(
        id="search-with-local-cooldown",
        tenant_id=1,
        name="纯搜索点击",
        type="search_click",
        status="paused",
        account_config={
            "selection_mode": "group",
            "account_group_id": 3,
            "cooldown_per_account_minutes": 5,
        },
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
        type_config={"daily_click_target_count": 1000},
    )
    session.add(task)
    session.flush()

    first = takeover_task(session, task)
    session.flush()
    second = takeover_task(session, task)

    assert first.changed is True
    assert second.changed is False
    assert task.account_config["cooldown_per_account_minutes"] == 0


def test_search_takeover_clears_retired_global_cooldown_error_and_wakes_task(
    session: Session,
) -> None:
    now_value = datetime(2026, 8, 2, 14, 55)
    task = Task(
        id="search-blocked-by-retired-cooldown",
        tenant_id=1,
        name="纯搜索点击",
        type="search_click",
        status="running",
        type_config={
            "daily_click_target_count": 1000,
            "target_operation_target_id": 31,
        },
    )
    session.add(task)
    session.flush()
    takeover_task(session, task, now=now_value)
    session.commit()
    task.last_error = "account_cooldown"
    task.next_run_at = now_value + timedelta(hours=1)
    session.commit()

    recovered = takeover_task(session, task, now=now_value)
    session.flush()
    repeated = takeover_task(session, task, now=now_value)

    assert recovered.changed is True
    assert repeated.changed is False
    assert task.last_error == ""
    assert task.next_run_at == now_value


def test_takeover_wakes_running_task_once_for_soft_pacing_contract(
    session: Session,
) -> None:
    now_value = datetime(2026, 7, 30, 3)
    task = Task(
        id="soft-pacing-search",
        tenant_id=1,
        name="纯搜索点击",
        type="search_click",
        status="running",
        next_run_at=now_value + timedelta(hours=4),
        stats={"fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION},
        type_config={
            "daily_click_target_count": 1,
            "target_operation_target_id": 31,
        },
    )
    session.add(task)
    session.flush()

    first = takeover_task(session, task, now=now_value)
    session.commit()
    task.next_run_at = now_value + timedelta(minutes=5)
    session.commit()
    second = takeover_task(session, task, now=now_value)

    assert first.changed is True
    assert task.stats["fulfillment_soft_pacing_version"] == "nonzero_v1"
    assert second.changed is False
    assert task.next_run_at == now_value + timedelta(minutes=5)


def test_ai_takeover_removes_retired_quantity_and_hour_gates(
    session: Session,
) -> None:
    task = Task(
        id="ai-gate-task",
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        status="paused",
        pacing_config={"max_actions_per_hour": 4},
        type_config={
            "daily_message_target": 20,
            "per_account_daily_min_messages": 1,
            "per_account_daily_max_messages": 2,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 10,
            "hard_hourly_strategy": "force_planning",
        },
        stats={
            "hard_hourly_deficit": 9,
            "hard_hourly_last_blockers": {"account_capacity": 3},
            "coverage_capacity_status": "blocked",
            "coverage_capacity_proof": {
                "blocker_code": "daily_coverage_capacity_insufficient",
            },
            "sendable_coverage_capacity_proof": {"sufficient": False},
        },
    )
    old_action = Action(
        id="old-ai-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="pending",
        payload={"message": "旧计划"},
    )
    started_action = Action(
        id="started-ai-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="executing",
        payload={"message": "已进入 Gateway 的旧计划"},
    )
    session.add_all([task, old_action, started_action])
    session.flush()
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=started_action.id,
        attempt_no=1,
        status="gateway_call_started",
        gateway_call_started_at=_now(),
    ))

    takeover_task(session, task, now=_now())

    assert task.type_config["daily_message_target"] == 20
    assert task.type_config["account_coverage_mode"] == "all_accounts_daily"
    assert RETIRED_AI_QUANTITY_GATE_FIELDS.isdisjoint(task.type_config)
    assert task.pacing_config["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    assert "hard_hourly_deficit" not in task.stats
    assert "hard_hourly_last_blockers" not in task.stats
    assert "coverage_capacity_status" not in task.stats
    assert "coverage_capacity_proof" not in task.stats
    assert "sendable_coverage_capacity_proof" not in task.stats
    assert task.hard_hourly_next_check_at is None
    assert old_action.status == "skipped"
    assert old_action.result["error_code"] == "legacy_action_retired_by_fulfillment_takeover"
    assert started_action.status == "unknown_after_send"
    assert started_action.result["error_code"] == (
        "legacy_action_retired_after_gateway_unknown"
    )


def test_running_like_and_view_actions_are_bound_during_takeover(
    session: Session,
) -> None:
    now_value = _now()
    channel = OperationTarget(
        id=41,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10041",
        title="频道",
    )
    message = ChannelMessage(
        id=51,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=901,
    )
    account = TgAccount(
        id=101,
        tenant_id=1,
        display_name="账号",
        phone_masked="101",
    )
    like_task = Task(
        id="like-task",
        tenant_id=1,
        name="点赞",
        type="channel_like",
        status="running",
    )
    view_task = Task(
        id="running-view-task",
        tenant_id=1,
        name="浏览",
        type="channel_view",
        status="running",
        type_config={"task_daily_view_safety_cap": 500},
    )
    like_action = Action(
        id="like-success",
        tenant_id=1,
        task_id=like_task.id,
        task_type=like_task.type,
        action_type="like_message",
        account_id=account.id,
        status="success",
        executed_at=now_value,
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_target_id": channel.id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
            "reaction_emoji": "👍",
        },
    )
    view_action = Action(
        id="view-pending",
        tenant_id=1,
        task_id=view_task.id,
        task_type=view_task.type,
        action_type="view_message",
        account_id=account.id,
        status="pending",
        scheduled_at=now_value,
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_target_id": channel.id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
        },
    )
    session.add_all(
        [
            channel,
            message,
            account,
            like_task,
            view_task,
            like_action,
            view_action,
        ]
    )
    session.flush()

    like_result = takeover_task(session, like_task, now=now_value)
    view_result = takeover_task(session, view_task, now=now_value)
    session.flush()

    assert like_result.backfilled_fact_count == 1
    assert view_result.bound_action_count == 1
    assert like_action.payload["reaction_fulfillment_obligation_id"]
    assert view_action.payload["view_fulfillment_obligation_id"]
    assert session.scalar(select(func.count(ReactionRemoteFact.id))) == 1
    view_obligation = session.scalar(select(ViewFulfillmentObligation))
    assert view_obligation is not None
    assert view_obligation.current_action_id == view_action.id


def test_like_takeover_does_not_preserve_legacy_sibling_closure(
    session: Session,
) -> None:
    now_value = _now()
    channel = OperationTarget(
        id=41,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10041",
        title="频道",
    )
    message = ChannelMessage(
        id=51,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=901,
    )
    task = Task(
        id="legacy-like-task",
        tenant_id=1,
        name="历史点赞",
        type="channel_like",
        status="running",
    )
    actions = [
        Action(
            id="like-unavailable",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="like_message",
            account_id=101,
            status="skipped",
            scheduled_at=now_value,
            payload={
                "channel_id": channel.tg_peer_id,
                "channel_target_id": channel.id,
                "channel_message_id": message.id,
                "message_id": message.message_id,
                "reaction_emoji": "👍",
            },
            result={
                "error_code": "reaction_unavailable_message",
                "error_message": "该账号不可点赞",
            },
        ),
        Action(
            id="like-legacy-sibling",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="like_message",
            account_id=102,
            status="skipped",
            scheduled_at=now_value,
            payload={
                "channel_id": channel.tg_peer_id,
                "channel_target_id": channel.id,
                "channel_message_id": message.id,
                "message_id": message.message_id,
                "reaction_emoji": "👍",
            },
            result={"error_code": "reaction_unavailable_sibling"},
        ),
    ]
    session.add_all([channel, message, task, *actions])
    session.flush()

    takeover_task(session, task, now=now_value)
    session.flush()

    obligations = session.scalars(
        select(ReactionFulfillmentObligation).where(
            ReactionFulfillmentObligation.task_id == task.id
        )
    ).all()
    assert [(item.account_id, item.status) for item in obligations] == [
        (101, "unavailable")
    ]
    assert "reaction_unavailable_message_ids" not in task.stats


def test_view_takeover_deduplicates_successes_pending_in_same_transaction(
    session: Session,
) -> None:
    now_value = _now()
    channel = OperationTarget(
        id=41,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10041",
        title="频道",
    )
    message = ChannelMessage(
        id=51,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=901,
    )
    account = TgAccount(
        id=101,
        tenant_id=1,
        display_name="浏览账号",
        phone_masked="101",
    )
    task = Task(
        id="duplicate-view-task",
        tenant_id=1,
        name="历史重复浏览",
        type="channel_view",
        status="running",
    )
    payload = {
        "channel_id": channel.tg_peer_id,
        "channel_target_id": channel.id,
        "channel_message_id": message.id,
        "message_id": message.message_id,
    }
    actions = [
        Action(
            id=f"duplicate-view-{index}",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="view_message",
            account_id=account.id,
            status="success",
            executed_at=now_value + timedelta(minutes=index),
            payload=payload,
        )
        for index in range(2)
    ]
    session.add_all([channel, message, account, task, *actions])
    session.flush()

    result = takeover_task(session, task, now=now_value)
    session.flush()

    assert result.backfilled_fact_count == 1
    assert result.duplicate_action_count == 1
    assert session.scalar(select(func.count(ViewRemoteFact.id))) == 1


def test_running_comment_actions_are_migrated_to_remote_fact_obligations(
    session: Session,
) -> None:
    now_value = _now()
    channel = OperationTarget(
        id=42,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10042",
        title="评论频道",
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
        display_name="评论账号",
        phone_masked="102",
    )
    task = Task(
        id="running-comment-task",
        tenant_id=1,
        name="评论",
        type="channel_comment",
        status="running",
        config_revision=3,
        type_config={"target_channel_id": channel.id, "max_total_comments": 80},
    )
    success = Action(
        id="comment-success",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        account_id=account.id,
        status="success",
        executed_at=now_value,
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_target_id": channel.id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
            "comment_mode": "comment",
        },
    )
    pending = Action(
        id="comment-pending",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="post_comment",
        account_id=account.id,
        status="pending",
        scheduled_at=now_value,
        payload={
            "channel_id": channel.tg_peer_id,
            "channel_target_id": channel.id,
            "channel_message_id": message.id,
            "message_id": message.message_id,
            "comment_mode": "reply",
            "reply_to_message_id": 811,
        },
    )
    attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=success.id,
        account_id=account.id,
        attempt_no=1,
        status="success",
        after_call_at=now_value,
        remote_message_id="9902",
    )
    session.add_all([channel, message, account, task, success, pending])
    session.flush()
    session.add(attempt)
    session.flush()

    result = takeover_task(session, task, now=now_value)
    session.flush()

    assert result.bound_action_count == 2
    assert result.backfilled_fact_count == 1
    obligations = list(session.scalars(
        select(CommentFulfillmentObligation)
        .where(CommentFulfillmentObligation.task_id == task.id)
        .order_by(CommentFulfillmentObligation.target_ordinal)
    ))
    assert [item.status for item in obligations] == ["confirmed", "pending"]
    assert obligations[0].remote_comment_id == "9902"
    assert obligations[1].reply_to_message_id == 811
    assert success.payload["comment_fulfillment_obligation_id"] == obligations[0].id
    assert pending.payload["comment_fulfillment_obligation_id"] == obligations[1].id


def test_release_fences_workers_during_fulfillment_takeover() -> None:
    script = (PROJECT_ROOT / "deploy/compose-up.sh").read_text()
    stop_index = script.index('compose stop "${WORKER_SERVICES[@]}"')
    start_index = script.index('compose up -d --no-build --remove-orphans "${WORKER_SERVICES[@]}"')
    ready_index = script.index("manage_shared_dispatch_contract verify-ready")
    takeover_index = script.index("scripts.takeover_all_task_fulfillment")
    activate_index = script.index("manage_shared_dispatch_contract activate")
    assert stop_index < start_index < ready_index < takeover_index < activate_index
    assert "--apply" in script[takeover_index:activate_index]
