import asyncio
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.integrations.telegram import SendResult
from app.services.verification import (
    _apply_batch_approval_detail,
    _mark_image_verification_if_needed,
    _upgrade_existing_verification_task,
    _verification_action_for_group_restriction,
    _verification_send_failure_status,
    confirm_verification_task,
)
from app.integrations.telegram import OperationResult
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


def test_group_restriction_recheck_uses_image_verification_for_retryable_reasons():
    assert _verification_action_for_group_restriction("批量重查发现账号仍未获群发言权限") == "识别图形验证码"
    assert _verification_action_for_group_restriction("未解析到群关联频道") == "识别图形验证码"
    assert _verification_action_for_group_restriction("群无权限或账号不可发言") == "识别图形验证码"


def test_mark_image_verification_if_needed_supports_group_permission_reasons():
    task = VerificationTask(
        detected_reason="批量重查发现账号仍未获群发言权限",
        suggested_action="人工处理",
    )

    _mark_image_verification_if_needed(task, "未解析到群关联频道")

    assert task.suggested_action == "识别图形验证码"


def test_existing_manual_verification_task_upgrades_to_auto_action():
    task = VerificationTask(
        detected_reason="旧人工原因",
        suggested_action="人工处理",
        status="需人工处理",
        failure_detail="旧失败详情",
        handled_at=datetime(2026, 6, 9, 12, 0),
        target_peer_id="@old",
        target_display="旧目标",
    )

    _upgrade_existing_verification_task(
        task,
        "未解析到群关联频道",
        "识别图形验证码",
        "@qdsfxy",
        "青岛师范学院",
    )

    assert task.detected_reason == "未解析到群关联频道"
    assert task.suggested_action == "识别图形验证码"
    assert task.status == "待处理"
    assert task.failure_detail == ""
    assert task.handled_at is None
    assert task.target_peer_id == "@qdsfxy"
    assert task.target_display == "青岛师范学院"


def test_existing_auto_verification_task_refreshes_stale_target_ref_reason():
    task = VerificationTask(
        detected_reason="群无权限或账号不可发言",
        suggested_action="关注频道",
        status="失败",
        failure_detail="目标实体无法解析，请重新同步账号群聊/运营目标后再试",
        handled_at=datetime(2026, 6, 15, 12, 0),
        target_peer_id="-1003583171851",
        target_display="天津音乐学院",
    )

    _upgrade_existing_verification_task(
        task,
        "群无权限或账号不可发言",
        "识别图形验证码",
        "-1003583171851",
        "天津音乐学院",
    )

    assert task.suggested_action == "识别图形验证码"
    assert task.status == "待处理"
    assert task.failure_detail == ""
    assert task.handled_at is None


def test_confirm_verification_task_runs_auto_image_for_manual_required(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[int] = []

    def fake_auto_image(session, task, account, credentials, *, reader_candidates=None):
        calls.append(task.id)
        return OperationResult(True, "已处理", detail="MiMo 已识别并提交验证码")

    monkeypatch.setattr("app.services.verification.credentials_for_account", lambda *_args: object())
    monkeypatch.setattr("app.services.verification.auto_resolve_image_verification", fake_auto_image)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="验证码账号", phone_masked="11", status="在线", session_ciphertext="cipher"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="青岛师范学院", can_send=False))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=False))
        verification = VerificationTask(
            id=101,
            tenant_id=1,
            account_id=11,
            group_id=7,
            verification_type="群发言权限",
            detected_reason="群无权限或账号不可发言",
            suggested_action="识别图形验证码",
            target_peer_id="-1007",
            target_display="青岛师范学院",
            status="需人工处理",
        )
        session.add(verification)
        session.commit()

        updated = confirm_verification_task(session, 101, "tester")
        link = session.query(TgGroupAccount).filter_by(group_id=7, account_id=11).one()

    assert calls == [101]
    assert updated.status == "已处理"
    assert updated.failure_detail == "MiMo 已识别并提交验证码"
    assert link.can_send is True


def test_verification_context_keeps_button_and_media_challenges():
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

    row = asyncio.run(_verification_context_row(message))

    assert row is not None
    media_fingerprint = row.pop("media_fingerprint")
    assert media_fingerprint
    assert row == {
        "message_id": 88,
        "sender": "验证机器人",
        "text": "[媒体消息] [按钮：点击验证]",
        "sent_at": sent_at,
        "has_media": True,
        "media_message_id": 88,
        "media_mime_type": "",
    }


def test_verification_context_keeps_pure_media_challenge():
    sender = SimpleNamespace(first_name="验证机器人", username="verify_bot", title="")
    sent_at = datetime(2026, 6, 9, 12, 31)

    async def get_sender():
        return sender

    message = SimpleNamespace(
        id=89,
        message="",
        media=object(),
        buttons=[],
        date=sent_at,
        get_sender=get_sender,
    )

    row = asyncio.run(_verification_context_row(message))

    assert row is not None
    assert row["text"] == "[媒体消息]"
    assert row["has_media"] is True
    assert row["media_message_id"] == 89
