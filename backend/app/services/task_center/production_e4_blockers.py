"""Diagnostic acceptance failures, separate from scheduling and execution."""
from typing import Any

SUPPORTED_TASK_TYPES = {"group_ai_chat", "search_click", "channel_view"}


def e4_blockers(snapshot: dict[str, Any]) -> list[str]:
    blockers = _common_blockers(snapshot)
    if not snapshot.get("ledger_id"):
        return blockers
    task_type = str(snapshot.get("task_type") or "")
    if task_type == "group_ai_chat":
        blockers.extend(_group_blockers(snapshot))
    elif task_type == "search_click":
        blockers.extend(_search_blockers(snapshot))
    elif task_type == "channel_view":
        blockers.extend(_view_blockers(snapshot))
    elif task_type not in SUPPORTED_TASK_TYPES:
        blockers.append("unsupported_task_type")
    return blockers


def _common_blockers(snapshot: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if snapshot.get("task_status") == "missing":
        blockers.append("task_missing")
    elif snapshot.get("task_status") not in {"running", "completed"}:
        blockers.append("task_not_active")
    if snapshot.get("task_deleted"):
        blockers.append("task_deleted")
    if not snapshot.get("ledger_id"):
        blockers.append("task_day_ledger_missing")
    if snapshot.get("planner_runtime_error"):
        blockers.append("planner_runtime_error")
    return blockers


def _group_blockers(snapshot: dict[str, Any]) -> list[str]:
    daily = dict(snapshot.get("group_daily") or {})
    blockers: list[str] = []
    if int(daily.get("target_row_count") or 0) <= 0:
        blockers.append("ai_daily_target_missing")
    if int(daily.get("confirmed_message_count") or 0) < int(daily.get("due_message_count") or 0):
        blockers.append("ai_daily_due_unmet")
    if int(daily.get("coverage_confirmed_count") or 0) < int(daily.get("coverage_required_count") or 0):
        blockers.append("ai_daily_coverage_unmet")
    if int(daily.get("post_release_remote_fact_count") or 0) <= 0:
        blockers.append("ai_post_release_remote_fact_missing")
    return blockers


def _search_blockers(snapshot: dict[str, Any]) -> list[str]:
    click = dict(snapshot.get("search_click") or {})
    required = int(click.get("required_count") or 0)
    blockers = ["search_click_obligation_missing"] if required <= 0 else []
    if int(click.get("confirmed_count") or 0) < required:
        blockers.append("search_click_unmet")
    if int(click.get("post_release_confirmed_count") or 0) <= 0:
        blockers.append("search_click_post_release_fact_missing")
    return blockers


def _view_blockers(snapshot: dict[str, Any]) -> list[str]:
    view = dict(snapshot.get("channel_view") or {})
    required = int(view.get("required_count") or 0)
    materialized_gap = int(
        view.get("materialization_gap")
        if view.get("materialization_gap") is not None
        else max(0, required - int(view.get("materialized_count") or 0))
    )
    confirmation_gap = int(
        view.get("confirmation_gap")
        if view.get("confirmation_gap") is not None
        else max(0, required - int(view.get("confirmed_count") or 0))
    )
    source_state = str(view.get("source_state") or "")
    blockers: list[str] = []
    if source_state == "listener_stalled":
        blockers.append("channel_view_listener_stalled")
    elif source_state == "waiting_for_source":
        blockers.append("channel_view_waiting_for_source")
    elif source_state == "source_empty_terminal":
        blockers.append("channel_view_source_empty_terminal")
    elif required <= 0:
        blockers.append("channel_view_not_due")
    unique_capacity = dict(view.get("unique_account_capacity_shortfall") or {})
    if (
        str(view.get("capacity_warning") or "")
        or int(unique_capacity.get("deficit_count") or 0) > 0
    ):
        blockers.append("channel_view_structural_capacity_shortfall")
    if materialized_gap > 0:
        blockers.append("channel_view_due_unmaterialized")
    if confirmation_gap > 0:
        blockers.append("channel_view_unmet")
    remote_fact_gap = int(
        view.get("remote_fact_gap")
        if view.get("remote_fact_gap") is not None
        else max(
            0,
            int(view.get("confirmed_count") or 0)
            - int(view.get("remote_fact_count") or 0),
        )
    )
    if remote_fact_gap > 0:
        blockers.append("channel_view_remote_fact_missing")
    if int(view.get("post_release_remote_fact_count") or 0) <= 0:
        blockers.append("channel_view_post_release_fact_missing")
    return blockers
