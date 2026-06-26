from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AccountStatus, OperationTarget, Task, TaskRuntimeSummary, Tenant, TgAccount, TgAccountSecurityBatchItem, TgAccountSecuritySnapshot
from app.models.enums import FailureType
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
    assert summary.health_score < 90
    assert summary.risk_level == "E"
    assert any("外部登录设备" in reason for reason in summary.score_reasons)


def test_task_runtime_summary_uses_unknown_after_send_as_latest_failure() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="异常群", can_send=True))
        task = Task(
            id="task-runtime-unknown",
            tenant_id=1,
            name="结果未知任务",
            type="group_ai_chat",
            status="running",
            type_config={"target_operation_target_id": 31},
        )
        session.add(task)
        session.add(
            Action(
                id="action-runtime-unknown",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                status="unknown_after_send",
                scheduled_at=now,
                executed_at=now,
                result={"error_code": "unknown_after_send", "error_message": "已进入 Gateway 调用边界但本地结果未知"},
            )
        )
        session.commit()

        summary = refresh_task_summary(session, task)

    assert summary.pending_count == 1
    assert summary.latest_failure_type == "unknown_after_send"


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


def test_account_runtime_summary_ignores_target_permission_failures_as_recent_risk() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=13,
                tenant_id=1,
                display_name="目标权限失败账号",
                phone_masked="13",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-comment", tenant_id=1, name="评论任务", type="channel_comment", status="running"))
        session.add(
            Action(
                id="action-comment-denied",
                tenant_id=1,
                task_id="task-comment",
                task_type="channel_comment",
                action_type="post_comment",
                account_id=13,
                status="failed",
                scheduled_at=now,
                created_at=now,
                result={
                    "failure_type": FailureType.COMMENT_UNAVAILABLE.value,
                    "error_message": "无评论权限，未通过群限制发言",
                },
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 13)

    assert "recent_risk_reason" not in summary.failure_trend
    assert summary.health_score == 90
    assert summary.score_reasons == []
    assert "无评论权限，未通过群限制发言" in summary.non_score_reasons


def test_account_runtime_summary_does_not_score_target_permission_blocker_text() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=16,
                tenant_id=1,
                display_name="群权限账号",
                phone_masked="16",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-group-permission", tenant_id=1, name="群活跃", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-group-permission-denied",
                tenant_id=1,
                task_id="task-group-permission",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=16,
                status="failed",
                scheduled_at=now,
                created_at=now,
                result={
                    "failure_type": FailureType.GROUP_PERMISSION_DENIED.value,
                    "blockers": ["账号未通过群限制发言"],
                    "error_message": "账号没有该目标发言权限",
                },
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 16)

    assert "recent_risk_reason" not in summary.failure_trend
    assert summary.health_score == 90
    assert summary.score_reasons == []
    assert "账号没有该目标发言权限" in summary.non_score_reasons


def test_account_runtime_summary_ignores_task_failures_as_recent_risk() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=15,
                tenant_id=1,
                display_name="任务失败账号",
                phone_masked="15",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-unknown", tenant_id=1, name="发送任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-task-unknown",
                tenant_id=1,
                task_id="task-unknown",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=15,
                status="failed",
                scheduled_at=now,
                created_at=now,
                result={"failure_type": "unknown", "error_message": "任务执行失败"},
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 15)

    assert "recent_risk_reason" not in summary.failure_trend
    assert summary.health_score == 90
    assert summary.score_reasons == []
    assert "任务执行失败" in summary.non_score_reasons


def test_account_runtime_summary_does_not_score_target_slowmode() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=17,
                tenant_id=1,
                display_name="目标慢速账号",
                phone_masked="17",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-slowmode", tenant_id=1, name="群活跃", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-target-slowmode",
                tenant_id=1,
                task_id="task-slowmode",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=17,
                status="failed",
                scheduled_at=now,
                created_at=now,
                result={
                    "failure_type": FailureType.SLOWMODE.value,
                    "error_message": "群慢速模式，需要等待 60 秒",
                },
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 17)

    assert "rate_limit_count" not in summary.failure_trend
    assert "recent_risk_reason" not in summary.failure_trend
    assert summary.health_score == 90
    assert summary.score_reasons == []
    assert "群慢速模式，需要等待 60 秒" in summary.non_score_reasons


def test_account_runtime_summary_keeps_account_level_failures_as_recent_risk() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=14,
                tenant_id=1,
                display_name="账号限流账号",
                phone_masked="14",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
                health_score=90,
            )
        )
        session.add(Task(id="task-send", tenant_id=1, name="发送任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-flood-wait",
                tenant_id=1,
                task_id="task-send",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=14,
                status="failed",
                scheduled_at=now,
                created_at=now,
                result={"failure_type": FailureType.FLOOD_WAIT.value, "error_message": "FloodWait 120 秒"},
            )
        )
        session.commit()

        summary = refresh_account_summary(session, 1, 14)

    assert summary.failure_trend["recent_risk_reason"] == FailureType.FLOOD_WAIT.value
    assert summary.health_score < 90
    assert any(FailureType.FLOOD_WAIT.value in reason for reason in summary.score_reasons)


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


def test_target_summary_ignores_deleted_task_runtime_summary() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=24, tenant_id=1, target_type="group", tg_peer_id="-10024", title="群", can_send=True))
        session.add(
            Task(
                id="task-deleted-summary",
                tenant_id=1,
                name="已删任务",
                type="group_ai_chat",
                status="deleted",
                deleted_at=now,
                type_config={"target_operation_target_id": 24},
            )
        )
        session.add(
            TaskRuntimeSummary(
                tenant_id=1,
                task_id="task-deleted-summary",
                target_id=24,
                task_status="running",
                planned_count=20,
                failed_count=7,
                pending_count=5,
                oldest_pending_at=now - timedelta(days=10),
            )
        )
        session.commit()

        summary = refresh_target_summary(session, 1, 24)

        assert summary.failed_action_count == 0
        assert summary.affected_task_count == 0
        assert summary.summary["task_count"] == 0
