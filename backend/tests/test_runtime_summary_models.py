from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AccountStatus, OperationTarget, Task, Tenant, TgAccount, TgAccountSecurityBatchItem, TgAccountSecuritySnapshot
from app.services._common import _now
from app.services.runtime_summary import refresh_account_summary, refresh_target_summary, refresh_task_summary


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_account_runtime_summary_uses_security_snapshot_and_security_retry() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=11,
                tenant_id=1,
                display_name="安全待处理账号",
                phone_masked="11",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(
            TgAccountSecuritySnapshot(
                tenant_id=1,
                account_id=11,
                trusted_session_status="missing",
                two_fa_status="pending_email_confirmation",
                external_authorization_count=1,
                profile_status="incomplete",
            )
        )
        session.add(
            TgAccountSecurityBatchItem(
                tenant_id=1,
                batch_id=101,
                account_id=11,
                status="waiting",
                next_retry_at=now + timedelta(minutes=20),
                failure_type="email_confirmation_required",
                failure_detail="等待邮箱确认",
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 11)

    assert summary.send_available is False
    assert summary.listen_available is False
    assert summary.join_available is False
    assert summary.comment_available is False
    assert summary.unavailable_reason == "平台可信设备无法确认"
    assert summary.next_retry_at == now + timedelta(minutes=20)
    assert summary.failure_trend["security_blocked"] is True
    assert summary.failure_trend["trusted_session_status"] == "missing"
    assert summary.failure_trend["two_fa_status"] == "pending_email_confirmation"
    assert "外部登录设备" in summary.failure_trend["security_risk_reason"]


def test_account_runtime_summary_surfaces_recent_risk_preflight_result() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=12,
                tenant_id=1,
                display_name="近期风控账号",
                phone_masked="12",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-risk", tenant_id=1, name="风控任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-risk",
                tenant_id=1,
                task_id="task-risk",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=12,
                status="skipped",
                scheduled_at=now,
                created_at=now,
                result={
                    "decision": "warn",
                    "risk_level": "D",
                    "decision_reasons": ["account_limited"],
                    "suggested_actions": ["降低频率后再试"],
                },
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 12)

    assert summary.send_available is True
    assert summary.failure_trend["recent_risk_decision"] == "warn"
    assert summary.failure_trend["recent_risk_level"] == "D"
    assert summary.failure_trend["recent_risk_reason"] == "account_limited"


def test_target_summary_does_not_match_target_id_by_json_substring() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                OperationTarget(id=2, tenant_id=1, target_type="channel", tg_peer_id="-1002", title="目标 2", can_send=True),
                OperationTarget(id=12, tenant_id=1, target_type="channel", tg_peer_id="-10012", title="目标 12", can_send=True),
            ]
        )
        session.add(Task(id="task-target-12", tenant_id=1, name="目标 12 任务", type="channel_like", status="running", type_config={"target_operation_target_id": 12}))
        session.add(
            Action(
                id="action-target-12-failed",
                tenant_id=1,
                task_id="task-target-12",
                task_type="channel_like",
                action_type="like_message",
                status="failed",
                scheduled_at=now,
                executed_at=now,
                result={"failure_type": "reaction_failed"},
            )
        )
        session.commit()

        refresh_task_summary(session, session.get(Task, "task-target-12"))
        summary = refresh_target_summary(session, 1, 2)

        assert summary.failed_action_count == 0
        assert summary.latest_failure_at is None
