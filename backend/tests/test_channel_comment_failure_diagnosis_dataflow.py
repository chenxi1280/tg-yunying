from datetime import datetime

from app.models import Action
from app.services.task_center.service import _action_payload


def _comment_action(error_code: str, error_message: str) -> Action:
    return Action(
        id=f"action-{error_code}",
        tenant_id=1,
        task_id="task-channel-comment",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=11,
        scheduled_at=datetime(2026, 1, 1, 10, 0, 0),
        status="skipped",
        payload={"channel_target_id": 31, "channel_message_id": 41},
        result={"error_code": error_code, "error_message": error_message},
    )


def test_channel_comment_failure_diagnosis_classifies_recoverable_membership_gap():
    payload = _action_payload(
        _comment_action("comment_membership_required", "账号未关注 / 加入目标频道，等待准入后继续评论"),
    )

    diagnosis = payload["failure_diagnosis"]
    assert diagnosis["category"] == "comment_membership_required"
    assert diagnosis["scope"] == "account_channel_membership"
    assert diagnosis["operator_summary"] == "等待账号关注 / 加入频道后继续评论"
    assert "准入" in diagnosis["suggested_action"]


def test_channel_comment_failure_diagnosis_classifies_account_permission_gap():
    payload = _action_payload(
        _comment_action("comment_account_permission_denied", "该账号对频道评论区不可发言：群无权限或账号不可发言"),
    )

    diagnosis = payload["failure_diagnosis"]
    assert diagnosis["category"] == "comment_account_permission_denied"
    assert diagnosis["scope"] == "account_channel_comment"
    assert diagnosis["operator_summary"] == "该账号对频道评论区不可发言"
    assert "其他账号" in diagnosis["suggested_action"]


def test_channel_comment_failure_diagnosis_classifies_message_unavailable():
    payload = _action_payload(
        _comment_action("comment_unavailable_message", "该消息无法评论：频道帖子无法解析到评论区"),
    )

    diagnosis = payload["failure_diagnosis"]
    assert diagnosis["category"] == "comment_unavailable_message"
    assert diagnosis["scope"] == "channel_message"
    assert diagnosis["operator_summary"] == "该消息无法评论"
    assert "频道未绑定讨论组" in diagnosis["suggested_action"]


def test_channel_comment_failure_diagnosis_preserves_unknown_raw_errors():
    payload = _action_payload(
        _comment_action("telegram_rpc_error", "RPC_CALL_FAIL: raw telegram error"),
    )

    diagnosis = payload["failure_diagnosis"]
    assert diagnosis["category"] == "unknown"
    assert payload["failure_type"] == "telegram_rpc_error"
    assert payload["failure_reason"] == "RPC_CALL_FAIL: raw telegram error"
