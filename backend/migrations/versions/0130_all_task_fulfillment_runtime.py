"""Add all-task fulfillment contracts and current-state operations.

Revision ID: 0130_fulfillment_runtime
Revises: 0129_ai_memory_account_mask
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401


revision = "0130_fulfillment_runtime"
down_revision = "0129_ai_memory_account_mask"
branch_labels = None
depends_on = None


FULFILLMENT_TABLES = (
    "task_day_ledgers",
    "task_group_daily_message_slots",
    "content_mix_contracts",
    "content_mix_cycles",
    "content_mix_cycle_slots",
    "content_mix_obligations",
    "comment_fulfillment_obligations",
    "reaction_fulfillment_obligations",
    "reaction_remote_facts",
    "view_fulfillment_obligations",
    "view_remote_facts",
    "search_click_fulfillment_obligations",
    "consistency_quarantines",
    "task_day_ledger_lifecycle_events",
    "task_start_operations",
)

DISPATCH_COLUMNS = {
    "dispatch_claim_scopes": {
        "opportunity_cursor": ("0", False),
    },
    "dispatch_claim_windows": {
        "allocation_state": ("'ready'", False),
        "rebuild_input_hash": ("''", False),
        "pending_rebuild_release_count": ("0", False),
        "allocation_scope_version": ("0", False),
        "allocation_scope_active_count": ("0", False),
        "rebuild_input_version": ("0", False),
        "ready_rebuild_snapshot_hash": ("''", False),
    },
    "dispatch_claim_shard_allocations": {
        "dispatch_allocation_epoch": ("1", False),
        "rebuild_input_hash": ("''", False),
        "dispatch_rebuild_snapshot_hash": ("''", False),
    },
    "dispatch_claim_reservations": {
        "dispatch_claim_task_allocation_id": (None, True),
        "dispatch_allocation_epoch": ("1", False),
        "rebuild_input_hash": ("''", False),
        "dispatch_rebuild_snapshot_hash": ("''", False),
        "bound_count": ("0", False),
        "released_count": ("0", False),
    },
}

EXISTING_COLUMNS = {
    "tasks": {
        "created_by_user_id": (None, True),
        "create_task_type": (None, True),
        "client_request_id": (None, True),
        "request_fingerprint": (None, True),
        "request_field_hashes": ("'{}'", False),
        "idempotency_legacy_unproven": ("true", False),
    },
    "actions": {
        "primary_quantity_slot_id": (None, True),
        "content_mix_cycle_slot_id": (None, True),
        "content_mix_slot_attempt": (None, True),
    },
    "pending_visibility_credits": {
        "task_day_ledger_id": (None, True),
        "primary_quantity_slot_id": (None, True),
        "task_account_daily_coverage_id": (None, True),
        "admission_version": (None, True),
    },
    "task_account_daily_coverage": {
        "task_day_ledger_id": (None, True),
    },
    "task_group_daily_targets": {
        "task_day_ledger_id": (None, True),
    },
}


def upgrade() -> None:
    for table_name in FULFILLMENT_TABLES:
        _create_model_table(table_name)
    _create_model_table("dispatch_claim_task_allocations")
    for table_name, columns in {**DISPATCH_COLUMNS, **EXISTING_COLUMNS}.items():
        for column_name, (default, nullable) in columns.items():
            _add_model_column(
                table_name,
                column_name,
                server_default=default,
                nullable=nullable,
            )
    _replace_dispatch_shard_identity()
    _create_runtime_constraints()
    _create_runtime_foreign_keys()


def _create_model_table(table_name: str) -> None:
    if table_name in sa.inspect(op.get_bind()).get_table_names():
        return
    Base.metadata.tables[table_name].create(op.get_bind(), checkfirst=True)


def _add_model_column(
    table_name: str,
    column_name: str,
    *,
    server_default: str | None,
    nullable: bool,
) -> None:
    if _has_column(table_name, column_name):
        return
    model_column = Base.metadata.tables[table_name].c[column_name]
    column = sa.Column(
        column_name,
        model_column.type,
        nullable=nullable,
        server_default=sa.text(server_default) if server_default else None,
    )
    op.add_column(table_name, column)


def _replace_dispatch_shard_identity() -> None:
    if _has_constraint(
        "dispatch_claim_shard_allocations",
        "uq_dispatch_claim_shard_window",
    ):
        op.drop_constraint(
            "uq_dispatch_claim_shard_window",
            "dispatch_claim_shard_allocations",
            type_="unique",
        )
    if not _has_constraint(
        "dispatch_claim_shard_allocations",
        "uq_dispatch_claim_shard_window_epoch",
    ):
        op.create_unique_constraint(
            "uq_dispatch_claim_shard_window_epoch",
            "dispatch_claim_shard_allocations",
            (
                "dispatch_claim_window_id",
                "dispatch_allocation_epoch",
                "account_shard_total",
                "account_shard_index",
            ),
        )
    if _has_index(
        "dispatch_claim_shard_allocations",
        "ix_dispatch_claim_shard_window",
    ):
        op.drop_index(
            "ix_dispatch_claim_shard_window",
            table_name="dispatch_claim_shard_allocations",
        )
    op.create_index(
        "ix_dispatch_claim_shard_window",
        "dispatch_claim_shard_allocations",
        ("dispatch_claim_window_id", "dispatch_allocation_epoch"),
    )


def _create_runtime_constraints() -> None:
    _create_unique(
        "tasks",
        "uq_tasks_create_idempotency",
        ("created_by_user_id", "create_task_type", "client_request_id"),
    )
    _create_unique(
        "actions",
        "uq_actions_content_mix_slot_attempt",
        ("content_mix_cycle_slot_id", "content_mix_slot_attempt"),
    )
    if not _has_index(
        "pending_visibility_credits",
        "uq_pending_visibility_credit_open_slot",
    ):
        op.create_index(
            "uq_pending_visibility_credit_open_slot",
            "pending_visibility_credits",
            ("primary_quantity_slot_id",),
            unique=True,
            postgresql_where=sa.text("status IN ('open', 'unknown')"),
        )


def _create_runtime_foreign_keys() -> None:
    foreign_keys = (
        ("dispatch_claim_reservations", "dispatch_claim_task_allocation_id", "dispatch_claim_task_allocations", "id", "fk_dispatch_reservation_task_allocation"),
        ("actions", "primary_quantity_slot_id", "task_group_daily_message_slots", "id", "fk_actions_primary_quantity_slot"),
        ("actions", "content_mix_cycle_slot_id", "content_mix_cycle_slots", "id", "fk_actions_content_mix_cycle_slot"),
        ("pending_visibility_credits", "task_day_ledger_id", "task_day_ledgers", "id", "fk_pending_visibility_task_day_ledger"),
        ("pending_visibility_credits", "primary_quantity_slot_id", "task_group_daily_message_slots", "id", "fk_pending_visibility_quantity_slot"),
        ("pending_visibility_credits", "task_account_daily_coverage_id", "task_account_daily_coverage", "id", "fk_pending_visibility_coverage"),
        ("task_account_daily_coverage", "task_day_ledger_id", "task_day_ledgers", "id", "fk_task_account_coverage_day_ledger"),
        ("task_group_daily_targets", "task_day_ledger_id", "task_day_ledgers", "id", "fk_group_daily_target_day_ledger"),
    )
    for table, local, remote_table, remote, name in foreign_keys:
        if _has_foreign_key(table, name):
            continue
        op.create_foreign_key(
            name,
            table,
            remote_table,
            (local,),
            (remote,),
        )


def _create_unique(table: str, name: str, columns: tuple[str, ...]) -> None:
    if not _has_constraint(table, name):
        op.create_unique_constraint(name, table, columns)


def _has_column(table: str, column: str) -> bool:
    return column in {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)
    }


def _has_constraint(table: str, name: str) -> bool:
    return name in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
    }


def _has_index(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)
    }


def _has_foreign_key(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
    }


def downgrade() -> None:
    raise RuntimeError("0130 fulfillment migration is forward-only")
