from datetime import timedelta

from sqlalchemy import case

from app.models import Action
from app.timezone import as_beijing


GENERATION_PREPARATION_SECONDS = 10
GENERATION_LOOKAHEAD = timedelta(seconds=GENERATION_PREPARATION_SECONDS)
GENERATION_LEASE = timedelta(minutes=10)


def generation_not_before(action: Action):
    limits = (action.scheduled_at, action.release_not_before_at, action.effective_claim_at)
    send_at = max(as_beijing(value) for value in limits if value is not None)
    return send_at - GENERATION_LOOKAHEAD


def generation_send_time_expression():
    effective = case(
        (Action.effective_claim_at > Action.scheduled_at, Action.effective_claim_at),
        else_=Action.scheduled_at,
    )
    return case(
        (Action.release_not_before_at > effective, Action.release_not_before_at),
        else_=effective,
    )


__all__ = ["GENERATION_LEASE", "GENERATION_LOOKAHEAD", "generation_not_before", "generation_send_time_expression"]
