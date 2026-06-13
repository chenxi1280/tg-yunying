from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant, TgAccount, TgGroup, VerificationTask
from app.services._common import _now
from app.services.task_center.dispatcher import _group_send_verification_action
from app.services.task_center.service import get_task_detail, list_membership_items_page


def test_membership_items_page_projects_action_and_verification_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-membership-items", tenant_id=1, name="天津", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="目标群"))
        session.add_all(
            [
                TgAccount(id=11, tenant_id=1, display_name="按钮账号", phone_masked="+861***0011", status="在线", session_ciphertext="cipher-11"),
                TgAccount(id=12, tenant_id=1, display_name="人工账号", phone_masked="+861***0012", status="在线", session_ciphertext="cipher-12"),
                TgAccount(id=13, tenant_id=1, display_name="就绪账号", phone_masked="+861***0013", status="在线", session_ciphertext="cipher-13"),
                TgAccount(id=14, tenant_id=1, display_name="图形验证码账号", phone_masked="+861***0014", status="在线", session_ciphertext="cipher-14"),
            ]
        )
        session.add_all(
            [
                Action(
                    id="membership-button",
                    tenant_id=1,
                    task_id="task-membership-items",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=11,
                    status="skipped",
                    scheduled_at=now_value,
                    result={"membership_status": "permission_denied", "error_message": "需要点击按钮完成验证"},
                    payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
                ),
                Action(
                    id="membership-manual",
                    tenant_id=1,
                    task_id="task-membership-items",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=12,
                    status="skipped",
                    scheduled_at=now_value,
                    result={"membership_status": "permission_denied", "error_message": "等待管理员审批"},
                    payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
                ),
                Action(
                    id="membership-captcha",
                    tenant_id=1,
                    task_id="task-membership-items",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=14,
                    status="executing",
                    scheduled_at=now_value,
                    result={"membership_status": "permission_denied", "error_message": "需要群管理 bot 的验证码"},
                    payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
                ),
                Action(
                    id="membership-ready",
                    tenant_id=1,
                    task_id="task-membership-items",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=13,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    result={"success": True, "membership_status": "joined"},
                    payload={"channel_id": "-1007", "channel_target_id": 21, "target_type": "group", "target_display": "目标群", "require_send": True},
                ),
            ]
        )
        session.add_all(
            [
                VerificationTask(
                    tenant_id=1,
                    account_id=14,
                    group_id=7,
                    verification_type="群发言权限",
                    detected_reason="需要群管理 bot 的验证码",
                    suggested_action="识别图形验证码",
                    target_peer_id="-1007",
                    target_display="目标群",
                    status="待处理",
                ),
                VerificationTask(
                    tenant_id=1,
                    account_id=11,
                    group_id=7,
                    verification_type="群发言权限",
                    detected_reason="需要点击按钮完成验证",
                    suggested_action="点击按钮",
                    target_peer_id="-1007",
                    target_display="目标群",
                    status="待处理",
                ),
                VerificationTask(
                    tenant_id=1,
                    account_id=12,
                    group_id=7,
                    verification_type="群发言权限",
                    detected_reason="等待管理员审批",
                    suggested_action="人工处理",
                    target_peer_id="-1007",
                    target_display="目标群",
                    status="需人工处理",
                ),
            ]
        )
        session.commit()

        rows, total = list_membership_items_page(session, 1, "task-membership-items", page=1, page_size=10)
        assert total == 4
        by_account = {row["account_id"]: row for row in rows}
        assert by_account[11]["phase"] == "challenge_required"
        assert by_account[11]["verification_action"] == "点击按钮"
        assert by_account[11]["can_auto_resolve"] is True
        assert by_account[12]["phase"] == "manual_required"
        assert by_account[12]["manual_required"] is True
        assert by_account[13]["phase"] == "ready"
        assert by_account[13]["can_send"] is True
        assert by_account[14]["phase"] == "captcha_solving"
        assert by_account[14]["verification_action"] == "识别图形验证码"
        assert by_account[14]["can_auto_resolve"] is True

        manual_rows, manual_total = list_membership_items_page(session, 1, "task-membership-items", manual_required=True, page=1, page_size=10)
        assert manual_total == 1
        assert manual_rows[0]["account_id"] == 12

        challenge_rows, challenge_total = list_membership_items_page(session, 1, "task-membership-items", phase="challenge_required", page=1, page_size=10)
        assert challenge_total == 1
        assert challenge_rows[0]["account_id"] == 11


