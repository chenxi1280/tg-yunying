"""Typed message completion correlated to its original ledger and attempt."""
from sqlalchemy import and_, or_, select, func

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, TaskDayLedger, TaskGroupDailyMessageSlot


def verified_message_action_ids(task, *, since=None):
    fact, attempt = FulfillmentRemoteFact, ExecutionAttempt
    query = select(fact.action_id).join(attempt, and_(
        attempt.id == fact.attempt_id, attempt.action_id == fact.action_id,
    )).join(Action, Action.id == fact.action_id).where(
        fact.tenant_id == task.tenant_id, fact.task_id == task.id,
        fact.task_type == "group_ai_chat", fact.mutation_kind == "send_message",
        fact.fact_kind == "remote_message_observed",
        fact.observed_at >= attempt.gateway_call_started_at,
        fact.outcome["remote_message_id"].as_string() == attempt.remote_message_id,
        Action.tenant_id == task.tenant_id, Action.task_id == task.id,
        Action.task_type == "group_ai_chat", Action.action_type == "send_message",
        attempt.tenant_id == task.tenant_id, attempt.account_id == Action.account_id,
        attempt.status == "success", func.trim(attempt.remote_message_id) != "",
        _fact_ledger_matches(task),
    )
    if since is not None:
        query = query.where(fact.observed_at >= since, attempt.gateway_call_started_at >= since)
    return query.distinct()


def _fact_ledger_matches(task):
    fact, slot, ledger = FulfillmentRemoteFact, TaskGroupDailyMessageSlot, TaskDayLedger
    ledger_matches = select(ledger.id).where(
        ledger.id == fact.task_day_ledger_id, ledger.tenant_id == task.tenant_id,
        ledger.task_id == task.id,
    ).correlate(fact).exists()
    payload_ledger = Action.payload["task_day_ledger_id"].as_string()
    slot_matches = select(slot.id).where(
        slot.id == Action.primary_quantity_slot_id, slot.tenant_id == task.tenant_id,
        slot.task_id == task.id, slot.task_day_ledger_id == fact.task_day_ledger_id,
    ).correlate(Action, fact).exists()
    canonical = and_(slot_matches, or_(payload_ledger.is_(None), payload_ledger == fact.task_day_ledger_id))
    if (task.type_config or {}).get("engagement_contract_version") == "unified_engagement_v1":
        return and_(ledger_matches, canonical)
    return and_(ledger_matches, or_(canonical, and_(
        Action.primary_quantity_slot_id.is_(None), payload_ledger == fact.task_day_ledger_id,
    )))
