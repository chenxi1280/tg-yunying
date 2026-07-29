from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task, TaskDayLedger
from app.services._common import _now, audit

from .daily_ledgers import ensure_task_day_ledger
from .channel_fulfillment_takeover import (
    ChannelTakeoverSummary,
    migrate_channel_fulfillment,
)
from .comment_fulfillment_takeover import migrate_comment_fulfillment


UNIFIED_TASK_GATE_LIMIT = 1_000_000
FULFILLMENT_CONTRACT_VERSION = "all_task_v2"
FULFILLMENT_TASK_TYPES = frozenset(
    {
        "group_ai_chat",
        "channel_comment",
        "channel_like",
        "channel_view",
        "search_click",
    }
)
TAKEOVER_TASK_TYPES = frozenset(
    {*FULFILLMENT_TASK_TYPES, "search_join_group"}
)
ACTIVE_TAKEOVER_STATUSES = frozenset({"draft", "pending", "running", "paused", "stopped"})
LEGACY_MEMBERSHIP_ACTION_TYPES = frozenset(
    {
        "search_join_membership",
        "ensure_channel_membership",
        "ensure_target_membership",
    }
)
PRE_GATEWAY_ACTION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)
RETIRED_AI_QUANTITY_GATE_FIELDS = frozenset(
    {
        "per_account_daily_min_messages",
        "per_account_daily_max_messages",
        "hard_hourly_target_enabled",
        "hourly_min_messages",
        "hard_hourly_strategy",
    }
)


@dataclass(frozen=True)
class TaskTakeoverResult:
    task_id: str
    changed: bool
    previous_type: str
    current_type: str
    retired_action_count: int
    ledger_id: str | None
    bound_action_count: int
    backfilled_fact_count: int
    duplicate_action_count: int


def takeover_task(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    write_audit: bool = True,
) -> TaskTakeoverResult:
    previous_type = task.type
    if not _eligible(task):
        return _result(task, previous_type, False, 0, None)
    was_legacy_search = task.type == "search_join_group"
    before = _task_snapshot(task)
    contract_changed = (
        (task.stats or {}).get("fulfillment_contract_version")
        != FULFILLMENT_CONTRACT_VERSION
    )
    retired = _takeover_legacy_search(session, task)
    if contract_changed or was_legacy_search:
        retired += _retire_unbound_legacy_actions(session, task)
    _normalize_task_gate_limits(task)
    _stamp_contract(task)
    if contract_changed and task.status == "running":
        task.next_run_at = now or _now()
    ledger_id, ledger_created = _ensure_running_ledger(session, task, now)
    channel_summary = _migrate_channel_actions(
        session,
        task,
        now=now,
        contract_changed=contract_changed,
    )
    changed = (
        before != _task_snapshot(task)
        or retired > 0
        or ledger_created
        or channel_summary != ChannelTakeoverSummary()
    )
    if changed and write_audit:
        _write_takeover_audit(
            session,
            task,
            previous_type,
            retired,
            ledger_id,
            channel_summary,
        )
    return _result(
        task,
        previous_type,
        changed,
        retired,
        ledger_id,
        channel_summary,
    )


def block_invalid_fulfillment_task(task: Task, exc: ValueError) -> None:
    detail = str(exc)
    task.status = "paused"
    task.next_run_at = None
    task.last_error = f"任务结构阻塞：{detail}"
    task.stats = {
        **(task.stats or {}),
        "fulfillment_takeover_status": "blocked",
        "fulfillment_takeover_blocker_code": "task_contract_invalid",
        "fulfillment_takeover_error": detail,
        "fulfillment_takeover_checked_at": _now().isoformat(),
    }


def _eligible(task: Task) -> bool:
    return (
        task.deleted_at is None
        and task.status in ACTIVE_TAKEOVER_STATUSES
        and task.type in TAKEOVER_TASK_TYPES
    )


def _task_snapshot(task: Task) -> tuple:
    return (
        task.type,
        dict(task.type_config or {}),
        dict(task.pacing_config or {}),
        str((task.stats or {}).get("fulfillment_contract_version") or ""),
    )


