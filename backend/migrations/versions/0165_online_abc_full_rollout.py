"""Add frozen-N rollout plans and source-less C provision support.

Revision ID: 0165_online_abc_full
Revises: 0164_online_abc_exec_sha
"""

from alembic import op
import sqlalchemy as sa


revision = "0165_online_abc_full"
down_revision = "0164_online_abc_exec_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tg_authorization_online_abc_batches",
        sa.Column("selection_mode", sa.String(32), nullable=False, server_default="exact_ten_canary"),
    )
    op.add_column(
        "tg_authorization_online_abc_items",
        sa.Column("standby_1_plan", sa.String(32), nullable=False, server_default="provision"),
    )
    op.add_column(
        "tg_authorization_online_abc_items",
        sa.Column("standby_2_plan", sa.String(32), nullable=False, server_default="migrate"),
    )
    op.alter_column("tg_authorization_online_abc_items", "source_c_authorization_id", nullable=True)
    op.alter_column("tg_authorization_online_abc_items", "primary_authorization_id", nullable=True)
    op.alter_column("tg_authorization_online_abc_items", "app_b_id", nullable=True)
    op.alter_column("tg_authorization_online_abc_items", "proxy_id", nullable=True)
    op.alter_column("tg_authorization_dr_batch_items", "expected_source_authorization_id", nullable=True)
    op.alter_column("tg_authorization_slot_decisions", "expected_old_authorization_id", nullable=True)


def downgrade() -> None:
    op.alter_column("tg_authorization_slot_decisions", "expected_old_authorization_id", nullable=False)
    op.alter_column("tg_authorization_dr_batch_items", "expected_source_authorization_id", nullable=False)
    op.alter_column("tg_authorization_online_abc_items", "source_c_authorization_id", nullable=False)
    op.alter_column("tg_authorization_online_abc_items", "proxy_id", nullable=False)
    op.alter_column("tg_authorization_online_abc_items", "app_b_id", nullable=False)
    op.alter_column("tg_authorization_online_abc_items", "primary_authorization_id", nullable=False)
    op.drop_column("tg_authorization_online_abc_items", "standby_2_plan")
    op.drop_column("tg_authorization_online_abc_items", "standby_1_plan")
    op.drop_column("tg_authorization_online_abc_batches", "selection_mode")
