"""Persist daily fulfillment, variation, and generation contract facts.

Revision ID: 0121_daily_fulfillment_contracts
Revises: 0120_humanized_admission
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0121_daily_fulfillment_contracts"
down_revision = "0120_humanized_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_coverage_columns()
    _create_variation_intents()
    _create_fulfillment_decisions()
    _create_generation_contract_audits()


def _add_coverage_columns() -> None:
    _add_column_if_missing("task_account_daily_coverage", sa.Column("last_action_id", sa.String(length=36), nullable=True))
    _add_column_if_missing(
        "task_account_daily_coverage",
        sa.Column("blocker_stage", sa.String(length=40), nullable=False, server_default=""),
    )
    _add_column_if_missing(
        "task_account_daily_coverage",
        sa.Column("recovery_path", sa.String(length=80), nullable=False, server_default=""),
    )
    _add_column_if_missing("task_account_daily_coverage", sa.Column("next_decision_at", sa.DateTime(timezone=True), nullable=True))
    _create_index_if_missing(
        "task_account_daily_coverage",
        "ix_task_daily_coverage_last_action",
        ["last_action_id"],
    )


def _create_variation_intents() -> None:
    if _has_table("ai_coverage_variation_intents"):
        return
    op.create_table(
        "ai_coverage_variation_intents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("coverage_ledger_id", sa.String(length=36), sa.ForeignKey("task_account_daily_coverage.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("content_variation_key", sa.String(length=128), nullable=False),
        sa.Column("context_version", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("intent_snapshot_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=60), nullable=False, server_default="planned"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("coverage_ledger_id", "content_variation_key", name="uq_ai_coverage_variation_key"),
    )
    _create_index_if_missing("ai_coverage_variation_intents", "ix_ai_coverage_variation_action", ["action_id"])


def _create_fulfillment_decisions() -> None:
    if _has_table("task_daily_fulfillment_decisions"):
        return
    op.create_table(
        "task_daily_fulfillment_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("coverage_date", sa.Date(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("full_shortfall_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_future_open_cover_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown_hold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_to_plan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_shortfall_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hard_hourly_required_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("next_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_snapshot", sa.JSON(), nullable=False),
    )
    _create_index_if_missing(
        "task_daily_fulfillment_decisions",
        "ix_task_daily_fulfillment_decision_task_date",
        ["task_id", "coverage_date", "decided_at"],
    )


def _create_generation_contract_audits() -> None:
    if _has_table("ai_generation_contract_audits"):
        return
    op.create_table(
        "ai_generation_contract_audits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation_attempt_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("provider_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("model_id", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("prompt_contract_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("parser_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("expected_slot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_slot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slot_summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("restricted_response_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("generation_attempt_id", name="uq_ai_generation_contract_attempt"),
    )
    _create_index_if_missing(
        "ai_generation_contract_audits",
        "ix_ai_generation_contract_task_created",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    for table, index in (
        ("ai_generation_contract_audits", "ix_ai_generation_contract_task_created"),
        ("task_daily_fulfillment_decisions", "ix_task_daily_fulfillment_decision_task_date"),
        ("ai_coverage_variation_intents", "ix_ai_coverage_variation_action"),
        ("task_account_daily_coverage", "ix_task_daily_coverage_last_action"),
    ):
        if _has_index(table, index):
            op.drop_index(index, table_name=table)
    for table in ("ai_generation_contract_audits", "task_daily_fulfillment_decisions", "ai_coverage_variation_intents"):
        if _has_table(table):
            op.drop_table(table)
    for column in ("next_decision_at", "recovery_path", "blocker_stage", "last_action_id"):
        if _has_column("task_account_daily_coverage", column):
            op.drop_column("task_account_daily_coverage", column)


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _create_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns)


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return name in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