def _takeover_legacy_search(session: Session, task: Task) -> int:
    if task.type != "search_join_group":
        return 0
    task.type_config = _pure_click_config(task.type_config or {})
    pacing = dict(task.pacing_config or {})
    pacing["max_actions_per_day"] = UNIFIED_TASK_GATE_LIMIT
    task.pacing_config = pacing
    task.type = "search_click"
    return _retire_legacy_membership_actions(session, task)


def _pure_click_config(config: dict) -> dict:
    required = (
        "target_operation_target_id",
        "target_input",
        "target_title",
        "target_link",
        "daily_click_target_count",
        "search_bots",
        "keyword_hashes",
        "keyword_text_ciphertexts",
    )
    missing = [field for field in required if not config.get(field)]
    if missing:
        raise ValueError(f"legacy_search_click_contract_invalid:{','.join(missing)}")
    return {
        field: config[field]
        for field in required
    } | {
        "search_execution_mode": "click_only",
        "execution_mode": "mtproto_userbot",
        "max_pages": int(config.get("max_pages") or 5),
    }


def _retire_legacy_membership_actions(session: Session, task: Task) -> int:
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type.in_(LEGACY_MEMBERSHIP_ACTION_TYPES),
            Action.status.in_(PRE_GATEWAY_ACTION_STATUSES),
        )
    ))
    started_ids = _gateway_started_action_ids(session, actions)
    retired = [action for action in actions if action.id not in started_ids]
    for action in retired:
        action.status = "skipped"
        action.result = {
            **(action.result or {}),
            "error_code": "legacy_membership_retired_by_click_takeover",
            "error_message": "存量混合搜索已切换为纯搜索点击",
        }
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.lease_owner = ""
        action.lease_expires_at = None
    return len(retired)


def _retire_unbound_legacy_actions(session: Session, task: Task) -> int:
    action_type = {
        "group_ai_chat": "send_message",
        "search_click": "search_join",
    }.get(task.type)
    if action_type is None:
        return 0
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status.in_(PRE_GATEWAY_ACTION_STATUSES),
        )
    ))
    started_ids = _gateway_started_action_ids(session, actions)
    retired = [
        action
        for action in actions
        if action.id not in started_ids and _legacy_action_unbound(task, action)
    ]
    for action in retired:
        _retire_legacy_action(action)
    return len(retired)


def _gateway_started_action_ids(
    session: Session,
    actions: list[Action],
) -> set[str]:
    if not actions:
        return set()
    return set(session.scalars(
        select(ExecutionAttempt.action_id).where(
            ExecutionAttempt.action_id.in_([action.id for action in actions]),
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        )
    ))


def _legacy_action_unbound(task: Task, action: Action) -> bool:
    if task.type == "group_ai_chat":
        return not action.primary_quantity_slot_id
    payload = action.payload if isinstance(action.payload, dict) else {}
    return not str(payload.get("search_click_obligation_id") or "")


def _retire_legacy_action(action: Action) -> None:
    action.status = "skipped"
    action.result = {
        **(action.result or {}),
        "error_code": "legacy_action_retired_by_fulfillment_takeover",
        "error_message": "旧履约 Action 已在 Gateway 前终结，由新义务模型重新规划",
    }
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None


def _normalize_task_gate_limits(task: Task) -> None:
    config = dict(task.type_config or {})
    if task.type == "group_ai_chat":
        for field in RETIRED_AI_QUANTITY_GATE_FIELDS:
            config.pop(field, None)
        config["account_coverage_mode"] = "all_accounts_daily"
    if task.type == "channel_view":
        config["task_daily_view_safety_cap"] = UNIFIED_TASK_GATE_LIMIT
        config["max_views_per_account_per_day"] = UNIFIED_TASK_GATE_LIMIT
    if task.type == "channel_like":
        config["max_likes_per_account_per_hour"] = UNIFIED_TASK_GATE_LIMIT
    if task.type == "channel_comment":
        config["max_total_comments"] = UNIFIED_TASK_GATE_LIMIT
        config["max_total_comments_jitter"] = 0
        config["max_comments_per_account_per_hour"] = UNIFIED_TASK_GATE_LIMIT
        task.stats = {
            **(task.stats or {}),
            "max_total_comments_resolved": UNIFIED_TASK_GATE_LIMIT,
        }
    task.type_config = config
    task.pacing_config = normalize_fulfillment_pacing(
        task.type,
        task.pacing_config or {},
    )


