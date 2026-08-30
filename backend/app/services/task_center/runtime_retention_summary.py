from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import ActionTerminalDailyStat, DailyRuntimeStat
from app.services._common import _now


DailyStatKey = tuple[date, str, str, str]
TerminalStatKey = tuple[date, str, str, str]


def summarize_actions(actions: list) -> dict[DailyStatKey, int]:
    stats: dict[DailyStatKey, int] = defaultdict(int)
    for action in actions:
        stat_date = _action_date(action)
        status = str(action.status or "unknown")
        _add_existing_dimensions(stats, action, stat_date, status)
    return dict(stats)


def summarize_terminal_actions(
    actions: list,
    reason_by_action_id: dict[str, str],
) -> dict[TerminalStatKey, int]:
    stats: dict[TerminalStatKey, int] = defaultdict(int)
    for action in actions:
        key = (
            _action_date(action),
            str(action.status or "unknown"),
            str(action.action_type or "unknown")[:30],
            reason_by_action_id.get(str(action.id), "unclassified"),
        )
        stats[key] += 1
    return dict(stats)


def upsert_daily_stats(session: Session, stats: dict[DailyStatKey, int]) -> None:
    for key, value in stats.items():
        stat_date, dimension_type, dimension_id, metric_name = key
        insert = _insert(session, DailyRuntimeStat).values(
            stat_date=stat_date,
            dimension_type=dimension_type,
            dimension_id=dimension_id,
            metric_name=metric_name,
            metric_value=int(value),
            updated_at=_now(),
        )
        session.execute(insert.on_conflict_do_update(
            index_elements=["stat_date", "dimension_type", "dimension_id", "metric_name"],
            set_={"metric_value": DailyRuntimeStat.metric_value + insert.excluded.metric_value, "updated_at": _now()},
        ))


def upsert_terminal_stats(session: Session, stats: dict[TerminalStatKey, int]) -> None:
    for key, value in stats.items():
        stat_date, status, action_type, reason_code = key
        insert = _insert(session, ActionTerminalDailyStat).values(
            stat_date=stat_date,
            status=status,
            action_type=action_type,
            reason_code=reason_code,
            action_count=int(value),
            updated_at=_now(),
        )
        session.execute(insert.on_conflict_do_update(
            index_elements=["stat_date", "status", "action_type", "reason_code"],
            set_={"action_count": ActionTerminalDailyStat.action_count + insert.excluded.action_count, "updated_at": _now()},
        ))


def _add_existing_dimensions(
    stats: dict[DailyStatKey, int],
    action,
    stat_date: date,
    status: str,
) -> None:
    dimensions = [("global", "all")]
    dimensions.append(("task", action.task_id or ""))
    if action.account_id is not None:
        dimensions.append(("account", str(action.account_id)))
    if action.task_type:
        dimensions.append(("task_type", action.task_type))
    if action.target_dimension:
        dimensions.append(("target", str(action.target_dimension)))
    for dimension_type, dimension_id in dimensions:
        stats[(stat_date, dimension_type, dimension_id, "total")] += 1
        stats[(stat_date, dimension_type, dimension_id, f"status.{status}")] += 1


def _insert(session: Session, model):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"unsupported runtime retention dialect: {dialect}")


def _action_date(action) -> date:
    value = action.executed_at or action.scheduled_at or action.created_at or _now()
    return value.date()


__all__ = [
    "summarize_actions",
    "summarize_terminal_actions",
    "upsert_daily_stats",
    "upsert_terminal_stats",
]
