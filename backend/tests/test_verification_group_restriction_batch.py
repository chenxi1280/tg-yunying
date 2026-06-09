from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models import VerificationTask
from app.integrations.telegram import SendResult
from app.services.verification import _apply_batch_approval_detail, _verification_send_failure_status
from app.integrations.telegram.gateway import _verification_context_row


def test_batch_approval_detail_marks_blocked_tasks_once():
    task = VerificationTask(
        status="需人工处理",
        failure_detail="目标能力重查未通过：缓存频道不可访问 / 账号无权限。",
    )
    approval = ("需人工处理", "未找到可执行群验证放行的管理员账号", None)

    _apply_batch_approval_detail([task], approval)
    _apply_batch_approval_detail([task], approval)

    assert task.failure_detail == (
        "管理员放行：需人工处理（未找到可执行群验证放行的管理员账号）；"
        "目标能力重查未通过：缓存频道不可访问 / 账号无权限。"
    )


def test_batch_approval_detail_skips_restored_tasks():
    task = VerificationTask(
        status="已处理",
        failure_detail="目标能力重查通过：可发言。",
    )

    _apply_batch_approval_detail([task], ("已执行", "已点击 1 条通过（管理员）验证", 8))

    assert task.failure_detail == "目标能力重查通过：可发言。"


def test_verification_send_failure_status_supports_send_result():
    result = SendResult(False, failure_type="群无权限", detail="账号不可发言")

    assert _verification_send_failure_status(result) == "失败"


@pytest.mark.asyncio
async def test_verification_context_keeps_button_and_media_challenges():
    sender = SimpleNamespace(first_name="验证机器人", username="verify_bot", title="")
    sent_at = datetime(2026, 6, 9, 12, 30)

    async def get_sender():
        return sender

    message = SimpleNamespace(
        id=88,
        message="",
        media=object(),
        buttons=[[SimpleNamespace(text="点击验证")]],
        date=sent_at,
        get_sender=get_sender,
    )

    row = await _verification_context_row(message)

    assert row == {
        "message_id": 88,
        "sender": "验证机器人",
        "text": "[媒体消息] [按钮：点击验证]",
        "sent_at": sent_at,
    }
