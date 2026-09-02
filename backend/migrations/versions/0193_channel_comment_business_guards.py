"""Freeze channel comment business caps on plan contracts.

Revision ID: 0193_comment_business_guards
Revises: 0192_comment_quality_target
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0193_comment_business_guards"
down_revision = "0192_comment_quality_target"
branch_labels = None
depends_on = None


PLAN_TABLE = "channel_comment_plan_contracts"


def upgrade() -> None:
    _add_columns()
    _backfill_legacy_plans()


def _add_columns() -> None:
    columns = (
        sa.Column(
            "uncapped_required_distinct_account_count",
            sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "business_max_comments_per_message",
            sa.Integer(), nullable=False, server_default="80",
        ),
        sa.Column(
            "business_cap_state",
            sa.String(length=32), nullable=False, server_default="not_adjusted",
        ),
        sa.Column(
            "planned_fallback_max_bps",
            sa.Integer(), nullable=False, server_default="2000",
        ),
    )
    for column in columns:
        if not _has_column(PLAN_TABLE, column.name):
            op.add_column(PLAN_TABLE, column)


def _backfill_legacy_plans() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    plans = sa.Table(PLAN_TABLE, metadata, autoload_with=bind)
    required = plans.c.required_distinct_account_count
    bind.execute(plans.update().values(
        uncapped_required_distinct_account_count=required,
        business_max_comments_per_message=required,
        business_cap_state="legacy_unbounded",
        planned_fallback_max_bps=10000,
    ))


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(
        row["name"] == column_name
        for row in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def downgrade() -> None:
    for column_name in (
        "planned_fallback_max_bps",
        "business_cap_state",
        "business_max_comments_per_message",
        "uncapped_required_distinct_account_count",
    ):
        if _has_column(PLAN_TABLE, column_name):
            op.drop_column(PLAN_TABLE, column_name)
