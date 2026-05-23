from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AccountStatus, OperationIssue, OperationIssueAccount, OperationIssueSource, OperationTarget, Task, Tenant, TgAccount
from app.models.enums import FailureType
from app.services._common import _now
from app.services.runtime_summary import acknowledge_operation_issue, claim_operation_issue, get_operation_issue_detail, list_operation_issues, refresh_task_summary


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_failed_task_summary_opens_and_resolves_operation_issue() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=21, tenant_id=1, target_type="channel", tg_peer_id="-10021", title="频道", can_send=True))
        session.add(TgAccount(id=11, tenant_id=1, display_name="账号", phone_masked="11", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.add(
            Task(
                id="task-issue",
                tenant_id=1,
                name="评论任务",
                type="channel_comment",
                status="running",
                account_config={"account_ids": [11]},
                type_config={"target_operation_target_id": 21},
            )
        )
        action = Action(
            id="action-failed",
            tenant_id=1,
            task_id="task-issue",
            task_type="channel_comment",
            action_type="post_comment",
            account_id=11,
            status="failed",
            scheduled_at=now,
            executed_at=now,
            result={"failure_type": FailureType.COMMENT_UNAVAILABLE.value, "failure_detail": "评论区不可用"},
        )
        session.add(action)
        session.commit()

        refresh_task_summary(session, session.get(Task, "task-issue"))
        issue = session.scalar(select(OperationIssue).where(OperationIssue.source_task_id == "task-issue"))
        detail = get_operation_issue_detail(session, 1, issue.id)

        assert issue.status == "open"
        assert issue.target_id == 21
        assert issue.affected_account_ids == [11]
        assert issue.affected_account_count == 1
        assert issue.affected_task_count == 1
        assert issue.handling_mode == "drawer"
        assert issue.return_to["source_issue_id"] == issue.id
        assert issue.return_to["target_id"] == 21
        assert issue.return_to["task_id"] == "task-issue"
        assert detail["sources"][0].source_id == "action-failed"
        assert detail["sources"][0].source_type == "action"
        assert detail["issue_accounts"][0].account_id == 11
        assert detail["recent_failed_actions"][0]["id"] == "action-failed"
        assert session.query(OperationIssueSource).filter_by(issue_id=issue.id).count() == 2
        assert session.query(OperationIssueAccount).filter_by(issue_id=issue.id).count() == 1

        action.status = "success"
        action.result = {}
        refresh_task_summary(session, session.get(Task, "task-issue"))
        session.refresh(issue)

        assert list_operation_issues(session, 1, status="open") == []
        assert issue.status == "resolved"


def test_acknowledge_operation_issue_moves_issue_out_of_open_status() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=22, tenant_id=1, target_type="group", tg_peer_id="-10022", title="群", can_send=True))
        session.add(
            OperationIssue(
                id="issue-ack",
                tenant_id=1,
                target_id=22,
                issue_type="task_failure",
                severity="warning",
                failure_type="send_failed",
                status="open",
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        session.commit()

        issue = acknowledge_operation_issue(session, 1, "issue-ack", "tester", "已安排处理")
        session.flush()

        assert issue.status == "acknowledged"
        assert issue.summary["acknowledged_by"] == "tester"
        assert issue.claimed_by == ""
        assert list_operation_issues(session, 1, status="open") == []


def test_claim_operation_issue_sets_owner_without_acknowledging() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationIssue(
                id="issue-claim",
                tenant_id=1,
                target_id=22,
                issue_type="task_failure",
                severity="warning",
                failure_type="send_failed",
                status="open",
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        session.commit()

        issue = claim_operation_issue(session, 1, "issue-claim", "tester", "接手排查")
        session.flush()

        assert issue.status == "open"
        assert issue.claimed_by == "tester"
        assert issue.claimed_at is not None
        assert issue.summary["claimed_by"] == "tester"
        assert issue.summary["claim_reason"] == "接手排查"
