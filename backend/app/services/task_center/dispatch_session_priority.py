"""Rank existing behavior windows without granting or changing send authority."""
from sqlalchemy import JSON, and_, case, cast, func, or_, select, true

from app.models import AccountBehaviorSessionPlan, AccountPacingReservation, Action
from app.timezone import as_beijing

from .account_pacing_guard import ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION


def current_session_priority(now, *, dialect_name):
    available = or_(~_needs_session(), _has_current_window(now, dialect_name=dialect_name))
    return case((and_(available, or_(
        Action.release_not_before_at.is_(None), Action.release_not_before_at <= now,
    )), 0), else_=1)


def _needs_session():
    return select(AccountPacingReservation.id).where(
        AccountPacingReservation.tenant_id == Action.tenant_id,
        AccountPacingReservation.account_id == Action.account_id,
        AccountPacingReservation.pacing_slot_key == Action.pacing_slot_key,
        AccountPacingReservation.state.in_(("reserved", "bound")),
        AccountPacingReservation.policy_version == ACCOUNT_BEHAVIOR_SESSION_PACING_POLICY_VERSION,
    ).correlate(Action).exists()


def _has_current_window(now, *, dialect_name):
    return _current_window_query(now, dialect_name=dialect_name).exists()


def current_session_end(now, *, dialect_name):
    query = _current_window_query(now, dialect_name=dialect_name).where(_needs_session())
    # Non-session work has no session expiry; this key grants no runtime permission.
    return query.order_by("session_end").limit(1).scalar_subquery()


def _current_window_query(now, *, dialect_name):
    plan = AccountBehaviorSessionPlan
    local_now = as_beijing(now).replace(tzinfo=None)
    if dialect_name == "sqlite":
        windows = func.json_each(plan.windows).table_valued("value", joins_implicitly=True)
        start = func.json_extract(windows.c.value, "$.start_at")
        end = func.json_extract(windows.c.value, "$.end_at")
    else:
        windows = func.json_array_elements(plan.windows).table_valued("value", joins_implicitly=True)
        start = cast(windows.c.value, JSON)["start_at"].as_string()
        end = cast(windows.c.value, JSON)["end_at"].as_string()
    return select(end.label("session_end")).select_from(plan).join(windows, true()).where(
        plan.tenant_id == Action.tenant_id, plan.account_id == Action.account_id,
        plan.task_day == local_now.date(), plan.state == "active",
        start <= local_now.isoformat(), end > local_now.isoformat(),
    ).correlate(Action)
