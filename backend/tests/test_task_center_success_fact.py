from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.models import Action, TgAccount
from app.services.task_center.dispatcher import _apply_send_result, _group_bot_admission_wait_message
from app.services.task_center.service import _action_payload

pytestmark = pytest.mark.no_postgres
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _success_action() -> Action:
    return Action(
        id="action-success-fact",
        tenant_id=1,
        task_id="task-success-fact",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=datetime(2026, 7, 27, 10, 0, 0),
        result={
            "success": False,
            "error_code": "required_channel_admission_pending",
            "error_message": "账号需要先关注必需频道并复检群发言权限",
            "validation_stage": "required_channel_follow",
        },
    )


def test_successful_send_clears_transient_admission_error() -> None:
    action = _success_action()
    account = TgAccount(id=11, tenant_id=1, display_name="sender", phone_masked="+100")

    _apply_send_result(action, account, True, remote_id="9001")

    assert action.status == "success"
    assert action.result["telegram_msg_id"] == "9001"
    assert action.result["success"] is True
    assert "error_code" not in action.result
    assert "error_message" not in action.result


def test_success_payload_hides_legacy_failure_diagnosis() -> None:
    action = _success_action()
    action.status = "success"
    action.result = {**(action.result or {}), "success": True, "telegram_msg_id": "9001"}

    payload = _action_payload(action)

    assert payload["failure_type"] == ""
    assert payload["failure_reason"] == ""
    assert payload["failure_diagnosis"] == {}


def test_frontend_success_result_precedes_stale_error_message() -> None:
    source = (REPOSITORY_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text(encoding="utf-8")
    start = source.index("export function actionResult")
    body = source[start:source.index("export function isPlannedAction", start)]

    assert body.index("action.status === 'success'") < body.index("action.result?.error_message")


def test_group_bot_wait_message_does_not_claim_channel_follow_without_rule() -> None:
    assert "关注" not in _group_bot_admission_wait_message("group_bot_policy_unresolved")
    assert "观察证据不足" in _group_bot_admission_wait_message("observation_stale")
    assert "频道" in _group_bot_admission_wait_message("required_channel_follow_pending")
