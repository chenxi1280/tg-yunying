"""enforce AI group hourly hard target

Revision ID: 0057_ai_group_hard_target_300
Revises: 0056_login_flow_auth_assets
Create Date: 2026-06-08
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0057_ai_group_hard_target_300"
down_revision = "0056_login_flow_auth_assets"
branch_labels = None
depends_on = None


GROUP_AI_CHAT = "group_ai_chat"
HARD_HOURLY_MIN_MESSAGES = 300
HARD_HOURLY_STRATEGY = "force_planning"
RUNNING_STATUS = "running"

tasks_table = sa.table(
    "tasks",
    sa.column("id", sa.String),
    sa.column("tenant_id", sa.Integer),
    sa.column("type", sa.String),
    sa.column("status", sa.String),
    sa.column("type_config", sa.JSON),
    sa.column("stats", sa.JSON),
    sa.column("next_run_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)

targets_table = sa.table(
    "operation_targets",
    sa.column("id", sa.Integer),
    sa.column("tenant_id", sa.Integer),
    sa.column("target_type", sa.String),
    sa.column("title", sa.String),
)


def upgrade() -> None:
    bind = op.get_bind()
    target_titles = _target_titles(bind)
    current_time = datetime.now(timezone.utc)
    for row in _group_ai_tasks(bind):
        config = _hard_hourly_config(row.type_config, row.tenant_id, target_titles)
        values = _task_update_values(row, config, current_time)
        bind.execute(sa.update(tasks_table).where(tasks_table.c.id == row.id).values(**values))


def downgrade() -> None:
    # One-way data repair: do not disable operator hard targets during rollback.
    pass


def _target_titles(bind) -> dict[tuple[int, int], str]:
    rows = bind.execute(
        sa.select(targets_table.c.tenant_id, targets_table.c.id, targets_table.c.title)
        .where(targets_table.c.target_type == "group")
    )
    return {(int(row.tenant_id), int(row.id)): str(row.title or "") for row in rows}


def _group_ai_tasks(bind):
    return bind.execute(
        sa.select(
            tasks_table.c.id,
            tasks_table.c.tenant_id,
            tasks_table.c.status,
            tasks_table.c.type_config,
            tasks_table.c.stats,
        )
        .where(tasks_table.c.type == GROUP_AI_CHAT)
        .where(tasks_table.c.deleted_at.is_(None))
    )


def _hard_hourly_config(
    raw_config: Any,
    tenant_id: int,
    target_titles: dict[tuple[int, int], str],
) -> dict[str, Any]:
    config = dict(raw_config or {})
    target_id = _int_value(config.get("target_operation_target_id"))
    target_title = target_titles.get((int(tenant_id), target_id or 0), "")
    hourly_min_messages = _minimum_goal(config.get("hourly_min_messages"))
    config["hard_hourly_target_enabled"] = True
    config["hourly_min_messages"] = hourly_min_messages
    config["hard_hourly_strategy"] = HARD_HOURLY_STRATEGY
    if target_title:
        config["target_group_name"] = target_title
    return config


def _task_update_values(row, config: dict[str, Any], current_time: datetime) -> dict[str, Any]:
    values: dict[str, Any] = {
        "type_config": config,
        "stats": _hard_hourly_stats(row.stats, int(config["hourly_min_messages"])),
        "updated_at": current_time,
    }
    if row.status == RUNNING_STATUS:
        values["next_run_at"] = current_time
    return values


def _hard_hourly_stats(raw_stats: Any, hourly_min_messages: int) -> dict[str, Any]:
    stats = dict(raw_stats or {})
    stats["hard_hourly_target_enabled"] = True
    stats["hard_hourly_goal"] = hourly_min_messages
    if not stats.get("hard_hourly_status") or stats.get("hard_hourly_status") == "disabled":
        stats["hard_hourly_status"] = "catching_up"
    for key in STALE_HARD_HOURLY_STAT_KEYS:
        stats.pop(key, None)
    return stats


def _int_value(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _minimum_goal(value: Any) -> int:
    configured = _int_value(value) or 0
    return max(HARD_HOURLY_MIN_MESSAGES, configured)


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
