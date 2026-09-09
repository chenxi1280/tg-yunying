"""Count independent content only from the original successful typed send facts."""
from sqlalchemy import or_, select

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, TaskDayLedger, TaskGroupDailyMessageSlot


def independent_quality_summary(session, task, task_day) -> dict:
    selection = Action.payload["emergency_selection_id"].as_string()
    mode = Action.payload["ai_generation_context_mode"].as_string()
    rows = session.execute(select(Action.id, selection, mode)
        .join(TaskGroupDailyMessageSlot, TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id)
        .join(TaskDayLedger, TaskDayLedger.id == TaskGroupDailyMessageSlot.task_day_ledger_id)
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .join(FulfillmentRemoteFact, (FulfillmentRemoteFact.action_id == Action.id)
              & (FulfillmentRemoteFact.attempt_id == ExecutionAttempt.id))
        .where(Action.tenant_id == task.tenant_id, Action.task_id == task.id,
            TaskDayLedger.obligation_local_date == task_day,
            TaskDayLedger.tenant_id == task.tenant_id, TaskDayLedger.task_id == task.id,
            TaskGroupDailyMessageSlot.tenant_id == task.tenant_id, TaskGroupDailyMessageSlot.task_id == task.id,
            or_(selection != "", mode == "topic_only"),
            ExecutionAttempt.status == "success", ExecutionAttempt.tenant_id == Action.tenant_id,
            ExecutionAttempt.account_id == Action.account_id,
            ExecutionAttempt.remote_message_id != "", ExecutionAttempt.gateway_call_started_at.is_not(None),
            FulfillmentRemoteFact.tenant_id == Action.tenant_id, FulfillmentRemoteFact.task_id == Action.task_id,
            FulfillmentRemoteFact.obligation_id == Action.obligation_id,
            FulfillmentRemoteFact.obligation_type == Action.obligation_type,
            FulfillmentRemoteFact.fact_kind == "remote_message_observed",
            FulfillmentRemoteFact.mutation_kind == "send_message",
            FulfillmentRemoteFact.observed_at >= ExecutionAttempt.gateway_call_started_at))
    facts = {action_id: (emergency, context) for action_id, emergency, context in rows}
    return {"remote_emergency_count": sum(bool(emergency) for emergency, _ in facts.values()),
            "remote_topic_only_count": sum(not emergency and context == "topic_only"
                                           for emergency, context in facts.values())}
