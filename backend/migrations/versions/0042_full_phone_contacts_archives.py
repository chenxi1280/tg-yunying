"""full phone for contacts and archives

Revision ID: 0042_phone_contacts_archives
Revises: 0041_account_security_batches
Create Date: 2026-05-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0042_phone_contacts_archives"
down_revision = "0041_account_security_batches"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _drop_column_if_exists(table: str, name: str) -> None:
    if name in _columns(table):
        op.drop_column(table, name)


def upgrade() -> None:
    _add_column_if_missing("tg_contacts", sa.Column("phone_ciphertext", sa.Text(), nullable=True))
    _add_column_if_missing("archived_messages", sa.Column("sender_phone_masked", sa.String(length=60), nullable=False, server_default=""))
    _add_column_if_missing("archived_messages", sa.Column("sender_phone_ciphertext", sa.Text(), nullable=True))
    _add_column_if_missing("archived_members", sa.Column("phone_masked", sa.String(length=60), nullable=False, server_default=""))
    _add_column_if_missing("archived_members", sa.Column("phone_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    _drop_column_if_exists("archived_members", "phone_ciphertext")
    _drop_column_if_exists("archived_members", "phone_masked")
    _drop_column_if_exists("archived_messages", "sender_phone_ciphertext")
    _drop_column_if_exists("archived_messages", "sender_phone_masked")
    _drop_column_if_exists("tg_contacts", "phone_ciphertext")
