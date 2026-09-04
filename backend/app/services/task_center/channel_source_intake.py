"""Task-lifecycle intake: fixed initial backlog plus durably observed new posts."""
from datetime import datetime

from sqlalchemy import func, or_, select

from app.models import ChannelMessage, ChannelSourceDecision, ChannelTaskIntake, Task
from app.services._common import _now
from app.timezone import as_beijing

from .channel_source_policy import logical_source_key, source_filter_reason, source_opportunity_state, source_window_end
from .channel_source_observation import source_interval_complete


DEFAULT_HISTORICAL_LIMIT = 5
MAX_HISTORICAL_LIMIT = 10


def unified_source_intake(session, task, messages, *, config, observation_complete):
    if config.get("engagement_contract_version") != "unified_engagement_v1":
        return messages
    session.scalar(select(Task.id).where(Task.id == task.id).with_for_update())
    intake = _existing_intake(session, task, config)
    if intake is None and not observation_complete:
        record_source_wait(session, task)
        return []
    all_messages = _known_messages(session, task, config, intake=intake)
    intake = intake or _intake(session, task, all_messages, config)
    previous = {row.channel_message_id: row for row in session.scalars(
        select(ChannelSourceDecision).where(ChannelSourceDecision.intake_id == intake.id,
            ChannelSourceDecision.channel_message_id.in_([m.id for m in all_messages])))}
    accepted = []
    for message in all_messages:
        decision, reason = _decision(intake, message, task.type)
        _record_decision(session, intake, message, previous=previous, decision=decision, reason=reason)
        if decision == "accepted":
            accepted.append(message)
    session.flush()
    counts = dict(session.execute(select(ChannelSourceDecision.decision, func.count()).where(
        ChannelSourceDecision.intake_id == intake.id).group_by(ChannelSourceDecision.decision)).all())
    observation_complete = bool(observation_complete and not counts.get("source_ingestion_unproven")
        and (config.get("message_scope") == "specific" or source_interval_complete(
            session, task, since=intake.anchor_at, until=_now(), allow_fresh=True)))
    mode = config.get("source_expectation_mode", "continuous_event_driven")
    available = [message for message in accepted if source_window_end(task, message) > as_beijing(_now())]
    capability_blocked = sum(not message.comment_available for message in available) if task.type == "channel_comment" else 0
    state = source_opportunity_state(mode, complete=observation_complete, has_sources=bool(available))
    if observation_complete and available and capability_blocked == len(available):
        state = "source_capability_blocked"
    summary = {"intake_id": intake.id, "anchor_at": intake.anchor_at.isoformat(),
               "initial_source_count": len(intake.initial_source_keys), "counts": counts,
               "mode": mode, "observation_complete": observation_complete,
               "capability_blocked_count": capability_blocked, "state": state}
    task.stats = {**dict(task.stats or {}), "source_intake": summary}
    from .daily_ledgers import ensure_task_day_ledger
    ensure_task_day_ledger(session, task)
    return accepted


def _known_messages(session, task, config, *, intake=None):
    query = select(ChannelMessage).where(ChannelMessage.tenant_id == task.tenant_id,
        ChannelMessage.channel_target_id == int(config["target_channel_id"]))
    if intake is not None:
        album_ids = [key.removeprefix("album:") for key in intake.initial_source_keys if key.startswith("album:")]
        message_ids = [int(key.removeprefix("message:")) for key in intake.initial_source_keys if key.startswith("message:")]
        query = query.where(or_(ChannelMessage.published_at > intake.anchor_at,
            ChannelMessage.published_at.is_(None), ChannelMessage.grouped_id.in_(album_ids),
            ChannelMessage.message_id.in_(message_ids)))
    if config.get("message_scope") == "specific":
        ids = config.get("message_ids") or []
        query = query.where(ChannelMessage.message_id.in_(ids) | ChannelMessage.id.in_(ids))
    if config.get("message_scope") == "date_range" or config.get("initial_message_scope") == "date_range":
        if config.get("date_from"):
            query = query.where(ChannelMessage.published_at >= _wall_config_date(config["date_from"]))
        if config.get("date_to"):
            query = query.where(ChannelMessage.published_at <= _wall_config_date(config["date_to"]))
    return list(session.scalars(query.order_by(ChannelMessage.published_at.desc(), ChannelMessage.id.desc())))