def normalize_fulfillment_pacing(task_type: str, pacing: dict) -> dict:
    normalized = dict(pacing or {})
    if task_type not in FULFILLMENT_TASK_TYPES:
        return normalized
    normalized["max_actions_per_hour"] = UNIFIED_TASK_GATE_LIMIT
    if task_type == "search_click":
        normalized["max_actions_per_day"] = UNIFIED_TASK_GATE_LIMIT
    return normalized


def _stamp_contract(task: Task) -> None:
    stats = {
        key: value
        for key, value in (task.stats or {}).items()
        if not key.startswith("hard_hourly_")
    }
    task.stats = {
        **stats,
        "fulfillment_contract_version": FULFILLMENT_CONTRACT_VERSION,
    }
    task.hard_hourly_next_check_at = None


def _ensure_running_ledger(
    session: Session,
    task: Task,
    now: datetime | None,
) -> tuple[str | None, bool]:
    if task.status != "running" or task.type not in {
        "group_ai_chat",
        "channel_view",
        "search_click",
    }:
        return None, False
    existing_ids = set(session.scalars(
        select(TaskDayLedger.id).where(TaskDayLedger.task_id == task.id)
    ))
    ledger = ensure_task_day_ledger(session, task, now=now)
    return ledger.id, ledger.id not in existing_ids


def _migrate_channel_actions(
    session: Session,
    task: Task,
    *,
    now: datetime | None,
    contract_changed: bool,
) -> ChannelTakeoverSummary:
    if not contract_changed:
        return ChannelTakeoverSummary()
    timestamp = now or datetime.now().astimezone()
    if task.type in {"channel_like", "channel_view"}:
        return migrate_channel_fulfillment(session, task, now=timestamp)
    if task.type == "channel_comment":
        return migrate_comment_fulfillment(session, task, now=timestamp)
    return ChannelTakeoverSummary()


def _write_takeover_audit(
    session: Session,
    task: Task,
    previous_type: str,
    retired: int,
    ledger_id: str | None,
    channel_summary: ChannelTakeoverSummary,
) -> None:
    audit(
        session,
        tenant_id=task.tenant_id,
        actor="system:fulfillment_takeover",
        action="接管任务到新履约模型",
        target_type="task",
        target_id=task.id,
        detail=(
            f"type={previous_type}->{task.type}; "
            f"retired_actions={retired}; ledger_id={ledger_id or '-'}; "
            f"bound_actions={channel_summary.bound_action_count}; "
            f"backfilled_facts={channel_summary.backfilled_fact_count}; "
            f"duplicate_actions={channel_summary.duplicate_action_count}"
        ),
    )


def _result(
    task: Task,
    previous_type: str,
    changed: bool,
    retired: int,
    ledger_id: str | None,
    channel_summary: ChannelTakeoverSummary | None = None,
) -> TaskTakeoverResult:
    summary = channel_summary or ChannelTakeoverSummary()
    return TaskTakeoverResult(
        task_id=task.id,
        changed=changed,
        previous_type=previous_type,
        current_type=task.type,
        retired_action_count=retired,
        ledger_id=ledger_id,
        bound_action_count=summary.bound_action_count,
        backfilled_fact_count=summary.backfilled_fact_count,
        duplicate_action_count=summary.duplicate_action_count,
    )


__all__ = [
    "FULFILLMENT_CONTRACT_VERSION",
    "FULFILLMENT_TASK_TYPES",
    "TaskTakeoverResult",
    "UNIFIED_TASK_GATE_LIMIT",
    "block_invalid_fulfillment_task",
    "normalize_fulfillment_pacing",
    "takeover_task",
]
