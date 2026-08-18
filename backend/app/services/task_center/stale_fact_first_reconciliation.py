from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
    ViewFulfillmentObligation,
)

from .fulfillment_activation import CURRENT_CONTRACT_VERSION


STALE_REASON_CODE = "stale_channel_daily_action"


@dataclass(frozen=True)
class StaleFactFirstScope:
    task_ids: tuple[str, ...]
    execution_date: date


def count_stale_scope(
    session: Session,
    scope: StaleFactFirstScope,
) -> dict[str, int]:
    row = session.execute(build_stale_scope_statement(scope)).one()
    return {
        "stale_count": int(row[0]),
        "existing_fact_count": int(row[1]),
        "gateway_started_count": int(row[2]),
        "no_fact_no_gateway_count": int(row[3]),
        "blocked_no_fact_no_gateway_count": int(row[4]),
    }


def build_stale_scope_statement(scope: StaleFactFirstScope):
    stale = _stale_actions_cte(scope)
    facts = _fact_action_ids_cte(stale)
    gateways = _gateway_action_ids_cte(stale)
    owners = _safe_owner_action_ids_cte(stale)
    reservations = _safe_reservation_action_ids_cte(stale)
    no_fact = facts.c.action_id.is_(None)
    no_gateway = gateways.c.action_id.is_(None)
    blocked = or_(owners.c.action_id.is_(None), reservations.c.action_id.is_(None))
    return (
        select(
            func.count(stale.c.action_id),
            func.count(facts.c.action_id),
            func.count(gateways.c.action_id),
            func.count(stale.c.action_id).filter(no_fact, no_gateway),
            func.count(stale.c.action_id).filter(no_fact, no_gateway, blocked),
        )
        .select_from(stale)
        .outerjoin(facts, facts.c.action_id == stale.c.action_id)
        .outerjoin(gateways, gateways.c.action_id == stale.c.action_id)
        .outerjoin(owners, owners.c.action_id == stale.c.action_id)
        .outerjoin(reservations, reservations.c.action_id == stale.c.action_id)
    )


def stale_scope_predicates(scope: StaleFactFirstScope) -> tuple:
    return (
        Action.task_id.in_(scope.task_ids),
        Action.task_type == "channel_view",
        Action.action_type == "view_message",
        Action.status == "skipped",
        Action.result["error_code"].as_string() == STALE_REASON_CODE,
        Action.payload["execution_date"].as_string()
        == scope.execution_date.isoformat(),
        Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
    )


def _stale_actions_cte(scope: StaleFactFirstScope):
    return (
        select(
            Action.id.label("action_id"),
            Action.payload["view_fulfillment_obligation_id"].as_string().label(
                "obligation_id"
            ),
        )
        .join(Task, Task.id == Action.task_id)
        .where(*stale_scope_predicates(scope))
        .cte("stale_actions")
    )


def _fact_action_ids_cte(stale):
    return (
        select(FulfillmentRemoteFact.action_id)
        .join(stale, stale.c.action_id == FulfillmentRemoteFact.action_id)
        .group_by(FulfillmentRemoteFact.action_id)
        .cte("fact_action_ids")
    )


def _gateway_action_ids_cte(stale):
    return (
        select(ExecutionAttempt.action_id)
        .join(stale, stale.c.action_id == ExecutionAttempt.action_id)
        .where(ExecutionAttempt.gateway_call_started_at.is_not(None))
        .group_by(ExecutionAttempt.action_id)
        .cte("gateway_action_ids")
    )


def _safe_owner_action_ids_cte(stale):
    return (
        select(stale.c.action_id)
        .join(ViewFulfillmentObligation, ViewFulfillmentObligation.id == stale.c.obligation_id)
        .where(
            ViewFulfillmentObligation.status != "confirmed",
            or_(
                ViewFulfillmentObligation.current_action_id.is_(None),
                ViewFulfillmentObligation.current_action_id == stale.c.action_id,
            ),
        )
        .group_by(stale.c.action_id)
        .cte("safe_owner_action_ids")
    )


def _safe_reservation_action_ids_cte(stale):
    return (
        select(AccountPacingReservation.action_id)
        .join(stale, stale.c.action_id == AccountPacingReservation.action_id)
        .where(AccountPacingReservation.state.in_(("reserved", "bound")))
        .group_by(AccountPacingReservation.action_id)
        .cte("safe_reservation_action_ids")
    )


__all__ = [
    "STALE_REASON_CODE",
    "StaleFactFirstScope",
    "build_stale_scope_statement",
    "count_stale_scope",
    "stale_scope_predicates",
]
