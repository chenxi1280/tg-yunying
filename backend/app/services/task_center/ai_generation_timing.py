from datetime import timedelta

from sqlalchemy import func

from app.models import Action
from app.timezone import as_beijing


GENERATION_PREPARATION_SECONDS = 10
GENERATION_LOOKAHEAD = timedelta(seconds=GENERATION_PREPARATION_SECONDS)
GENERATION_LEASE = timedelta(minutes=10)


def generation_not_before(action: Action):
    send_at = action.effective_claim_at or action.release_not_before_at or action.scheduled_at
    return as_beijing(send_at) - GENERATION_LOOKAHEAD


def generation_send_time_expression():
    return func.coalesce(Action.effective_claim_at, Action.release_not_before_at, Action.scheduled_at)


__all__ = ["GENERATION_LEASE", "GENERATION_LOOKAHEAD", "generation_not_before", "generation_send_time_expression"]
