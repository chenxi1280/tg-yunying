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
    _add_column(
        "tg_authorization_online_abc_batches",
        sa.Column("selection_mode", sa.String(32), nullable=False, server_default="exact_ten_canary"),
    )
    _add_column(
        "tg_authorization_online_abc_items",
        sa.Column("standby_1_plan", sa.String(32), nullable=False, server_default="provision"),
    )
    _add_column(
        "tg_authorization_online_abc_items",
        sa.Column("standby_2_plan", sa.String(32), nullable=False, server_default="migrate"),
    )
    _set_nullable("tg_authorization_online_abc_items", "source_c_authorization_id", True)
    _set_nullable("tg_authorization_online_abc_items", "primary_authorization_id", True)
    _set_nullable("tg_authorization_online_abc_items", "app_b_id", True)
    _set_nullable("tg_authorization_online_abc_items", "proxy_id", True)
    _set_nullable("tg_authorization_dr_batch_items", "expected_source_authorization_id", True)
    _set_nullable("tg_authorization_slot_decisions", "expected_old_authorization_id", True)


def downgrade() -> None:
    _set_nullable("tg_authorization_slot_decisions", "expected_old_authorization_id", False)
    _set_nullable("tg_authorization_dr_batch_items", "expected_source_authorization_id", False)
    _set_nullable("tg_authorization_online_abc_items", "source_c_authorization_id", False)
    _set_nullable("tg_authorization_online_abc_items", "proxy_id", False)
    _set_nullable("tg_authorization_online_abc_items", "app_b_id", False)
    _set_nullable("tg_authorization_online_abc_items", "primary_authorization_id", False)
    _drop_column("tg_authorization_online_abc_items", "standby_2_plan")
    _drop_column("tg_authorization_online_abc_items", "standby_1_plan")
    _drop_column("tg_authorization_online_abc_batches", "selection_mode")


def _columns(table: str) -> dict[str, dict]:
    return {str(column["name"]): column for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _drop_column(table: str, column: str) -> None:
    if column in _columns(table):
        op.drop_column(table, column)


def _set_nullable(table: str, column: str, nullable: bool) -> None:
    current = _columns(table).get(column)
    if current is None or bool(current["nullable"]) == nullable:
        return
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column, nullable=nullable)
