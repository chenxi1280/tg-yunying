"""worker heartbeats

Revision ID: 0028_worker_heartbeats
Revises: 0027_action_execution_lease
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_worker_heartbeats"
down_revision = "0027_action_execution_lease"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists("worker_heartbeats"):
        return
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=160), nullable=False),
        sa.Column("process_type", sa.String(length=60), nullable=False, server_default="task_center"),
        sa.Column("hostname", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("pid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("heartbeat_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id"),
    )


def downgrade() -> None:
    if _table_exists("worker_heartbeats"):
        op.drop_table("worker_heartbeats")
