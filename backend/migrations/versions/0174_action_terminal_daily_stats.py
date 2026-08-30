"""Add typed terminal Action daily summaries.

Revision ID: 0174_action_terminal_stats
Revises: 0173_channel_view_fact_nav
"""

from alembic import op
import sqlalchemy as sa


revision = "0174_action_terminal_stats"
down_revision = "0173_channel_view_fact_nav"
branch_labels = None
depends_on = None

TABLE = "action_terminal_daily_stats"
LOOKUP_INDEX = "ix_action_terminal_daily_stats_lookup"


def upgrade() -> None:
    if not _has_table():
        _create_table()
    if LOOKUP_INDEX not in _index_names():
        op.create_index(
            LOOKUP_INDEX,
            TABLE,
            ["stat_date", "status", "action_type"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table():
        op.drop_table(TABLE)


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stat_date",
            "status",
            "action_type",
            "reason_code",
            name="uq_action_terminal_daily_stats_bucket",
        ),
    )


def _has_table() -> bool:
    return TABLE in sa.inspect(op.get_bind()).get_table_names()


def _index_names() -> set[str]:
    if not _has_table():
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(TABLE)}
