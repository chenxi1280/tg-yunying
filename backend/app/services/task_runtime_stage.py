from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.models import Action, Task


AI_UNAVAILABLE_MARKERS = ("AI 生成不可用", "没有健康 AI", "read operation timed out", "The read operation timed out")
WAITING_CONTEXT_MARKERS = ("暂无新的真人上下文", "持续监听中", "等待新消息", "等待群内新消息", "上下文不足")
WAITING_COOLDOWN_MARKERS = ("冷却", "慢速模式", "FloodWait", "等待下一轮")
EXECUTING_STATUSES = {"claiming", "executing"}
PENDING_STATUSES = {"pending", "retryable_failed", "unknown_after_send"}


def derive_task_runtime_stage(
    task: Task,
    *,
    actions: Sequence[Action] | None = None,
    membership_phase: Mapping[str, Any] | None = None,
    summary: Any | None = None,
) -> dict[str, Any]:
    status = str(task.status or "")
    if status == "paused":
        return _stage(task, "paused", "已暂停", "danger", _paused_reason(task))
    if status in {"stopped", "failed", "completed", "deleted"}:
        return _terminal_stage(task, status)
    if status in {"draft", "pending"}:
        return _stage(task, "startup_checking", "启动校验中", "warning", "任务尚未进入可执行调度")
    membership = _membership_stage(task, membership_phase)
    if membership and _is_waiting_ai(task):
        return _with_secondary_reason(membership, _ai_reason(task))
    if membership:
        return membership
    if _is_waiting_ai(task):
        return _stage(task, "waiting_ai", "等待 AI", "warning", _ai_reason(task))
    if _is_waiting_context(task):
        return _stage(task, "waiting_context", "等待上下文", "warning", task.last_error or "等待真人上下文或空闲续聊触发")
    if _is_waiting_cooldown(task):
        return _stage(task, "waiting_cooldown", "等待冷却 / 下一轮", "warning", task.last_error or "等待账号、目标或计划冷却")
    if _has_executing_actions(actions, summary):
        return _stage(task, "sending", "发送中", "warning", "已有执行项正在发送或认领资源")
    if _has_future_pending(task, actions, summary):
        return _stage(task, "waiting_next_run", "等待下一轮", "warning", _next_run_reason(task))
    return _stage(task, "running", "运行中", "warning", task.last_error or "任务可继续规划和执行")


def _stage(task: Task, code: str, label: str, severity: str, reason: str) -> dict[str, Any]:
    return {
        "primary_status": str(task.status or ""),
        "primary_status_label": _primary_status_label(task.status),
        "stage_code": code,
        "stage_label": label,
        "severity": severity,
        "reason": reason,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_error": task.last_error or "",
    }


def _terminal_stage(task: Task, status: str) -> dict[str, Any]:
    labels = {"stopped": "已停止", "failed": "失败", "completed": "已完成", "deleted": "已删除"}
    severity = "danger" if status == "failed" else "muted"
    return _stage(task, status, labels.get(status, status), severity, task.last_error or labels.get(status, status))


def _with_secondary_reason(stage: dict[str, Any], reason: str) -> dict[str, Any]:
    if not reason:
        return stage
    next_stage = dict(stage)
    next_stage["secondary_reasons"] = [reason]
    next_stage["reason"] = f"{stage['reason']}；同时存在：{reason}"
    return next_stage


def _primary_status_label(status: Any) -> str:
    value = str(status or "")
    return {
        "draft": "草稿",
        "pending": "待启动",
        "running": "运行中",
        "paused": "已暂停",
        "stopped": "已停止",
        "failed": "失败",
        "completed": "已完成",
        "deleted": "已删除",
    }.get(value, value or "未运行")


def _paused_reason(task: Task) -> str:
    reason = "任务已暂停，不会继续规划或执行新动作"
    return f"{reason}；最近错误：{task.last_error}" if task.last_error else reason


def _is_waiting_ai(task: Task) -> bool:
    stats = task.stats if isinstance(task.stats, dict) else {}
    text = f"{task.last_error or ''} {stats.get('ai_unavailable_reason') or ''}"
    return any(marker.lower() in text.lower() for marker in AI_UNAVAILABLE_MARKERS)


def _ai_reason(task: Task) -> str:
    stats = task.stats if isinstance(task.stats, dict) else {}
    return task.last_error or str(stats.get("ai_unavailable_reason") or "AI 生成不可用，等待恢复后继续执行")


def _membership_stage(task: Task, membership_phase: Mapping[str, Any] | None) -> dict[str, Any] | None:
    phase = membership_phase or _membership_phase_from_stats(task)
    if not phase:
        return None
    status = str(phase.get("status") or phase.get("stage") or "")
    pending = int(phase.get("pending_account_count") or phase.get("need_join_count") or 0)
    running = int(phase.get("running_account_count") or phase.get("running_count") or 0)
    active_statuses = {"pending", "running", "partial_success", "membership_pending", "membership_running", "membership_partial"}
    if status in active_statuses and (pending or running):
        reason = f"目标准入补齐中：待准备 {pending}，执行中 {running}"
        return _stage(task, "membership_preparing", "准入补齐中", "warning", reason)
    return None


def _membership_phase_from_stats(task: Task) -> dict[str, Any]:
    stats = task.stats if isinstance(task.stats, dict) else {}
    summary = stats.get("membership_summary") if isinstance(stats.get("membership_summary"), dict) else {}
    return {
        "status": stats.get("membership_stage") or summary.get("status") or "",
        "pending_account_count": stats.get("membership_need_join_count") or summary.get("pending_account_count") or summary.get("need_join_account_count") or 0,
        "running_account_count": summary.get("running_account_count") or 0,
    }


def _is_waiting_context(task: Task) -> bool:
    stats = task.stats if isinstance(task.stats, dict) else {}
    text = f"{task.last_error or ''} {stats.get('context_mode') or ''}"
    return any(marker in text for marker in WAITING_CONTEXT_MARKERS)


def _is_waiting_cooldown(task: Task) -> bool:
    text = task.last_error or ""
    return any(marker in text for marker in WAITING_COOLDOWN_MARKERS)


def _has_executing_actions(actions: Sequence[Action] | None, summary: Any | None) -> bool:
    if actions is not None:
        return any(action.status in EXECUTING_STATUSES for action in actions)
    counts = _summary_counts(summary)
    return any(int(counts.get(status) or 0) for status in EXECUTING_STATUSES)


def _has_future_pending(task: Task, actions: Sequence[Action] | None, summary: Any | None) -> bool:
    if task.next_run_at and _is_future(task.next_run_at):
        return True
    if actions is not None:
        return any(action.status in PENDING_STATUSES for action in actions)
    return int(getattr(summary, "pending_count", 0) or 0) > 0


def _summary_counts(summary: Any | None) -> dict[str, Any]:
    raw = getattr(summary, "summary", None)
    if not isinstance(raw, dict):
        return {}
    counts = raw.get("counts")
    return counts if isinstance(counts, dict) else {}


def _is_future(value: datetime) -> bool:
    from app.services._common import _now

    return value > _now()


def _next_run_reason(task: Task) -> str:
    return "等待下一轮计划时间" if task.next_run_at else "已有待执行动作，等待调度"
