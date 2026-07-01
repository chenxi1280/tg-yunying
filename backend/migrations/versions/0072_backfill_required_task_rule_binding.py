"""backfill required task rule bindings

Revision ID: 0072_required_rule_binding
Revises: 0071_ai_group_quality_foundation
Create Date: 2026-07-01
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alembic import context, op
import sqlalchemy as sa


revision = "0072_required_rule_binding"
down_revision = "0071_ai_group_quality_foundation"
branch_labels = None
depends_on = None


REQUIRED_TASK_TYPES = frozenset({"group_relay", "group_ai_chat", "channel_comment"})
ACTIVE_TASK_STATUSES = frozenset({"draft", "pending", "running", "paused"})
DEFAULT_RULE_SET_NAME = "默认运营规则集"
DEFAULT_RULE_SET_DESCRIPTION = "系统初始化的通用规则集，默认不拦截内容，可用于监听转发、AI 回复、AI 评论和普通消息发送。"
DEFAULT_RULE_TASK_TYPES = ["group_relay", "group_ai_chat", "channel_comment", "message_send"]
RULE_BINDING_REQUIRED_MESSAGE = "任务必须绑定已发布规则集版本"

DEFAULT_FILTERS = {
    "keyword_whitelist": [],
    "keyword_blacklist": [],
    "min_message_length": None,
    "max_message_length": None,
    "allowed_media_types": [],
    "blocked_user_ids": [],
    "only_with_media": False,
    "only_text": False,
    "language_filter": None,
}
DEFAULT_OUTPUT_CHECKS = {
    "forbidden_keywords": [],
    "forbid_links": False,
    "forbid_mentions": True,
    "max_length": None,
    "failure_strategy": "transform_once_drop",
}

tasks_table = sa.table(
    "tasks",
    sa.column("id", sa.String),
    sa.column("tenant_id", sa.Integer),
    sa.column("type", sa.String),
    sa.column("status", sa.String),
    sa.column("type_config", sa.JSON),
    sa.column("stats", sa.JSON),
    sa.column("last_error", sa.Text),
    sa.column("next_run_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
)
rule_sets_table = sa.table(
    "rule_sets",
    sa.column("id", sa.Integer),
    sa.column("tenant_id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("description", sa.Text),
    sa.column("status", sa.String),
    sa.column("task_types", sa.JSON),
    sa.column("default_policy", sa.JSON),
    sa.column("active_version_id", sa.Integer),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
rule_versions_table = sa.table(
    "rule_set_versions",
    sa.column("id", sa.Integer),
    sa.column("tenant_id", sa.Integer),
    sa.column("rule_set_id", sa.Integer),
    sa.column("version", sa.Integer),
    sa.column("status", sa.String),
    sa.column("filters", sa.JSON),
    sa.column("output_checks", sa.JSON),
    sa.column("transforms", sa.JSON),
    sa.column("routing", sa.JSON),
    sa.column("account_strategy", sa.JSON),
    sa.column("rate_limits", sa.JSON),
    sa.column("retry_policy", sa.JSON),
    sa.column("created_by", sa.String),
    sa.column("published_by", sa.String),
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    current_time = datetime.now(timezone.utc)
    for tenant_id in _tenant_ids_with_unbound_tasks(bind):
        rule_set_id = _ensure_default_rule_set(bind, tenant_id, current_time)
        for row in _unbound_tasks(bind, tenant_id):
            values = _task_update_values(
                row.type_config,
                row.stats,
                status=row.status,
                last_error=row.last_error,
                rule_set_id=rule_set_id,
                current_time=current_time,
            )
            bind.execute(sa.update(tasks_table).where(tasks_table.c.id == row.id).values(**values))


def downgrade() -> None:
    pass


def _tenant_ids_with_unbound_tasks(bind) -> list[int]:
    rows = bind.execute(
        sa.select(tasks_table.c.tenant_id)
        .where(tasks_table.c.type.in_(REQUIRED_TASK_TYPES))
        .where(tasks_table.c.status.in_(ACTIVE_TASK_STATUSES))
        .where(tasks_table.c.deleted_at.is_(None))
    )
    return sorted({int(row.tenant_id or 1) for row in rows})


def _unbound_tasks(bind, tenant_id: int):
    rows = bind.execute(
        sa.select(tasks_table.c.id, tasks_table.c.type_config, tasks_table.c.stats, tasks_table.c.status, tasks_table.c.last_error)
        .where(tasks_table.c.tenant_id == tenant_id)
        .where(tasks_table.c.type.in_(REQUIRED_TASK_TYPES))
        .where(tasks_table.c.status.in_(ACTIVE_TASK_STATUSES))
        .where(tasks_table.c.deleted_at.is_(None))
    )
    return [row for row in rows if not _has_rule_binding(row.type_config)]


def _ensure_default_rule_set(bind, tenant_id: int, current_time: datetime) -> int:
    rule_set_id = _default_rule_set_id(bind, tenant_id)
    if rule_set_id is None:
        rule_set_id = _create_default_rule_set(bind, tenant_id, current_time)
    version_id = _published_version_id(bind, tenant_id, rule_set_id)
    if version_id is None:
        version_id = _create_default_rule_version(bind, tenant_id, rule_set_id=rule_set_id, current_time=current_time)
    bind.execute(
        sa.update(rule_sets_table)
        .where(rule_sets_table.c.id == rule_set_id)
        .values(
            status="active",
            task_types=DEFAULT_RULE_TASK_TYPES,
            active_version_id=version_id,
            updated_at=current_time,
        )
    )
    return int(rule_set_id)


def _default_rule_set_id(bind, tenant_id: int) -> int | None:
    return bind.scalar(
        sa.select(rule_sets_table.c.id)
        .where(rule_sets_table.c.tenant_id == tenant_id)
        .where(rule_sets_table.c.name == DEFAULT_RULE_SET_NAME)
        .order_by(rule_sets_table.c.id.asc())
        .limit(1)
    )


def _create_default_rule_set(bind, tenant_id: int, current_time: datetime) -> int:
    return int(
        bind.execute(
            sa.insert(rule_sets_table)
            .values(
                tenant_id=tenant_id,
                name=DEFAULT_RULE_SET_NAME,
                description=DEFAULT_RULE_SET_DESCRIPTION,
                status="active",
                task_types=DEFAULT_RULE_TASK_TYPES,
                default_policy={"version_binding": "follow_current"},
                updated_at=current_time,
            )
            .returning(rule_sets_table.c.id)
        ).scalar_one()
    )


def _published_version_id(bind, tenant_id: int, rule_set_id: int) -> int | None:
    return bind.scalar(
        sa.select(rule_versions_table.c.id)
        .where(rule_versions_table.c.tenant_id == tenant_id)
        .where(rule_versions_table.c.rule_set_id == rule_set_id)
        .where(rule_versions_table.c.status == "published")
        .order_by(rule_versions_table.c.version.desc(), rule_versions_table.c.id.desc())
        .limit(1)
    )


def _create_default_rule_version(bind, tenant_id: int, *, rule_set_id: int, current_time: datetime) -> int:
    return int(
        bind.execute(
            sa.insert(rule_versions_table)
            .values(
                tenant_id=tenant_id,
                rule_set_id=rule_set_id,
                version=_next_version(bind, rule_set_id),
                status="published",
                filters=DEFAULT_FILTERS,
                output_checks=DEFAULT_OUTPUT_CHECKS,
                transforms={},
                routing={},
                account_strategy={},
                rate_limits={},
                retry_policy={},
                created_by="system",
                published_by="system",
                published_at=current_time,
                updated_at=current_time,
            )
            .returning(rule_versions_table.c.id)
        ).scalar_one()
    )


def _next_version(bind, rule_set_id: int) -> int:
    latest = bind.scalar(sa.select(sa.func.max(rule_versions_table.c.version)).where(rule_versions_table.c.rule_set_id == rule_set_id))
    return int(latest or 0) + 1


def _task_update_values(
    raw_config: Any,
    raw_stats: Any,
    *,
    status: str,
    last_error: str,
    rule_set_id: int,
    current_time: datetime,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "type_config": {**dict(raw_config or {}), "rule_set_id": int(rule_set_id)},
        "stats": _clear_rule_binding_blocker(raw_stats),
        "updated_at": current_time,
    }
    if str(last_error or "") == RULE_BINDING_REQUIRED_MESSAGE:
        values["last_error"] = ""
    if str(status or "") == "running":
        values["next_run_at"] = current_time
    return values


def _clear_rule_binding_blocker(raw_stats: Any) -> dict[str, Any]:
    stats = dict(raw_stats or {})
    blockers = dict(stats.get("hard_hourly_last_blockers") or {})
    blockers.pop("rule_binding_missing", None)
    if blockers:
        stats["hard_hourly_last_blockers"] = blockers
    else:
        stats.pop("hard_hourly_last_blockers", None)
    stats.pop("hard_hourly_next_check_at", None)
    return stats


def _has_rule_binding(raw_config: Any) -> bool:
    config = dict(raw_config or {})
    return bool(_positive_int(config.get("rule_set_id")) or _positive_int(config.get("rule_set_version_id")))


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
