"""Prove contiguous observed intervals; a ready snapshot alone is not completeness."""
from sqlalchemy import select

from app.models import Action, ChannelMessage, ChannelSourceDecision, ChannelTaskIntake, ListenerSourceState, TaskSourceSubscription, TaskDayLedgerLifecycleEvent
from app.timezone import as_beijing

from .channel_source_policy import source_opportunity_state, source_window_end
from .datetime_compat import utc_storage_as_beijing_wall


SOURCE_MODE_EVENTS = {
    "continuous_event_driven": "source_mode_continuous",
    "finite_existing_sources": "source_mode_finite",
    "promised_daily_sources": "source_mode_promised",
}


def snapshot_source_expectation(session, task, ledger):
    if task.type not in {"channel_comment", "channel_like", "channel_view"} or (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return
    mode = (task.type_config or {}).get("source_expectation_mode", "continuous_event_driven")
    session.add(TaskDayLedgerLifecycleEvent(tenant_id=task.tenant_id,
        task_day_ledger_id=ledger.id, event_type=SOURCE_MODE_EVENTS[mode],
        occurred_at=ledger.planning_anchor_at, task_revision=task.config_revision))


def _source_mode(session, task, ledger):
    event = session.scalar(select(TaskDayLedgerLifecycleEvent.event_type).where(
        TaskDayLedgerLifecycleEvent.task_day_ledger_id == ledger.id,
        TaskDayLedgerLifecycleEvent.event_type.in_(SOURCE_MODE_EVENTS.values()))
        .order_by(TaskDayLedgerLifecycleEvent.created_at).limit(1))
    return next((mode for mode, code in SOURCE_MODE_EVENTS.items() if code == event),
                (task.type_config or {}).get("source_expectation_mode", "continuous_event_driven"))


def source_interval_complete(session, task, *, since, until, allow_fresh=False):
    states = session.scalars(select(ListenerSourceState).join(TaskSourceSubscription,
        TaskSourceSubscription.listener_source_state_id == ListenerSourceState.id).where(
        TaskSourceSubscription.task_id == task.id, ListenerSourceState.tenant_id == task.tenant_id,
        TaskSourceSubscription.lifecycle_epoch == task.task_lifecycle_epoch,
        ListenerSourceState.source_type == "channel",
        ListenerSourceState.source_peer_id == str((task.type_config or {}).get("target_channel_id"))))
    for state in states:
        complete_until = state.backfill_until or state.observed_at
        if allow_fresh and not state.backfill_until and state.fresh_until_at and as_beijing(state.fresh_until_at) >= as_beijing(until):
            complete_until = until
        if state.last_event_at and complete_until and as_beijing(state.last_event_at) <= as_beijing(since) and as_beijing(complete_until) >= as_beijing(until):
            return True
    return False


def source_deadline_outcome(session, task, ledger):
    config = task.type_config or {}
    if task.type not in {"channel_comment", "channel_like", "channel_view"} or config.get("engagement_contract_version") != "unified_engagement_v1":
        return None
    mode = _source_mode(session, task, ledger)
    start = utc_storage_as_beijing_wall(ledger.planning_anchor_at)
    deadline = utc_storage_as_beijing_wall(ledger.deadline_at)
    sources = session.scalars(select(ChannelMessage).join(ChannelSourceDecision,
        ChannelSourceDecision.channel_message_id == ChannelMessage.id).join(ChannelTaskIntake).where(
        ChannelTaskIntake.task_id == task.id, ChannelTaskIntake.lifecycle_epoch == task.task_lifecycle_epoch,
        ChannelSourceDecision.decision == "accepted",
        ChannelMessage.published_at < deadline))
    if mode == "promised_daily_sources":
        start = utc_storage_as_beijing_wall(ledger.period_start_at)
        available = any(as_beijing(message.published_at) >= start for message in sources)
    else:
        available = any(source_window_end(task, message) > start for message in sources)
    carryover = session.scalar(select(Action.id).where(Action.task_id == task.id,
        Action.task_lifecycle_epoch == task.task_lifecycle_epoch,
        Action.scheduled_at >= start, Action.scheduled_at < deadline).limit(1))
    if available or (carryover and mode != "promised_daily_sources"):
        return None
    complete = source_interval_complete(session, task, since=start, until=deadline)
    return source_opportunity_state(mode, complete=complete, has_sources=False, day_closed=True)
