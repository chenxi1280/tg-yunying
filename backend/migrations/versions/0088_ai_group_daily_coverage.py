"""add AI group all-account daily coverage ledger

Revision ID: 0088_ai_group_daily_coverage
Revises: 0087_rank_deboost_hardening
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0088_ai_group_daily_coverage"
down_revision = "0087_rank_deboost_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _table_exists("task_account_daily_coverage"):
        _create_coverage_table()
    if not _table_exists("account_eligibility_events"):
        _create_event_table()


def downgrade() -> None:
    if _table_exists("account_eligibility_events"):
        op.drop_table("account_eligibility_events")
    if _table_exists("task_account_daily_coverage"):
        op.drop_table("task_account_daily_coverage")


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _create_coverage_table() -> None:
    op.create_table(
        "task_account_daily_coverage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column(
            "membership_item_id", sa.Integer(),
            sa.ForeignKey("task_membership_admission_items.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("coverage_date", sa.Date(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=40), nullable=False, server_default="pending_admission"),
        sa.Column("reserved_action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("last_success_action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("last_remote_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("blocker_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("blocker_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("targeted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "group_id", "account_id", "coverage_date",
            name="uq_task_daily_coverage_obligation",
        ),
    )
    op.create_index(
        "ix_task_daily_coverage_task_date_state",
        "task_account_daily_coverage",
        ["task_id", "coverage_date", "state", "next_eligible_at"],
    )
    op.create_index(
        "ix_task_daily_coverage_account_date",
        "task_account_daily_coverage",
        ["tenant_id", "account_id", "coverage_date"],
    )


def _create_event_table() -> None:
    op.create_table(
        "account_eligibility_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_account_eligibility_events_pending",
        "account_eligibility_events",
        ["processed_at", "next_attempt_at", "occurred_at"],
    )
    op.create_index(
        "ix_account_eligibility_events_account",
        "account_eligibility_events",
        ["tenant_id", "account_id", "occurred_at"],
    )
