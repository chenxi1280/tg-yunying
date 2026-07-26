"""Persist account-mask generation jobs and attempts.

Revision ID: 0124_voice_profile_generation
Revises: 0123_coverage_reservation
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0124_voice_profile_generation"
down_revision = "0123_coverage_reservation"
branch_labels = None
depends_on = None

GENERATION_TABLES = (
    "ai_account_voice_profile_generation_jobs",
    "ai_account_voice_profile_generation_items",
    "ai_account_voice_profile_generation_attempts",
)
OPEN_ITEM_SQL = "status IN ('queued', 'generating', 'validating', 'retry_wait', 'persist_unknown')"


def upgrade() -> None:
    _create_jobs_table()
    _create_items_table()
    _create_attempts_table()
    _create_indexes()


def downgrade() -> None:
    for table in reversed(GENERATION_TABLES):
        if _has_table(table):
            op.drop_table(table)


def _create_jobs_table() -> None:
    table = GENERATION_TABLES[0]
    if _has_table(table):
        return
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_wait_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_ai_voice_profile_generation_job_idempotency"),
    )


def _create_items_table() -> None:
    table = GENERATION_TABLES[1]
    if _has_table(table):
        return
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey(f"{GENERATION_TABLES[0]}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("expected_profile_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("base_profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_profile_version", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("error_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider_request_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("lease_owner", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("previous_item_id", sa.String(length=36), nullable=True),
        sa.Column("operator_idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id", "account_id", name="uq_ai_voice_profile_generation_job_account"),
        sa.UniqueConstraint("tenant_id", "operator_idempotency_key", name="uq_ai_voice_profile_generation_item_operator_key"),
    )


def _create_attempts_table() -> None:
    table = GENERATION_TABLES[2]
    if _has_table(table):
        return
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("job_id", sa.String(length=36), sa.ForeignKey(f"{GENERATION_TABLES[0]}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.String(length=36), sa.ForeignKey(f"{GENERATION_TABLES[1]}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False, server_default="generate"),
        sa.Column("provider", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("provider_request_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False, server_default="running"),
        sa.Column("error_code", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("error_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_feedback_summary", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("item_id", "attempt_no", name="uq_ai_voice_profile_generation_attempt_no"),
    )


def _create_indexes() -> None:
    _create_index("ix_ai_voice_profile_generation_jobs_tenant_status", GENERATION_TABLES[0], ["tenant_id", "status", "created_at"])
    _create_index(
        "uq_ai_voice_profile_generation_open_account",
        GENERATION_TABLES[1],
        ["tenant_id", "account_id"],
        unique=True,
        sqlite_where=sa.text(OPEN_ITEM_SQL),
        postgresql_where=sa.text(OPEN_ITEM_SQL),
    )
    _create_index("ix_ai_voice_profile_generation_items_due", GENERATION_TABLES[1], ["status", "next_retry_at", "created_at"])
    _create_index("ix_ai_voice_profile_generation_items_lease", GENERATION_TABLES[1], ["status", "lease_expires_at"])
    _create_index("ix_ai_voice_profile_generation_attempts_item", GENERATION_TABLES[2], ["item_id", "attempt_no"])


def _create_index(name: str, table: str, columns: list[str], **kwargs) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns, **kwargs)


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    return name in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
