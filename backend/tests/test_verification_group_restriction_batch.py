import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
    VerificationTask,
)
from app.security import encrypt_session
from app.integrations.telegram import SendResult
from app.services.verification import (
    _apply_batch_approval_detail,
    _mark_image_verification_if_needed,
    _upgrade_existing_verification_task,
    _verification_action_for_group_restriction,
    _verification_send_failure_status,
    confirm_verification_task,
    resolve_group_restriction_task,
)
from app.integrations.telegram import OperationResult
from app.integrations.telegram.gateway import _verification_context_row
from app.services.task_center import daily_coverage


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

def _seed_paused_operation_target_coverage(
    session: Session,
    initial_at: datetime,
    cursor_at: datetime,
) -> Task:
    session.add_all([
        Tenant(id=1, name="默认运营空间"),
        AccountPool(id=10, tenant_id=1, name="普通", pool_purpose="normal", is_enabled=True),
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="青岛师范学院", can_send=False),
        OperationTarget(
            id=90,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-1007",
            title="青岛师范学院",
        ),
        Task(
            id="coverage-task", tenant_id=1, name="覆盖任务", type="group_ai_chat", status="paused",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 90, "account_coverage_mode": "all_accounts_daily"},
        ),
    ])
    session.add_all([
        TgAccount(
            id=11, tenant_id=1, pool_id=10, display_name="受限账号", phone_masked="11",
            status="在线", session_ciphertext=encrypt_session("session-11"),
        ),
        TgAccount(id=12, tenant_id=1, pool_id=10, display_name="游标账号", phone_masked="12", status="在线"),
        TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=False),
        TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
        TaskMembershipAdmissionItem(
            id=1, tenant_id=1, task_id="coverage-task", account_id=11, target_id=90, phase="failed",
        ),
        TaskAccountDailyCoverage(
            id="coverage-restricted", tenant_id=1, task_id="coverage-task", group_id=7, account_id=11,
            membership_item_id=1, coverage_date=initial_at.date(), target_count=1,
            state="blocked", blocker_code="cannot_send", targeted_at=initial_at,
        ),
        TaskAccountDailyCoverage(
            id="coverage-cursor", tenant_id=1, task_id="coverage-task", group_id=7, account_id=12,
            coverage_date=initial_at.date(), target_count=1, state="ready", targeted_at=cursor_at,
        ),
        VerificationTask(
            id=101, tenant_id=1, account_id=11, group_id=7, verification_type="群发言不可用",
            detected_reason="群无权限或账号不可发言", suggested_action="人工处理", status="需人工处理",
        ),
    ])
    session.flush()
    return session.get(Task, "coverage-task")


@pytest.mark.no_postgres
def test_group_permission_recheck_requeues_all_account_coverage(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    initial_at = datetime(2026, 7, 13, 11, 0)
    cursor_at = datetime(2026, 7, 13, 11, 1)
    recovered_at = datetime(2026, 7, 13, 11, 2)
    monkeypatch.setattr("app.services.verification._now", lambda: recovered_at)
    monkeypatch.setattr("app.services.verification.credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(
        "app.services.verification.gateway.probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="group:target:可访问"),
    )

    with Session(engine) as session:
        task = _seed_paused_operation_target_coverage(session, initial_at, cursor_at)
        cursor_row = session.get(TaskAccountDailyCoverage, "coverage-cursor")
        daily_coverage.advance_coverage_plan_cursor(session, task, cursor_row, now=cursor_at)
        cursor_row.state = "reserved"
        session.commit()

        resolved = resolve_group_restriction_task(session, 101, "tester")
        coverage = session.get(TaskAccountDailyCoverage, "coverage-restricted")
        batch = daily_coverage.ready_coverage_plan_batch(session, task, now=recovered_at, limit=20)

    assert resolved.status == "已处理"
    assert coverage.state == "ready"
    assert coverage.targeted_at == recovered_at
    assert [row.account_id for row in batch.rows] == [11]


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
