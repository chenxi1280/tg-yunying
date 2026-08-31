"""Freeze group-clone config on source events.

Revision ID: 0183_clone_event_config_snapshot
Revises: 0182_ai_provider_request_id
"""

from alembic import op
import sqlalchemy as sa


revision = "0183_clone_event_config_snapshot"
down_revision = "0182_ai_provider_request_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clone_source_events",
        sa.Column("config_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "clone_source_events",
        sa.Column("sender_name", sa.String(length=160), nullable=False, server_default=""),
    )
    _backfill_config_snapshots(op.get_bind())
    op.alter_column(
        "clone_source_events", "config_snapshot",
        existing_type=sa.JSON(), nullable=False,
    )


def _backfill_config_snapshots(bind) -> None:
    events = sa.table(
        "clone_source_events",
        sa.column("task_id", sa.String()),
        sa.column("config_snapshot", sa.JSON()),
    )
    tasks = sa.table(
        "tasks",
        sa.column("id", sa.String()),
        sa.column("type_config", sa.JSON()),
    )
    task_config = sa.select(tasks.c.type_config).where(
        tasks.c.id == events.c.task_id,
    ).scalar_subquery()
    bind.execute(events.update().values(config_snapshot=task_config))
    missing = bind.scalar(sa.select(sa.func.count()).select_from(events).where(
        sa.or_(
            events.c.config_snapshot.is_(None),
            sa.cast(events.c.config_snapshot, sa.Text()) == "{}",
        ),
    ))
    if missing:
        raise RuntimeError("clone_source_events config snapshot backfill incomplete")


def downgrade() -> None:
    op.drop_column("clone_source_events", "sender_name")
    op.drop_column("clone_source_events", "config_snapshot")