def _wall_config_date(value):
    return as_beijing(datetime.fromisoformat(value) if isinstance(value, str) else value)


def _intake(session, task, messages, config):
    channel_id = int(config["target_channel_id"])
    raw_anchor = (task.stats or {}).get("started_at")
    anchor = as_beijing(datetime.fromisoformat(raw_anchor)) if raw_anchor else as_beijing(task.scheduled_start or task.created_at)
    limit = int(config.get("initial_historical_post_limit", DEFAULT_HISTORICAL_LIMIT))
    if not 0 <= limit <= MAX_HISTORICAL_LIMIT:
        raise ValueError("initial_historical_post_limit_invalid")
    if config.get("initial_message_scope") == "new_only":
        limit = 0
    all_keys = list(dict.fromkeys(logical_source_key(m) for m in messages
        if m.published_at and as_beijing(m.published_at) <= anchor
        and not source_filter_reason(m, task_type=task.type)
        and not (m.source_metadata or {}).get("deleted")))
    is_finite = (
        config.get("source_expectation_mode") == "finite_existing_sources"
        or config.get("message_scope") == "specific"
    )
    if is_finite and (limit == DEFAULT_HISTORICAL_LIMIT or "initial_historical_post_limit" not in config):
        keys = all_keys
    else:
        keys = all_keys[:limit]
    intake = ChannelTaskIntake(tenant_id=task.tenant_id, task_id=task.id, lifecycle_epoch=task.task_lifecycle_epoch,
        channel_target_id=channel_id, anchor_at=anchor, initial_source_keys=keys, historical_limit=limit)
    session.add(intake)
    session.flush()
    return intake


def _existing_intake(session, task, config):
    return session.scalar(select(ChannelTaskIntake).where(ChannelTaskIntake.task_id == task.id,
        ChannelTaskIntake.lifecycle_epoch == task.task_lifecycle_epoch,
        ChannelTaskIntake.channel_target_id == int(config["target_channel_id"])))


def record_source_wait(session, task):
    from .daily_ledgers import ensure_task_day_ledger
    ensure_task_day_ledger(session, task)
    current = dict((task.stats or {}).get("source_intake") or {})
    task.stats = {**dict(task.stats or {}), "source_intake": {**current,
        "state": "source_ingestion_unproven", "observation_complete": False,
        "initial_source_count": current.get("initial_source_count", 0), "counts": current.get("counts", {})}}


def _decision(intake, message, task_type):
    if (message.source_metadata or {}).get("deleted"):
        return "source_deleted", "telegram_deleted"
    if message.published_at is None:
        return "source_ingestion_unproven", "published_at_missing"
    historical = as_beijing(message.published_at) <= as_beijing(intake.anchor_at)
    reason = source_filter_reason(message, task_type=task_type)
    if reason == "source_metadata_unproven":
        if historical and logical_source_key(message) not in intake.initial_source_keys:
            return "source_archived_skipped", "outside_initial_backlog"
        return "source_ingestion_unproven", reason
    if reason:
        return "source_filtered_non_content", reason
    if historical and logical_source_key(message) not in intake.initial_source_keys:
        return "source_archived_skipped", "outside_initial_backlog"
    return "accepted", "initial" if historical else "dynamic"


def _record_decision(session, intake, message, *, previous, decision, reason):
    row = previous.get(message.id)
    if row is None:
        row = ChannelSourceDecision(intake_id=intake.id, channel_message_id=message.id,
            source_key=logical_source_key(message), decision=decision, reason=reason)
        session.add(row)
    else:
        row.decision, row.reason = decision, reason
