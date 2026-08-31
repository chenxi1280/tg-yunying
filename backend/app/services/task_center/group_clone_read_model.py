from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, RemoteReconcileCase, Task
from app.models.fulfillment_v2 import FulfillmentObligationProjection, FulfillmentRemoteFact
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneManualReviewDecision,
    CloneMessagePart,
    CloneSenderBindingHistory,
    CloneSequencerHeadCase,
    CloneSourceEvent,
    CloneSourceStreamState,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now

from .group_clone_manual_review import allowed_manual_review_decisions

NON_BLOCKING_OBLIGATION_STATES = frozenset({
    "succeeded", "degraded", "filtered", "superseded",
    "failed_terminal", "unknown_after_send", "remote_reconcile_only",
})


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
        CloneMessagePart.epoch == task.task_lifecycle_epoch,
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
        Action.task_lifecycle_epoch == task.task_lifecycle_epoch,
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
    pending = 0
    if subscription is not None:
        pending = session.scalar(select(func.count()).select_from(
            TelegramAuthorizationUpdateDelivery,
        ).where(
            TelegramAuthorizationUpdateDelivery.subscription_id == subscription.id,
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
    session: Session,
    task: Task,
    *,
    limit: int,
    after_sequencer_id: int | None,
    include_resolved: bool = False,
) -> list[dict]:
    statement = select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
    )
    if include_resolved:
        decided_ids = select(CloneManualReviewDecision.obligation_id)
        statement = statement.where(or_(
            CloneDeliveryObligation.state == "waiting_manual_review",
            CloneDeliveryObligation.id.in_(decided_ids),
        ))
    else:
        statement = statement.where(
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
        result.append(_manual_review_item(obligation, decision))
    return result


def _manual_review_item(obligation, decision) -> dict:
    return {
        "review_id": obligation.id,
        "obligation_id": obligation.id,
        "obligation_kind": obligation.obligation_kind,
        "state": obligation.state,
        "error_code": obligation.error_code,
        "sequencer_id": obligation.sequencer_id,
        "revision": decision.review_revision + 1 if decision else 1,
        "last_decision": decision.decision if decision else None,
        "decision_actor": decision.actor_name if decision else None,
        "decision_reason": decision.reason if decision else None,
        "decided_at": _iso(decision.decided_at) if decision else None,
        "allowed_decisions": allowed_manual_review_decisions(obligation.error_code),
    }


def clone_runtime_summary(session: Session, task: Task) -> dict:
    scope = (
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
    )
    state_rows = session.execute(select(
        CloneDeliveryObligation.state,
        func.count(CloneDeliveryObligation.id),
    ).where(*scope).group_by(CloneDeliveryObligation.state)).all()
    obligation_states = {str(state): int(count) for state, count in state_rows}
    ingress = update_ingress_status(session, task)
    business_health, blocker = _business_health(
        task, obligation_states=obligation_states, ingress=ingress,
    )
    return {
        "epoch": task.task_lifecycle_epoch,
        "source_event_count": _count(session, CloneSourceEvent, task, "task_lifecycle_epoch"),
        "obligation_count": sum(obligation_states.values()),
        "obligation_states": obligation_states,
        "business_health": business_health,
        "business_blocker": blocker,
        "strict_success_count": obligation_states.get("succeeded", 0),
        "degraded_count": obligation_states.get("degraded", 0),
        "filtered_count": obligation_states.get("filtered", 0),
        "blocked_count": _blocking_count(obligation_states),
        "failed_count": obligation_states.get("failed_terminal", 0),
        "unknown_count": sum(
            obligation_states.get(state, 0)
            for state in ("unknown_after_send", "remote_reconcile_only")
        ),
        "message_mapping_count": _count(session, CloneMessagePart, task, "epoch"),
        "active_binding_count": _active_binding_count(session, task),
        "manual_review_count": obligation_states.get("waiting_manual_review", 0),
        "open_sequencer_case_count": _open_case_count(session, task),
        "ingress_lag": max(
            0,
            int(ingress.get("last_ingress_order_no") or 0)
            - int(ingress.get("last_consumed_ingress_order_no") or 0),
        ),
    }


def obligation_item(session: Session, obligation: CloneDeliveryObligation) -> dict:
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == "group_clone_delivery",
        FulfillmentObligationProjection.obligation_id == obligation.id,
    ))
    part = session.scalar(select(CloneMessagePart).where(
        CloneMessagePart.obligation_id == obligation.id,
    ).order_by(CloneMessagePart.part_index).limit(1))
    action, attempt, fact = _obligation_chain(session, obligation, projection, part)
    return {
        "id": obligation.id,
        "source_event_id": obligation.source_event_id,
        "stream_order_no": obligation.stream_order_no,
        "obligation_kind": obligation.obligation_kind,
        "state": obligation.state,
        "error_code": obligation.error_code,
        "degradation_reason": obligation.degradation_reason,
        "planned_at": _iso(obligation.planned_at),
        "resolved_at": _iso(obligation.resolved_at),
        "sequencer_head_case_id": obligation.sequencer_head_case_id,
        "fop_id": projection.id if projection else None,
        "action_id": action.id if action else None,
        "attempt_id": attempt.id if attempt else None,
        "remote_fact_id": fact.fact_id if fact else None,
        "target_message_id": part.target_message_id if part else None,
    }


