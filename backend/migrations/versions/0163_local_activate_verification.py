"""Persist local activate send verification facts.

Revision ID: 0163_local_activate_verify
Revises: 0162_online_abc_canary
"""

from alembic import op
import sqlalchemy as sa


revision = "0163_local_activate_verify"
down_revision = "0162_online_abc_canary"
branch_labels = None
depends_on = None


TABLE = "tg_authorization_local_activate_cases"


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    additions = (
        ("verification_operation_id", sa.String(length=36), ""),
        ("verification_remote_message_id", sa.String(length=64), ""),
        ("verification_blocker_code", sa.String(length=100), ""),
        ("verified_at", sa.DateTime(), None),
    )
    for name, column_type, default in additions:
        if name in existing:
            continue
        kwargs = {"nullable": True} if default is None else {
            "nullable": False,
            "server_default": default,
        }
        op.add_column(TABLE, sa.Column(name, column_type, **kwargs))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    for name in (
        "verified_at",
        "verification_blocker_code",
        "verification_remote_message_id",
        "verification_operation_id",
    ):
        if name in existing:
            op.drop_column(TABLE, name)
