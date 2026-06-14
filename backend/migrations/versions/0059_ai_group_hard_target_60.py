"""lower AI group hourly hard target default

Revision ID: 0059_ai_group_hard_target_60
Revises: 0058_membership_challenge
Create Date: 2026-06-14
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0059_ai_group_hard_target_60"
down_revision = "0058_membership_challenge"
branch_labels = None
depends_on = None


GROUP_AI_CHAT = "group_ai_chat"
OLD_DEFAULT_HOURLY_MIN_MESSAGES = 300
NEW_DEFAULT_HOURLY_MIN_MESSAGES = 60

tasks_table = sa.table(
    "tasks",
    sa.column("id", sa.String),
    sa.column("type", sa.String),
    sa.column("type_config", sa.JSON),
    sa.column("stats", sa.JSON),
    sa.column("updated_at", sa.DateTime(timezone=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    current_time = datetime.now(timezone.utc)
    for row in _candidate_tasks(bind):
        values = _task_update_values(row.type_config, row.stats, current_time)
        if not values:
            continue
        bind.execute(sa.update(tasks_table).where(tasks_table.c.id == row.id).values(**values))


def downgrade() -> None:
    # One-way policy change: do not raise operator targets during rollback.
    pass


def _candidate_tasks(bind):
    return bind.execute(
        sa.select(tasks_table.c.id, tasks_table.c.type_config, tasks_table.c.stats)
        .where(tasks_table.c.type == GROUP_AI_CHAT)
        .where(tasks_table.c.deleted_at.is_(None))
    )


def _task_update_values(raw_config: Any, raw_stats: Any, current_time: datetime) -> dict[str, Any] | None:
    should_lower = _should_lower_default(dict(raw_config or {}))
    if not should_lower:
        return None
    config = _hard_hourly_config(raw_config)
    return {
        "type_config": config,
        "stats": _hard_hourly_stats(raw_stats, should_lower),
        "updated_at": current_time,
    }


def _hard_hourly_config(raw_config: Any) -> dict[str, Any]:
    config = dict(raw_config or {})
    if not _should_lower_default(config):
        return config
    config["hourly_min_messages"] = NEW_DEFAULT_HOURLY_MIN_MESSAGES
    return config


def _hard_hourly_stats(raw_stats: Any, should_lower: bool) -> dict[str, Any]:
    if not should_lower:
        return dict(raw_stats or {})
    stats = dict(raw_stats or {})
    stats["hard_hourly_target_enabled"] = True
    stats["hard_hourly_goal"] = NEW_DEFAULT_HOURLY_MIN_MESSAGES
    stats["hard_hourly_status"] = "catching_up"
    for key in STALE_HARD_HOURLY_STAT_KEYS:
        stats.pop(key, None)
    return stats


def _should_lower_default(config: dict[str, Any]) -> bool:
    return (
        bool(config.get("hard_hourly_target_enabled"))
        and _int_value(config.get("hourly_min_messages")) == OLD_DEFAULT_HOURLY_MIN_MESSAGES
    )


def _int_value(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


STALE_HARD_HOURLY_STAT_KEYS = {
    "hard_hourly_bucket",
    "hard_hourly_deficit",
    "hard_hourly_last_blockers",
    "hard_hourly_next_check_at",
    "hard_hourly_open_count",
    "hard_hourly_overdue_open_count",
    "hard_hourly_recent_buckets",
    "hard_hourly_success_count",
}