def _obligation_chain(session, obligation, projection, part):
    action_id = part.action_id if part else projection.active_action_id if projection else None
    action = session.get(Action, action_id) if action_id else session.scalar(
        select(Action).where(
            Action.task_id == obligation.task_id,
            Action.obligation_id == obligation.id,
        ).order_by(Action.created_at.desc()).limit(1)
    )
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1)) if action else None
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.attempt_id == attempt.id,
    ).order_by(FulfillmentRemoteFact.observed_at.desc()).limit(1)) if attempt else None
    return action, attempt, fact


def _business_health(task, *, obligation_states, ingress) -> tuple[str, str | None]:
    if task.status == "failed":
        return "failed", task.last_error or "task_failed"
    if task.status not in {"running", "completed"}:
        return "blocked", f"task_not_running:{task.status}"
    ingress_ready = (
        ingress.get("state") == "live"
        and ingress.get("subscription_state") == "active"
        and ingress.get("stream_state") == "live"
        and ingress.get("owner_lease_healthy")
    )
    if not ingress_ready:
        return "blocked", "update_ingress_or_source_stream_unhealthy"
    if any(obligation_states.get(state, 0) for state in ("unknown_after_send", "remote_reconcile_only")):
        return "unknown", "remote_result_unproven"
    if obligation_states.get("failed_terminal", 0):
        return "failed", "failed_terminal_obligation"
    if _blocking_count(obligation_states):
        return "blocked", "open_delivery_obligation"
    if obligation_states.get("degraded", 0):
        return "degraded", "degraded_delivery"
    return "healthy", None


def _blocking_count(obligation_states) -> int:
    return sum(
        count for state, count in obligation_states.items()
        if state not in NON_BLOCKING_OBLIGATION_STATES
    )


def _count(session, model, task, epoch_field: str) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(
        model.task_id == task.id,
        getattr(model, epoch_field) == task.task_lifecycle_epoch,
    )) or 0)


def _active_binding_count(session, task) -> int:
    return int(session.scalar(select(func.count()).select_from(
        CloneSenderBindingHistory,
    ).where(
        CloneSenderBindingHistory.task_id == task.id,
        CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSenderBindingHistory.status.in_(("active", "guarded", "eligible")),
    )) or 0)


def _open_case_count(session, task) -> int:
    return int(session.scalar(select(func.count()).select_from(
        CloneSequencerHeadCase,
    ).where(
        CloneSequencerHeadCase.task_id == task.id,
        CloneSequencerHeadCase.epoch == task.task_lifecycle_epoch,
        CloneSequencerHeadCase.state.in_((
            "waiting_decision", "blocked", "retry_authorized", "retry_in_progress",
        )),
    )) or 0)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "clone_runtime_summary",
    "obligation_item",
    "manual_review_items",
    "message_mapping_items",
    "reconcile_case_items",
    "update_ingress_status",
]
