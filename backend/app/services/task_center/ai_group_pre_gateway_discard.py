"""Retire an uncalled blocked action while keeping its obligation open."""
from sqlalchemy import select

from app.models import (
    ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal,
    Task, TaskAccountDailyCoverage,
)
from .daily_coverage import release_coverage_reservation

DISCARDABLE_BLOCKERS = frozenset({
    "execution_circuit_open", "execution_circuit_half_open",
    "execution_circuit_probe_pending", "account_shared_usage_unproven",
    "account_legacy_remote_inflight",
    "task_account_portfolio_capacity_exhausted",
})
UNCALLED_STATES = frozenset({"before_call", "skipped_before_gateway", "call_not_started"})


def can_discard_blocked_action(session, action, error):
    if session is None or error.code not in DISCARDABLE_BLOCKERS:
        return False
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return False
    task = session.get(Task, action.task_id)
    if task is None or task.fulfillment_contract_version != "fact_first_v3":
        return False
    if (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return False
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id)))
    if not attempts or any(not _uncalled(attempt) for attempt in attempts):
        return False
    if session.scalar(select(GatewayRequestEvidenceJournal.id).where(
            GatewayRequestEvidenceJournal.action_id == action.id).limit(1)) is not None:
        return False
    return session.scalar(select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.fact_kind != "safely_not_executed").limit(1)) is None


def _uncalled(attempt):
    result = attempt.result_snapshot or {}
    return (attempt.gateway_call_started_at is None and not attempt.remote_message_id
        and result.get("remote_mutation_started") is not True
        and result.get("callback_mutation_started") is not True
        and (attempt.status in UNCALLED_STATES or (
            attempt.status == "failed" and result.get("remote_mutation_started") is False)))


def release_blocked_coverage(session, action, error, *, retry_at, now):
    coverage_id = str((action.payload or {}).get("coverage_ledger_id") or "")
    row = session.scalar(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.id == coverage_id,
        TaskAccountDailyCoverage.tenant_id == action.tenant_id,
        TaskAccountDailyCoverage.task_id == action.task_id,
        TaskAccountDailyCoverage.account_id == action.account_id,
        TaskAccountDailyCoverage.reserved_action_id == action.id,
        TaskAccountDailyCoverage.state.in_(("reserved", "sending")),
    ).with_for_update())
    if row is None:
        return
    if release_coverage_reservation(session, row.id, action.id,
            blocker_code=error.code, blocker_detail=error.detail, next_eligible_at=retry_at):
        row.targeted_at = now
