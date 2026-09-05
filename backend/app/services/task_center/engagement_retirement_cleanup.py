"""Release only positively unissued work under an already retired Task."""
from sqlalchemy import and_, func, or_, select

from app.models import (AccountPacingReservation, AccountPoolConcurrencyLease, Action, AiContentWindowPlanSlot,
    ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal, GenerationJob,
    TaskCommentCapacityReservation, ChannelViewDailyIdentityOwner, ViewFulfillmentObligation)
from app.services._common import _now

from .account_pacing_reservations import release_unbound_account_pacing_reservation
from .ai_content_runtime import invalidate_pre_gateway_window_slot
from .ai_content_window_retirement import retire_pre_gateway_bound_slot
from .channel_action_lifecycle import release_channel_action_resources_before_gateway
from .channel_comment_capacity import release_comment_capacity
from .channel_view_daily_identity import release_claimed_identity
from .direct_action_claims import reconcile_source_pacing_states
from .engagement_direct_cutover import _audit_stage, _require_operation, _require_receipt_audit, verify_retirement
from .engagement_runtime_resources import settle_attempt_resources
from .generation_provider_lineage import generation_lineage, unresolved_generation_lineages
from .source_pacing_admission import settle_source_pacing_admission
from .task_retirement import RETIREMENT_REASON


OPEN_ACTION_STATES = ("pending", "claiming", "executing", "retryable_failed")
UNCALLED_ATTEMPT_STATES = ("before_call", "before_gateway", "skipped_before_gateway", "call_not_started")
OPEN_JOB_STATES = ("pending", "generating", "unknown", "ready")
CLEANUP_AUDIT = "统一引擎旧工作退役清理"
CLEANUP_BATCH_SIZE = 100


def cleanup_cutover_batch(session, receipt, operation, *, batch_size=CLEANUP_BATCH_SIZE):
    _require_operation(operation)
    _require_receipt_audit(session, receipt)
    verify_retirement(session, receipt)
    if batch_size <= 0:
        raise ValueError("engagement_cleanup_batch_size_invalid")
    ids = tuple(receipt["mapping"])
    actions = list(session.scalars(_uncalled_actions(ids).order_by(Action.id).limit(batch_size)
        .with_for_update(nowait=True).execution_options(populate_existing=True)))
    for action in actions:
        _retire_action(session, action)
    jobs = _retire_jobs(session, ids, batch_size=batch_size)
    reservations = _release_unbound_reservations(session, ids, batch_size=batch_size)
    session.flush()
    remaining = cleanup_remaining(session, receipt)
    result = {"actions": len(actions), "jobs": jobs, "reservations": reservations, "remaining": remaining}
    _audit_stage(session, {**receipt, "cleanup": result}, operation, action=CLEANUP_AUDIT)
    return result


def cleanup_remaining(session, receipt):
    ids = tuple(receipt["mapping"])
    return {"actions": int(session.scalar(select(func.count()).select_from(_uncalled_actions(ids).subquery()))),
        "jobs": int(session.scalar(select(func.count()).select_from(_open_jobs(ids).subquery()))),
        "reservations": int(session.scalar(select(func.count()).select_from(_unbound_reservations(ids).subquery()))),
        "comment_capacity": int(session.scalar(select(func.count()).select_from(_unbound_comment_capacity(ids).subquery()))),
        "view_identities": int(session.scalar(select(func.count()).select_from(_unbound_view_identities(ids).subquery())))}


def require_cutover_cleanup(session, receipt):
    remaining = cleanup_remaining(session, receipt)
    if any(remaining.values()):
        raise ValueError(f"engagement_cutover_cleanup_incomplete:{remaining}")


