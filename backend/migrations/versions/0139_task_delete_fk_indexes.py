"""Add the remaining foreign-key indexes required for task deletion."""

from alembic import op


revision = "0139_task_delete_fk_indexes"
down_revision = "0138_physical_delete_hot_indexes"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_fk_dispatch_claim_reservations_85987d93", "dispatch_claim_reservations", "task_id"),
    (
        "ix_fk_dispatch_claim_task_allocations_aa8dfb94",
        "dispatch_claim_task_allocations",
        "allocation_business_task_id",
    ),
    (
        "ix_fk_fulfillment_obligation_projections_e7e8be62",
        "fulfillment_obligation_projections",
        "task_id",
    ),
    ("ix_fk_generation_jobs_571ea4f2", "generation_jobs", "task_id"),
    ("ix_fk_operation_plan_task_links_c3d7317a", "operation_plan_task_links", "task_id"),
    (
        "ix_fk_remote_reconcile_cases_c9ef708f",
        "remote_reconcile_cases",
        "execution_attempt_id",
    ),
    ("ix_fk_review_queue_3691d278", "review_queue", "task_id"),
    ("ix_fk_search_click_assignments_9ca57c62", "search_click_assignments", "task_id"),
    (
        "ix_fk_search_click_opportunity_assignments_75b99129",
        "search_click_opportunity_assignments",
        "task_id",
    ),
    (
        "ix_fk_search_click_solver_carrier_unit_bindings_9878d84e",
        "search_click_solver_carrier_unit_bindings",
        "task_id",
    ),
    (
        "ix_fk_task_daily_coverage_plan_cursors_c7ddfc48",
        "task_daily_coverage_plan_cursors",
        "task_id",
    ),
    (
        "ix_fk_task_group_daily_message_slots_82e51afc",
        "task_group_daily_message_slots",
        "task_id",
    ),
    ("ix_fk_task_hard_hourly_buckets_b82271d4", "task_hard_hourly_buckets", "task_id"),
    ("ix_fk_task_runtime_summary_94a942ca", "task_runtime_summary", "task_id"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)


def downgrade() -> None:
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