def test_group_send_verification_action_detects_captcha_text() -> None:
    action = _group_send_verification_action("加入时提示需要群管理 bot 的验证码")

    assert action == "识别图形验证码"
    assert _group_send_verification_action("未解析到群关联频道") == "识别图形验证码"
    assert _group_send_verification_action("批量重查发现账号仍未获群发言权限") == "识别图形验证码"
    assert _group_send_verification_action("验证码：请输入 1234") == "发送验证回复"


def test_membership_items_page_is_not_capped_by_detail_action_limit() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-large-membership", tenant_id=1, name="天津", type="group_ai_chat", status="running"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
        session.add_all(
            [
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=f"+861***{account_id:04d}",
                    status="在线",
                    session_ciphertext=f"cipher-{account_id}",
                )
                for account_id in range(1, 506)
            ]
        )
        session.add_all(
            [
                Action(
                    id=f"membership-{account_id}",
                    tenant_id=1,
                    task_id="task-large-membership",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=account_id,
                    status="pending",
                    scheduled_at=now_value,
                    payload={"channel_target_id": 21, "target_type": "group", "target_display": "目标群"},
                )
                for account_id in range(1, 506)
            ]
        )
        session.commit()

        rows, total = list_membership_items_page(session, 1, "task-large-membership", page=6, page_size=100)

        assert total == 505
        assert len(rows) == 5


def test_target_admission_retry_detail_defers_membership_rows_to_page_api() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-admission-retry",
                tenant_id=1,
                name="重试目标准入",
                type="target_admission_retry",
                status="running",
            )
        )
        session.add(OperationTarget(id=21, tenant_id=1, target_type="group", tg_peer_id="-1007", title="目标群"))
        session.add_all(
            [
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=f"+861***{account_id:04d}",
                    status="在线",
                )
                for account_id in range(1, 4)
            ]
        )
        session.add_all(
            [
                Action(
                    id=f"retry-membership-{account_id}",
                    tenant_id=1,
                    task_id="task-admission-retry",
                    task_type="target_admission_retry",
                    action_type="ensure_target_membership",
                    account_id=account_id,
                    status="pending",
                    scheduled_at=now_value,
                    payload={"channel_target_id": 21, "target_type": "group", "target_display": "目标群"},
                )
                for account_id in range(1, 4)
            ]
        )
        session.commit()

        detail = get_task_detail(session, 1, "task-admission-retry")

        assert detail["stats"]["total_actions"] == 3
        assert detail["stats"]["pending_count"] == 3
        assert detail["membership_phase"]["pending_account_count"] == 3
        assert detail["membership_accounts"] == []


def test_membership_items_page_tolerates_legacy_bad_target_id() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-bad-target", tenant_id=1, name="旧数据", type="group_ai_chat", status="running"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="历史账号", phone_masked="+861***0011", status="在线", session_ciphertext="cipher-11"))
        session.add(
            Action(
                id="membership-bad-target",
                tenant_id=1,
                task_id="task-bad-target",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=11,
                status="failed",
                scheduled_at=now_value,
                result={"error_message": "历史目标数据异常"},
                payload={"channel_target_id": "bad-target", "target_type": "group", "target_display": "历史群"},
            )
        )
        session.commit()

        rows, total = list_membership_items_page(session, 1, "task-bad-target", page=1, page_size=10)

        assert total == 1
        assert rows[0]["target_id"] is None
        assert rows[0]["failure_detail"] == "历史目标数据异常"