def _uncalled_actions(ids):
    unsafe_attempt = select(ExecutionAttempt.id).where(ExecutionAttempt.action_id == Action.id,
        or_(ExecutionAttempt.gateway_call_started_at.is_not(None), ExecutionAttempt.remote_message_id != "",
            ExecutionAttempt.status.not_in(UNCALLED_ATTEMPT_STATES))).exists()
    fact = select(FulfillmentRemoteFact.fact_id).where(FulfillmentRemoteFact.action_id == Action.id).exists()
    journal = select(GatewayRequestEvidenceJournal.id).where(GatewayRequestEvidenceJournal.action_id == Action.id).exists()
    result = Action.result
    eligible = or_(Action.status.in_(OPEN_ACTION_STATES),
        and_(Action.status.in_(("skipped", "failed", "cancelled")), _has_unreleased_resources()))
    return select(Action).where(Action.task_id.in_(ids), eligible,
        ~unsafe_attempt, ~fact, ~journal,
        func.coalesce(result["remote_message_id"].as_string(), "") == "",
        func.coalesce(result["gateway_call_started_at"].as_string(), "") == "",
        func.coalesce(result["remote_mutation_started"].as_boolean(), False).is_(False),
        func.coalesce(result["callback_mutation_started"].as_boolean(), False).is_(False),
        func.coalesce(result["success"].as_boolean(), False).is_(False))


def _has_unreleased_resources():
    pacing = select(AccountPacingReservation.id).where(AccountPacingReservation.action_id == Action.id,
        AccountPacingReservation.state.in_(("reserved", "bound"))).exists()
    lease = select(AccountPoolConcurrencyLease.id).where(AccountPoolConcurrencyLease.action_id == Action.id,
        AccountPoolConcurrencyLease.state == "reserved").exists()
    comment = select(TaskCommentCapacityReservation.id).where(TaskCommentCapacityReservation.action_id == Action.id,
        TaskCommentCapacityReservation.reservation_state.in_(("plan_reserved", "action_reserved"))).exists()
    view = select(ChannelViewDailyIdentityOwner.id).where(ChannelViewDailyIdentityOwner.action_id == Action.id,
        ChannelViewDailyIdentityOwner.state == "pre_gateway").exists()
    return or_(pacing, lease, comment, view)


def _retire_action(session, action):
    attempts = _uncalled_attempts(session, action)
    action.status = "skipped"
    action.action_version = int(action.action_version or 1) + 1
    action.executed_at = action.executed_at or _now()
    action.lease_owner = action.claim_owner = action.claim_token = ""
    action.lease_expires_at = action.claim_expires_at = None
    action.result = {**dict(action.result or {}), "success": False, "error_code": RETIREMENT_REASON,
        "error_message": "旧工作随任务退役，本次调用未发出", "remote_mutation_started": False}
    for attempt in attempts:
        attempt.status = "skipped_before_gateway"
        attempt.failure_type = RETIREMENT_REASON
        attempt.after_call_at = attempt.after_call_at or _now()
        settle_attempt_resources(attempt, action, remote_mutation_started=False)
        settle_source_pacing_admission(action, attempt)
    state_ids = release_channel_action_resources_before_gateway(session, action, remote_mutation_state="false")
    reconcile_source_pacing_states(session, state_ids)
    _release_action_pacing(session, action)
    if action.obligation_id:
        release_comment_capacity(session, action.obligation_id)
        retire_pre_gateway_bound_slot(session, obligation_type=action.obligation_type,
            obligation_id=action.obligation_id)


def _uncalled_attempts(session, action):
    attempts = list(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id)
        .with_for_update(nowait=True).execution_options(populate_existing=True)))
    if any(attempt.gateway_call_started_at is not None or attempt.status not in UNCALLED_ATTEMPT_STATES
            or attempt.remote_message_id for attempt in attempts):
        raise ValueError("engagement_retirement_attempt_changed")
    return attempts


def _release_action_pacing(session, action):
    rows = session.scalars(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action.id,
        AccountPacingReservation.state.in_(("reserved", "bound"))).with_for_update(nowait=True))
    for row in rows:
        row.state = "released"
        row.version = int(row.version or 1) + 1


