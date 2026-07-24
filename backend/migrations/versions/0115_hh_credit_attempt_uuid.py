"""Store hard-hourly credit execution-attempt UUIDs faithfully.

Revision ID: 0115_hh_credit_attempt_uuid
Revises: 0114_ai_continuity_lifecycle
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0115_hh_credit_attempt_uuid"
down_revision = "0114_ai_continuity_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "SELECT count(*) FROM task_hard_hourly_delivery_credits "
            "WHERE execution_attempt_id IS NOT NULL"
        )
    ).scalar_one()
    if existing:
        raise RuntimeError(
            "task_hard_hourly_delivery_credits contains integer attempt references; "
            "repair them before applying 0114"
        )
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "task_hard_hourly_delivery_credits",
            "execution_attempt_id",
            existing_type=sa.Integer(),
            type_=sa.String(length=36),
            existing_nullable=True,
            postgresql_using="execution_attempt_id::text",
        )
        return
    with op.batch_alter_table("task_hard_hourly_delivery_credits") as batch:
        batch.alter_column(
            "execution_attempt_id",
            existing_type=sa.Integer(),
            type_=sa.String(length=36),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "task_hard_hourly_delivery_credits",
            "execution_attempt_id",
            existing_type=sa.String(length=36),
            type_=sa.Integer(),
            existing_nullable=True,
            postgresql_using="NULL",
        )
        return
    with op.batch_alter_table("task_hard_hourly_delivery_credits") as batch:
        batch.alter_column(
            "execution_attempt_id",
            existing_type=sa.String(length=36),
            type_=sa.Integer(),
            existing_nullable=True,
        )
