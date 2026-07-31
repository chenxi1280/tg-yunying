"""Persist ordinary group listener remote cursor continuity.

Revision ID: 0133_group_listener_cursor
Revises: 0132_action_pointer_delete
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0133_group_listener_cursor"
down_revision = "0132_action_pointer_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tg_groups",
        sa.Column("listener_remote_cursor", sa.String(length=160), nullable=False, server_default=""),
    )
    op.add_column(
        "tg_groups",
        sa.Column("listener_cursor_status", sa.String(length=20), nullable=False, server_default="unproven"),
    )


def downgrade() -> None:
    raise RuntimeError("0133 listener cursor migration is forward-only")
