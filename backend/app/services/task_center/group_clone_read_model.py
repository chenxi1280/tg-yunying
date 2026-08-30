from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, RemoteReconcileCase, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneManualReviewDecision,
    CloneMessagePart,
    CloneSourceStreamState,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now


def message_mapping_items(
    session: Session,
    task: Task,
    *,
    limit: int,
    before_source_message_id: int | None,
    before_id: str | None,
) -> list[dict]:
    statement = select(CloneMessagePart).where(
        CloneMessagePart.task_id == task.id,
        CloneMessagePart.tenant_id == task.tenant_id,
    )
    if before_source_message_id is not None:
        cursor_clause = CloneMessagePart.source_message_id < before_source_message_id
        if before_id:
            cursor_clause = or_(
                cursor_clause,
                and_(
                    CloneMessagePart.source_message_id == before_source_message_id,
                    CloneMessagePart.id < before_id,
                ),
            )
        statement = statement.where(cursor_clause)
    rows = session.scalars(statement.order_by(
        CloneMessagePart.source_message_id.desc(),
        CloneMessagePart.id.desc(),
    ).limit(limit)).all()
    return [{
        "id": row.id,
        "obligation_id": row.obligation_id,
        "source_message_id": row.source_message_id,
        "target_message_id": row.target_message_id,
        "target_top_message_id": row.target_top_message_id,
        "account_id": row.account_id,
        "remote_fact_id": row.remote_fact_id,
        "remote_confirmed_at": _iso(row.remote_confirmed_at),
    } for row in rows]


def reconcile_case_items(
    session: Session,
    task: Task,
    *,
    limit: int,
    before_created_at,
    before_id: str | None,
) -> list[dict]:
    statement = select(RemoteReconcileCase, Action).join(
        Action,
        Action.id == RemoteReconcileCase.action_id,
    ).where(
        Action.task_id == task.id,
        Action.tenant_id == task.tenant_id,
    )
    if before_created_at is not None:
        cursor_clause = RemoteReconcileCase.created_at < before_created_at
        if before_id:
            cursor_clause = or_(
                cursor_clause,
                and_(
                    RemoteReconcileCase.created_at == before_created_at,
                    RemoteReconcileCase.id < before_id,
                ),
            )
        statement = statement.where(cursor_clause)
    rows = session.execute(statement.order_by(
        RemoteReconcileCase.created_at.desc(),
        RemoteReconcileCase.id.desc(),
    ).limit(limit)).all()
    return [{
        "id": case.id,
        "action_id": action.id,
        "execution_attempt_id": case.execution_attempt_id,
        "state": case.state,
        "evidence_source": case.evidence_source,
        "remote_message_id": case.remote_message_id,
        "failure_code": case.failure_code,
        "created_at": _iso(case.created_at),
        "unknown_deadline_at": _iso(case.unknown_deadline_at),
        "updated_at": _iso(case.updated_at),
    } for case, action in rows]


def update_ingress_status(session: Session, task: Task) -> dict:
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ))
    if stream is None:
        return {"state": "missing", "owner_lease_healthy": False, "pending_delivery_count": 0}
    state = session.get(TelegramAuthorizationUpdateState, stream.authorization_update_state_id)
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
    ))
    pending = session.scalar(select(func.count()).select_from(TelegramAuthorizationUpdateDelivery).where(
        TelegramAuthorizationUpdateDelivery.task_id == task.id,
        TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
    )) or 0
    lease_healthy = bool(state and state.owner_id and state.lease_expires_at and state.lease_expires_at > _now())
    return {
        "state_id": state.id if state else None,
        "session_generation": state.session_generation if state else None,
        "state": state.state if state else "missing",
        "owner_lease_healthy": lease_healthy,
        "owner_fencing_epoch": state.owner_fencing_epoch if state else None,
        "subscription_state": subscription.state if subscription else "missing",
        "last_ingress_order_no": state.last_ingress_order_no if state else 0,
        "last_consumed_ingress_order_no": stream.last_consumed_ingress_order_no,
        "pending_delivery_count": pending,
        "stream_state": stream.state,
    }


def manual_review_items(
    session: Session, task: Task, *, limit: int, after_sequencer_id: int | None,
) -> list[dict]:
    statement = select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.state == "waiting_manual_review",
    )
    if after_sequencer_id is not None:
        statement = statement.where(
            CloneDeliveryObligation.sequencer_id > after_sequencer_id,
        )
    obligations = session.scalars(statement.order_by(
        CloneDeliveryObligation.sequencer_id,
    ).limit(limit)).all()
    result = []
    for obligation in obligations:
        decision = session.scalar(select(CloneManualReviewDecision).where(
            CloneManualReviewDecision.obligation_id == obligation.id,
        ).order_by(CloneManualReviewDecision.review_revision.desc()).limit(1))
        result.append({
            "review_id": obligation.id,
            "obligation_id": obligation.id,
            "obligation_kind": obligation.obligation_kind,
            "state": obligation.state,
            "error_code": obligation.error_code,
            "sequencer_id": obligation.sequencer_id,
            "revision": decision.review_revision + 1 if decision else 1,
            "last_decision": decision.decision if decision else None,
        })
    return result


def _iso(value) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "manual_review_items",
    "message_mapping_items",
    "reconcile_case_items",
    "update_ingress_status",
]
