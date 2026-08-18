from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Action, FulfillmentObligationProjection
from app.services._common import _now


OPEN_ACTION_STATES = frozenset({
    "pending",
    "claiming",
    "executing",
    "unknown_after_send",
})


def rebind_projection(
    session: Session,
    action: Action,
    projection: FulfillmentObligationProjection,
) -> bool:
    if projection.active_action_id == action.id:
        _sync_materialization_version(action, projection)
        return True
    version = int(projection.version or 1)
    next_materialization_version = int(projection.materialization_version or 1) + 1
    with session.no_autoflush:
        changed = session.execute(
            update(FulfillmentObligationProjection)
            .where(
                FulfillmentObligationProjection.id == projection.id,
                FulfillmentObligationProjection.state == "open",
                FulfillmentObligationProjection.version == version,
            )
            .values(
                active_action_id=action.id,
                materialization_version=next_materialization_version,
                version=version + 1,
            )
        ).rowcount
        if changed != 1:
            session.refresh(projection)
            return _resolve_conflict(session, action, projection)
    action.materialization_version = next_materialization_version
    session.expire(projection)
    return True


def _resolve_conflict(
    session: Session,
    action: Action,
    projection: FulfillmentObligationProjection,
) -> bool:
    if projection.state != "open":
        skip_obligation_action(
            action,
            "obligation_not_open",
            obligation_state=projection.state,
        )
        return False
    if projection.active_action_id == action.id:
        _sync_materialization_version(action, projection)
        return True
    winner = _active_action(session, projection)
    if _is_open_winner(action, winner):
        skip_obligation_action(
            action,
            "duplicate_open_obligation",
            existing_action_id=winner.id,
        )
        return False
    raise ValueError("fulfillment_obligation_materialization_conflict")


def _active_action(
    session: Session,
    projection: FulfillmentObligationProjection,
) -> Action | None:
    if not projection.active_action_id:
        return None
    return session.get(Action, projection.active_action_id)


def _is_open_winner(action: Action, winner: Action | None) -> bool:
    return bool(
        winner
        and winner.status in OPEN_ACTION_STATES
        and winner.obligation_type == action.obligation_type
        and winner.obligation_id == action.obligation_id
    )


def _sync_materialization_version(
    action: Action,
    projection: FulfillmentObligationProjection,
) -> None:
    action.materialization_version = int(projection.materialization_version or 1)


def skip_obligation_action(action: Action, code: str, **detail) -> None:
    action.status = "skipped"
    action.executed_at = _now()
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": code,
        **detail,
    }
