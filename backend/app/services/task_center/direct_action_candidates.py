"""Read-only candidate selection for live claims and expired settlement."""
from datetime import datetime

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import (AccountPacingReservation, Action, ExecutionAttempt, Task,
    GatewayRequestEvidenceJournal, FulfillmentRemoteFact, GenerationJob, SourcePacingAdmission)
from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .channel_membership_execution import task_action_execution_condition
from .dispatch_session_priority import current_session_priority, current_session_end

def _candidate_rows(
    session: Session,
    *,
    limit: int,
    now: datetime,
    exclude_task_ids: set[str] | None,
    execution_lane: str | None,
    shard_total: int = 1,
    shard_index: int = 0,
    phase: str = "all",
) -> list[tuple[str, str, int]]:
    ranked = _ranked_candidate_query(now, dialect_name=session.bind.dialect.name,
        settlement=phase == "expired")
    if exclude_task_ids:
        ranked = ranked.where(Action.task_id.not_in(exclude_task_ids))
    ranked = _filter_execution_lane(ranked, execution_lane)
    ranked = _filter_account_shard(ranked, shard_total, shard_index)
    if phase == "expired":
        ranked = ranked.where(_deadline_exhausted_action(now), _uncalled_unowned_action())
    elif phase == "live":
        ranked = ranked.where(~_deadline_exhausted_action(now))
    rows = ranked.subquery()
    ordering = (rows.c.task_rank, rows.c.scheduled_at, rows.c.task_id, rows.c.action_id) if phase == "expired" else (
        rows.c.deadline_rank, rows.c.session_rank, rows.c.task_rank,
        rows.c.session_end.asc().nulls_last(), rows.c.scheduled_at, rows.c.task_id, rows.c.action_id)
    statement = (
        select(rows.c.action_id, rows.c.task_id, rows.c.action_version)
        .order_by(*ordering)
        .limit(max(1, limit))
    )
    return list(session.execute(statement))


def _ranked_candidate_query(now: datetime, *, dialect_name="postgresql", settlement=False):
    deadline_rank = case((_deadline_exhausted_action(now), 1), else_=0)
    session_rank = literal(1) if settlement else current_session_priority(now, dialect_name=dialect_name)
    session_end = literal(None) if settlement else current_session_end(now, dialect_name=dialect_name)
    rank = func.row_number().over(
        partition_by=Action.task_id,
        order_by=(deadline_rank, session_rank, session_end.asc().nulls_last(), Action.scheduled_at, Action.id),
    ).label("task_rank")
    return (
        select(
            Action.id.label("action_id"),
            Action.task_id.label("task_id"),
            Action.action_version.label("action_version"),
            Action.scheduled_at.label("scheduled_at"),
            deadline_rank.label("deadline_rank"),
            session_rank.label("session_rank"),
            session_end.label("session_end"),
            rank,
        )
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "pending",
            or_(Action.scheduled_at <= now, _deadline_exhausted_action(now)),
            _has_claimable_account_reservation(),
            task_action_execution_condition(),
            Task.deleted_at.is_(None),
            Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
            _group_generation_ready(now),
            _comment_generation_ready(now),
        )
    )


def _group_generation_ready(now: datetime | None = None):
    return or_(
        Action.task_type != "group_ai_chat",
        Action.action_type != "send_message",
        _deadline_exhausted_action(now),
        func.coalesce(Action.payload["message_text"].as_string(), "") != "",
        func.coalesce(Action.payload["ai_generation_status"].as_string(), "") == "",
    )


def _comment_generation_ready(now: datetime | None = None):
    status = func.coalesce(
        Action.payload["ai_generation_status"].as_string(), "",
    )
    return or_(
        Action.task_type != "channel_comment",
        Action.action_type != "post_comment",
        _deadline_exhausted_action(now),
        status.in_(("", "ready")),
    )


def _deadline_exhausted_action(now: datetime | None = None):
    deadline_condition = and_(
        Action.release_not_before_at.is_not(None),
        AccountPacingReservation.source_deadline_at <= Action.release_not_before_at,
    )
    if now is not None:
        deadline_condition = or_(
            deadline_condition,
            AccountPacingReservation.source_deadline_at <= now,
        )
    return select(AccountPacingReservation.id).where(
        AccountPacingReservation.action_id == Action.id,
        AccountPacingReservation.state.in_(("reserved", "bound")),
        AccountPacingReservation.source_deadline_at.is_not(None),
        deadline_condition,
    ).exists()


def _has_claimable_account_reservation():
    open_reservation = select(AccountPacingReservation.id).where(
        AccountPacingReservation.tenant_id == Action.tenant_id,
        AccountPacingReservation.account_id == Action.account_id,
        AccountPacingReservation.pacing_slot_key == Action.pacing_slot_key,
        AccountPacingReservation.state.in_(("reserved", "bound")),
    ).exists()
    return or_(
        Action.pacing_slot_key.is_(None),
        Action.pacing_slot_key == "",
        Action.account_id.is_(None),
        open_reservation,
    )


def _filter_execution_lane(statement, execution_lane: str | None):
    if execution_lane == "search":
        return statement.where(Action.execution_lane == "search")
    elif execution_lane == "non_search":
        return statement.where(or_(
            Action.execution_lane.is_(None),
            Action.execution_lane != "search",
        ))
    return statement


def _filter_account_shard(statement, shard_total: int, shard_index: int):
    total = max(1, int(shard_total or 1))
    index = max(0, min(total - 1, int(shard_index or 0)))
    if total == 1:
        return statement
    return statement.where(or_(
        Action.account_id.is_(None),
        (Action.account_id % total) == index,
    ))


def _uncalled_unowned_action():
    called = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    journal = select(GatewayRequestEvidenceJournal.id).where(
        GatewayRequestEvidenceJournal.action_id == Action.id,
    ).exists()
    fact = select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.action_id == Action.id,
    ).exists()
    source = select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.action_id == Action.id,
        SourcePacingAdmission.state.in_(("call_started", "remote_unknown", "finished")),
    ).exists()
    generating = select(GenerationJob.id).where(
        GenerationJob.id == Action.payload["generation_job_id"].as_string(),
        GenerationJob.state.in_(("generating", "unknown")),
    ).exists()
    return and_(~called, ~journal, ~fact, ~source, ~generating,
        func.coalesce(Action.claim_owner, "") == "",
        func.coalesce(Action.lease_owner, "") == "")
