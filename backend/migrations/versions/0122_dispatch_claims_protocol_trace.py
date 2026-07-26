"""Persist search-join protocol recovery traces.

Revision ID: 0122_claim_protocol_trace
Revises: 0121_daily_fulfillment_contracts
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0122_claim_protocol_trace"
down_revision = "0121_daily_fulfillment_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_protocol_trace_table()
    _create_dispatch_claim_tables()


def _create_protocol_trace_table() -> None:
    if _has_table("search_join_protocol_traces"):
        return
    op.create_table(
        "search_join_protocol_traces",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bot_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("protocol_sample_version", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("recovery_kind", sa.String(length=40), nullable=False, server_default="initial"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="observed"),
        sa.Column("event_type", sa.String(length=60), nullable=False, server_default="page_classified"),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_phase", sa.String(length=60), nullable=False, server_default="unknown_page"),
        sa.Column("post_reset_page_phase", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("trace_summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", "recovery_kind", name="uq_search_join_protocol_trace_recovery"),
    )
    op.create_index("ix_search_join_protocol_trace_task_created", "search_join_protocol_traces", ["task_id", "created_at"])


def _create_dispatch_claim_tables() -> None:
    _create_dispatch_claim_scope_table()
    _create_dispatch_claim_window_table()
    _create_dispatch_claim_shard_allocation_table()
    _create_dispatch_claim_reservation_table()



def _create_dispatch_claim_scope_table() -> None:
    if _has_table("dispatch_claim_scopes"):
        return
    op.create_table(
        "dispatch_claim_scopes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dispatcher_scope", sa.String(length=80), nullable=False, unique=True),
        sa.Column("claim_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dispatch_claim_scope_updated", "dispatch_claim_scopes", ["updated_at"])


def _create_dispatch_claim_window_table() -> None:
    if _has_table("dispatch_claim_windows"):
        return
    op.create_table(
        "dispatch_claim_windows",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dispatcher_scope", sa.String(length=80), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unclaimed_allocated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allocation_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dispatcher_scope", "bucket_start", "bucket_end", name="uq_dispatch_claim_window_scope_bucket"),
    )
    op.create_index("ix_dispatch_claim_window_scope_start", "dispatch_claim_windows", ["dispatcher_scope", "bucket_start"])


def _create_dispatch_claim_shard_allocation_table() -> None:
    if _has_table("dispatch_claim_shard_allocations"):
        return
    op.create_table(
        "dispatch_claim_shard_allocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dispatch_claim_window_id", sa.String(length=36), sa.ForeignKey("dispatch_claim_windows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_shard_total", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("account_shard_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required_claims", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unclaimed_allocated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dispatch_claim_window_id", "account_shard_total", "account_shard_index", name="uq_dispatch_claim_shard_window"),
    )
    op.create_index("ix_dispatch_claim_shard_window", "dispatch_claim_shard_allocations", ["dispatch_claim_window_id"])


def _create_dispatch_claim_reservation_table() -> None:
    if _has_table("dispatch_claim_reservations"):
        return
    op.create_table(
        "dispatch_claim_reservations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dispatch_claim_shard_allocation_id", sa.String(length=36), sa.ForeignKey("dispatch_claim_shard_allocations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("claim_class", sa.String(length=40), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required_claims", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_claims", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("urgency_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dispatch_claim_shard_allocation_id", "tenant_id", "task_id", "claim_class", name="uq_dispatch_claim_reservation_scope"),
    )
    op.create_index("ix_dispatch_claim_reservation_allocation", "dispatch_claim_reservations", ["dispatch_claim_shard_allocation_id", "claim_class"])


def downgrade() -> None:
    for table in ("dispatch_claim_reservations", "dispatch_claim_shard_allocations", "dispatch_claim_windows"):
        if _has_table(table):
            op.drop_table(table)
    if _has_table("search_join_protocol_traces"):
        op.drop_table("search_join_protocol_traces")
    if _has_table("dispatch_claim_scopes"):
        op.drop_table("dispatch_claim_scopes")


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()
