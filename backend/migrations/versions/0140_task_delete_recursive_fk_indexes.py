"""Index every recursive cascade below a task."""

from alembic import op


revision = "0140_task_delete_recursive_idx"
down_revision = "0139_task_delete_fk_indexes"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_fk_actions_04ad1423", "actions", "primary_quantity_slot_id"),
    ("ix_fk_content_mix_cycles_03f1f933", "content_mix_cycles", "task_day_ledger_id"),
    (
        "ix_fk_content_mix_obligations_2e18a065",
        "content_mix_obligations",
        "assigned_cycle_slot_id",
    ),
    (
        "ix_fk_dispatch_allocation_exclusions_0ca12c38",
        "dispatch_allocation_exclusions",
        "dispatch_claim_reservation_id",
    ),
    (
        "ix_fk_dispatch_allocation_release_batch_items_e39c4a1a",
        "dispatch_allocation_release_batch_items",
        "dispatch_claim_reservation_id",
    ),
    (
        "ix_fk_dispatch_allocation_release_batch_items_975f1496",
        "dispatch_allocation_release_batch_items",
        "assignment_id",
    ),
    (
        "ix_fk_dispatch_claim_reservations_5d9860a3",
        "dispatch_claim_reservations",
        "dispatch_claim_task_allocation_id",
    ),
    (
        "ix_fk_pending_visibility_credits_25db9b52",
        "pending_visibility_credits",
        "task_account_daily_coverage_id",
    ),
    (
        "ix_fk_pending_visibility_credits_bb4203bf",
        "pending_visibility_credits",
        "task_day_ledger_id",
    ),
    ("ix_fk_reaction_remote_facts_d4ee6055", "reaction_remote_facts", "obligation_id"),
    (
        "ix_fk_search_click_opportunity_assignments_237afe21",
        "search_click_opportunity_assignments",
        "obligation_id",
    ),
    (
        "ix_fk_search_click_opportunity_assignments_43291949",
        "search_click_opportunity_assignments",
        "task_day_ledger_id",
    ),
    (
        "ix_fk_search_click_solver_carrier_unit_bindings_ad7c1585",
        "search_click_solver_carrier_unit_bindings",
        "dispatch_claim_reservation_id",
    ),
    (
        "ix_fk_search_click_solver_carrier_unit_bindings_f3bc84a7",
        "search_click_solver_carrier_unit_bindings",
        "obligation_id",
    ),
    (
        "ix_fk_task_account_daily_coverage_5d113c59",
        "task_account_daily_coverage",
        "membership_item_id",
    ),
    (
        "ix_fk_task_account_daily_coverage_64353a2f",
        "task_account_daily_coverage",
        "task_day_ledger_id",
    ),
    (
        "ix_fk_task_group_daily_targets_88d4107f",
        "task_group_daily_targets",
        "task_day_ledger_id",
    ),
    (
        "ix_fk_task_start_operations_7b3819cd",
        "task_start_operations",
        "task_day_ledger_id",
    ),
    ("ix_fk_view_remote_facts_a4a4805d", "view_remote_facts", "obligation_id"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)


def downgrade() -> None:
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