def _open_jobs(ids):
    invalidated = func.coalesce(GenerationJob.evaluator_evidence["invalidation_reason"].as_string(), "")
    return select(GenerationJob).where(GenerationJob.task_id.in_(ids), GenerationJob.state.in_(OPEN_JOB_STATES),
        invalidated != RETIREMENT_REASON, ~_job_has_issued_action())


def _job_has_issued_action():
    matching = or_(Action.payload["generation_job_id"].as_string() == GenerationJob.id,
        and_(Action.obligation_id != "", Action.obligation_type == GenerationJob.obligation_type,
            Action.obligation_id == GenerationJob.obligation_id))
    issued = select(ExecutionAttempt.id).where(ExecutionAttempt.action_id == Action.id,
        or_(ExecutionAttempt.gateway_call_started_at.is_not(None), ExecutionAttempt.status.in_(("success", "result_unknown")))).exists()
    return select(Action.id).where(Action.task_id == GenerationJob.task_id, matching,
        or_(issued, Action.status.in_(("success", "unknown_after_send")))).exists()


def _retire_jobs(session, ids, *, batch_size):
    jobs = list(session.scalars(_open_jobs(ids).order_by(GenerationJob.id).limit(batch_size)
        .with_for_update(nowait=True).execution_options(populate_existing=True)))
    if not jobs:
        return 0
    unresolved = unresolved_generation_lineages(session, jobs)
    for job in jobs:
        job.state = "unknown" if generation_lineage(job) in unresolved else "cancelled"
        job.generation_owner_id = ""
        job.lease_expires_at = job.next_retry_at = None
        job.job_version = int(job.job_version or 1) + 1
        job.generation_lease_epoch = int(job.generation_lease_epoch or 0) + 1
        job.evaluator_evidence = {**dict(job.evaluator_evidence or {}), "invalidation_reason": RETIREMENT_REASON}
        slot = session.get(AiContentWindowPlanSlot, job.window_slot_id) if job.window_slot_id else None
        if slot is not None and slot.state != "gateway_bound":
            invalidate_pre_gateway_window_slot(slot)
    return len(jobs)


def _unbound_reservations(ids):
    return select(AccountPacingReservation).where(AccountPacingReservation.task_id.in_(ids),
        AccountPacingReservation.state == "reserved", AccountPacingReservation.action_id.is_(None))


def _release_unbound_reservations(session, ids, *, batch_size):
    rows = list(session.scalars(_unbound_reservations(ids).order_by(AccountPacingReservation.id)
        .limit(batch_size).with_for_update(nowait=True)))
    for row in rows:
        release_unbound_account_pacing_reservation(row)
    comment_rows = list(session.scalars(_unbound_comment_capacity(ids).limit(batch_size).with_for_update(nowait=True)))
    for row in comment_rows:
        release_comment_capacity(session, row.obligation_id)
    owners = list(session.scalars(_unbound_view_identities(ids).limit(batch_size).with_for_update(nowait=True)))
    for owner in owners:
        obligation = session.get(ViewFulfillmentObligation, owner.obligation_id)
        if obligation is None or not release_claimed_identity(session, obligation):
            raise ValueError("engagement_retirement_view_identity_not_releasable")
    return len(rows) + len(comment_rows) + len(owners)


def _unbound_comment_capacity(ids):
    return select(TaskCommentCapacityReservation).where(TaskCommentCapacityReservation.task_id.in_(ids),
        TaskCommentCapacityReservation.action_id.is_(None),
        TaskCommentCapacityReservation.reservation_state == "plan_reserved")


def _unbound_view_identities(ids):
    return select(ChannelViewDailyIdentityOwner).where(ChannelViewDailyIdentityOwner.logical_task_id.in_(ids),
        ChannelViewDailyIdentityOwner.state == "pre_gateway", ChannelViewDailyIdentityOwner.action_id.is_(None))
