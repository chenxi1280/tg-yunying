from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


TASK_CHILD_COLUMNS = (
    ("ai_generation_contract_audits", "task_id"),
    ("comment_fulfillment_obligations", "task_id"),
    ("content_mix_cycles", "task_id"),
    ("dispatch_claim_reservations", "task_id"),
    ("dispatch_claim_task_allocations", "allocation_business_task_id"),
    ("fulfillment_obligation_projections", "task_id"),
    ("generation_jobs", "task_id"),
    ("operation_plan_task_links", "task_id"),
    ("reaction_fulfillment_obligations", "task_id"),
    ("review_queue", "task_id"),
    ("search_click_assignments", "task_id"),
    ("search_click_opportunity_assignments", "task_id"),
    ("search_click_solver_carrier_unit_bindings", "task_id"),
    ("search_click_task_fairness_states", "task_id"),
    ("search_join_protocol_traces", "task_id"),
    ("search_rank_deboost_click_reservations", "task_id"),
    ("task_account_daily_coverage", "task_id"),
    ("task_daily_coverage_plan_cursors", "task_id"),
    ("task_daily_fulfillment_decisions", "task_id"),
    ("task_group_bot_admissions", "task_id"),
    ("task_group_daily_message_slots", "task_id"),
    ("task_group_daily_targets", "task_id"),
    ("task_hard_hourly_buckets", "task_id"),
    ("task_membership_admission_items", "task_id"),
    ("task_runtime_summary", "task_id"),
    ("task_start_operations", "task_id"),
    ("task_day_ledgers", "task_id"),
)

ACTION_CHILD_COLUMNS = (
    ("ai_content_scope_takeover_items", "action_id"),
    ("ai_coverage_variation_intents", "action_id"),
    ("comment_fulfillment_obligations", "current_action_id"),
    ("content_mix_cycle_slots", "current_action_id"),
    ("content_mix_obligations", "assigned_action_id"),
    ("gateway_request_evidence_journals", "action_id"),
    ("pending_visibility_credits", "action_id"),
    ("reaction_fulfillment_obligations", "current_action_id"),
    ("remote_reconcile_cases", "action_id"),
    ("review_queue", "action_id"),
    ("search_click_fulfillment_obligations", "source_action_id"),
    ("search_click_opportunity_assignments", "action_id"),
    ("search_join_protocol_traces", "action_id"),
    ("search_rank_deboost_click_reservations", "action_id"),
    ("task_account_daily_coverage", "last_success_action_id"),
    ("task_account_daily_coverage", "reserved_action_id"),
    ("task_hard_hourly_delivery_credits", "action_id"),
    ("task_membership_admission_items", "delete_action_id"),
    ("task_membership_admission_items", "rescue_action_id"),
    ("task_membership_admission_items", "membership_action_id"),
    ("task_membership_admission_items", "test_message_action_id"),
    ("view_fulfillment_obligations", "current_action_id"),
)


def delete_task_runtime_rows(session: Session, task_id: str) -> None:
    params = {"task_id": task_id}
    session.execute(text(
        "DELETE FROM fulfillment_remote_facts WHERE task_id = :task_id"
    ), params)
    _delete_cross_task_children(session, params)
    _delete_direct_children(session, TASK_CHILD_COLUMNS, params)
    _delete_channel_comment_plan_children(session, params)
    _delete_action_children(session, params)
    session.execute(text(
        "DELETE FROM execution_attempts "
        "WHERE action_id IN (SELECT id FROM actions WHERE task_id = :task_id)"
    ), params)
    session.execute(text("DELETE FROM actions WHERE task_id = :task_id"), params)


def _delete_channel_comment_plan_children(session: Session, params: dict[str, str]) -> None:
    plan_scope = "SELECT id FROM channel_comment_plan_contracts WHERE task_id = :task_id"
    session.execute(text(
        "DELETE FROM channel_comment_grounding_assignments "
        f"WHERE plan_contract_id IN ({plan_scope})"
    ), params)
    session.execute(text(
        "DELETE FROM channel_comment_grounding_snapshots WHERE task_id = :task_id"
    ), params)


def _delete_cross_task_children(session: Session, params: dict[str, str]) -> None:
    session.execute(text(
        "DELETE FROM pending_visibility_credits WHERE "
        "task_account_daily_coverage_id IN "
        "(SELECT id FROM task_account_daily_coverage WHERE task_id = :task_id) "
        "OR task_day_ledger_id IN "
        "(SELECT id FROM task_day_ledgers WHERE task_id = :task_id)"
    ), params)
    session.execute(text(
        "DELETE FROM dispatch_allocation_release_batch_items WHERE assignment_id IN "
        "(SELECT id FROM search_click_opportunity_assignments WHERE task_id = :task_id)"
    ), params)


def _delete_direct_children(
    session: Session,
    relations: tuple[tuple[str, str], ...],
    params: dict[str, str],
) -> None:
    for table, column in relations:
        session.execute(text(f"DELETE FROM {table} WHERE {column} = :task_id"), params)


def _delete_action_children(session: Session, params: dict[str, str]) -> None:
    for table, column in ACTION_CHILD_COLUMNS:
        session.execute(text(
            f"DELETE FROM {table} WHERE {column} IN "
            "(SELECT id FROM actions WHERE task_id = :task_id)"
        ), params)


__all__ = ["delete_task_runtime_rows"]
