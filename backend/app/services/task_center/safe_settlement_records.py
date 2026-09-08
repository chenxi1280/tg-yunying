"""Explicit records for actions proven unexecuted before the Telegram Gateway."""
from datetime import datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import Action, ExecutionAttempt


def _safe_settlement_result(
    action: Action,
    *,
    reason_code: str,
    detail: str,
    effective_at: datetime | None,
) -> dict:
    result = {
        **(action.result or {}),
        "success": False,
        "error_code": reason_code,
        "error_message": detail,
        "remote_mutation_started": False,
        "pre_gateway_safe_settlement": {"reason_code": reason_code},
    }
    if reason_code == "pacing_claim_deadline_exceeded":
        result[reason_code] = {
            "effective_claim_at": effective_at.isoformat() if effective_at else None,
        }
    return result


def _safe_shortfall_attempt(
    session: Session,
    action: Action,
    now: datetime,
    *,
    reason_code: str,
    detail: str,
) -> ExecutionAttempt:
    attempt_no = session.scalar(select(func.max(ExecutionAttempt.attempt_no)).where(
        ExecutionAttempt.action_id == action.id,
    )) or 0
    return ExecutionAttempt(
        tenant_id=action.tenant_id,
        action_id=action.id,
        task_lifecycle_epoch=int(action.task_lifecycle_epoch or 1),
        account_id=action.account_id,
        attempt_no=int(attempt_no) + 1,
        status="failed",
        before_call_at=now,
        after_call_at=now,
        failure_type=reason_code,
        failure_detail=detail,
        result_snapshot={"remote_mutation_started": False},
    )


