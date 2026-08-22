"""Track the release that executes a resumed online ABC batch.

Revision ID: 0164_online_abc_exec_sha
Revises: 0163_local_activate_verify
"""

from alembic import op
import sqlalchemy as sa


revision = "0164_online_abc_exec_sha"
down_revision = "0163_local_activate_verify"
branch_labels = None
depends_on = None


TABLE = "tg_authorization_online_abc_batches"
COLUMN = "execution_release_sha"


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN not in existing:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=False, server_default=""))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN in existing:
        op.drop_column(TABLE, COLUMN)
