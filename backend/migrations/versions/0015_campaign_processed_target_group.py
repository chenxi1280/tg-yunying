"""campaign processed messages target group

Revision ID: 0015_processed_target_group
Revises: 0014_commercial_config
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_processed_target_group"
down_revision = "0014_commercial_config"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _drop_processed_unique_constraints() -> None:
    inspector = sa.inspect(_bind())
    source_unique = {"campaign_id", "source_group_id", "source_remote_message_id"}
    target_unique = {"campaign_id", "source_group_id", "source_remote_message_id", "target_group_id"}
    for constraint in inspector.get_unique_constraints("campaign_processed_messages"):
        columns = set(constraint.get("column_names") or [])
        if columns in (source_unique, target_unique):
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, "campaign_processed_messages", type_="unique")


def upgrade() -> None:
    if _table_exists("campaigns"):
        op.execute(
            "UPDATE campaigns SET status = '执行中' "
            "WHERE execution_mode IN ('ai_activity', 'mirror_forward') AND status = '排队中'"
        )
    if not _table_exists("campaign_processed_messages"):
        return
    if not _column_exists("campaign_processed_messages", "target_group_id"):
        op.add_column("campaign_processed_messages", sa.Column("target_group_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_campaign_processed_messages_target_group_id",
            "campaign_processed_messages",
            "tg_groups",
            ["target_group_id"],
            ["id"],
        )
    _drop_processed_unique_constraints()
    op.create_unique_constraint(
        "uq_campaign_processed_messages_target",
        "campaign_processed_messages",
        ["campaign_id", "source_group_id", "source_remote_message_id", "target_group_id"],
    )


def downgrade() -> None:
    if _table_exists("campaigns"):
        op.execute(
            "UPDATE campaigns SET status = '排队中' "
            "WHERE execution_mode IN ('ai_activity', 'mirror_forward') AND status = '执行中'"
        )
    if not _table_exists("campaign_processed_messages"):
        return
    _drop_processed_unique_constraints()
    if _column_exists("campaign_processed_messages", "target_group_id"):
        op.drop_constraint("fk_campaign_processed_messages_target_group_id", "campaign_processed_messages", type_="foreignkey")
        op.drop_column("campaign_processed_messages", "target_group_id")
    op.create_unique_constraint(
        "uq_campaign_processed_messages_source",
        "campaign_processed_messages",
        ["campaign_id", "source_group_id", "source_remote_message_id"],
    )
