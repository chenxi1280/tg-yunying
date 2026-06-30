from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AccountStatus,
    Action,
    AiAccountVoiceProfile,
    AiGroupMessageMemory,
    Task,
    TgAccount,
    TgAccountOnlineState,
    WorkerHeartbeat,
)
from app.services.account_online_projection import task_account_online_summary
from app.services.task_center.hard_hourly import enabled as hard_hourly_enabled, hard_hourly_stats
from app.timezone import as_beijing


WINDOW_HOURS = 24
MEMORY_DAYS = 30
TASK_LIMIT = 8
ACTION_LIMIT = 250
WORKER_FRESH_MINUTES = 5
TEXT_PREVIEW_LIMIT = 64
ONLINE_FAILURE_SAMPLE_LIMIT = 10
ONLINE_SETTLE_SECONDS = 900
ONLINE_SETTLE_POLL_SECONDS = 15
QUALITY_PAYLOAD_BLOCKER_LIMIT = 20
MATERIAL_TRACE_SAMPLE_LIMIT = 8
ACTIVE_TASK_STATUSES = {"running"}
ONLINE_BLOCK_KEYS = (
    "stale_count",
    "missing_state_count",
    "blocked_count",
    "relogin_required_count",
    "offline_count",
)
EFFECTIVE_DUPLICATE_STATUSES = ("success", "unknown_after_send", "pending", "claiming", "executing")
OPEN_DUPLICATE_STATUSES = ("pending", "claiming", "executing")
SENT_DUPLICATE_STATUSES = ("success", "unknown_after_send")
QUALITY_PAYLOAD_REQUIRED_FIELDS = (
    "account_voice_profile_version",
    "ai_message_memory_id",
    "human_quality_decision",
    "generation_source",
    "act_type",
)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def json_line(label: str, payload: dict[str, Any] | list[Any]) -> None:
    print(f"{label}={json.dumps(jsonable(payload), ensure_ascii=False, sort_keys=True)}", flush=True)


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    return value


def preview_text(value: object) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:TEXT_PREVIEW_LIMIT]


def normalized_text(value: object) -> str:
    return " ".join(str(value or "").split())


def now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def worker_snapshot(session, now: datetime) -> dict[str, Any]:
    fresh_after = now - timedelta(minutes=WORKER_FRESH_MINUTES)
    rows = session.scalars(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.last_seen_at >= fresh_after)
        .order_by(WorkerHeartbeat.process_type.asc(), WorkerHeartbeat.last_seen_at.desc())
    ).all()
    counts = Counter(row.process_type for row in rows)
    return {
        "fresh_after": iso(fresh_after),
        "counts": {str(key): int(value) for key, value in counts.items()},
        "workers": [
            {
                "worker_id": row.worker_id,
                "process_type": row.process_type,
                "last_seen_at": iso(row.last_seen_at),
                "metadata": row.heartbeat_metadata or {},
            }
            for row in rows
        ],
    }


def voice_profile_snapshot(session) -> dict[str, Any]:
    active_accounts = int(
        session.scalar(
            select(func.count(TgAccount.id)).where(
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.deleted_at.is_(None),
            )
        )
        or 0
    )
    profile_rows = session.scalars(select(AiAccountVoiceProfile).where(AiAccountVoiceProfile.status == "active")).all()
    quality_counts = Counter(row.quality_status or "unknown" for row in profile_rows)
    source_counts = Counter(row.source or "unknown" for row in profile_rows)
    similarity_scores = [int(row.similarity_score or 0) for row in profile_rows if row.similarity_score is not None]
    return {
        "active_account_count": active_accounts,
        "active_profile_count": len(profile_rows),
        "missing_active_profile_count": max(active_accounts - len({row.account_id for row in profile_rows}), 0),
        "quality_counts": dict(quality_counts),
        "source_counts": dict(source_counts),
        "max_similarity_score": max(similarity_scores) if similarity_scores else None,
        "sample_profiles": [
            {
                "account_id": row.account_id,
                "version": row.version,
                "age_band": row.age_band,
                "quality_status": row.quality_status,
                "summary": preview_text(row.short_prompt_summary),
            }
            for row in profile_rows[:10]
        ],
    }


def memory_status_snapshot(session, now: datetime) -> dict[str, Any]:
    since_24h = now - timedelta(hours=WINDOW_HOURS)
    since_30d = now - timedelta(days=MEMORY_DAYS)
    status_counts = dict(
        session.execute(
            select(AiGroupMessageMemory.status, func.count(AiGroupMessageMemory.id))
            .where(AiGroupMessageMemory.planned_at >= since_30d)
            .group_by(AiGroupMessageMemory.status)
        ).all()
    )
    duplicate_counts = dict(
        session.execute(
            select(AiGroupMessageMemory.duplicate_window, func.count(AiGroupMessageMemory.id))
            .where(AiGroupMessageMemory.planned_at >= since_30d, AiGroupMessageMemory.duplicate_window != "")
            .group_by(AiGroupMessageMemory.duplicate_window)
        ).all()
    )
    recent_count = int(
        session.scalar(select(func.count(AiGroupMessageMemory.id)).where(AiGroupMessageMemory.planned_at >= since_24h)) or 0
    )
    return {
        "window_hours": WINDOW_HOURS,
        "retention_days": MEMORY_DAYS,
        "recent_memory_count": recent_count,
        "status_counts_30d": {str(key): int(value) for key, value in status_counts.items()},
        "duplicate_window_counts_30d": {str(key): int(value) for key, value in duplicate_counts.items()},
        "risk_clusters": memory_risk_clusters(session, since_30d),
    }


def memory_risk_clusters(session, since: datetime) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            AiGroupMessageMemory.group_id,
            AiGroupMessageMemory.text_fingerprint,
            func.count(AiGroupMessageMemory.id),
            func.max(AiGroupMessageMemory.raw_text),
        )
        .where(AiGroupMessageMemory.planned_at >= since, AiGroupMessageMemory.status.in_(("success", "reserved", "unknown_after_send")))
        .group_by(AiGroupMessageMemory.group_id, AiGroupMessageMemory.text_fingerprint)
        .having(func.count(AiGroupMessageMemory.id) > 1)
        .order_by(func.count(AiGroupMessageMemory.id).desc())
        .limit(10)
    ).all()
    return [
        {"group_id": group_id, "fingerprint": fingerprint, "count": int(count), "sample": preview_text(raw_text)}
        for group_id, fingerprint, count, raw_text in rows
    ]


def active_group_tasks(session) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.type == "group_ai_chat", Task.deleted_at.is_(None))
            .order_by(Task.updated_at.desc(), Task.created_at.desc())
            .limit(TASK_LIMIT)
        )
    )


def task_snapshot(session, task: Task, since: datetime) -> dict[str, Any]:
    config = task.type_config or {}
    stats = diagnostic_task_stats(session, task)
    recent_actions = recent_task_actions(session, task.id, since)
    payloads = [action.payload or {} for action in recent_actions]
    material_traces = material_trace_samples(recent_actions)
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status,
        "last_error": task.last_error,
        "next_run_at": task.next_run_at,
        "stats": stats,
        "topic_count": len(config.get("topic_directions") or []),
        "teacher_target_count": len(config.get("teacher_targets") or []),
        "legacy_topic_hint_present": bool(str(config.get("topic_hint") or "").strip()),
        "recent_send_count": len(recent_actions),
        "memory_payload_count": sum(1 for payload in payloads if payload.get("ai_message_memory_id")),
        "voice_profile_payload_count": sum(1 for payload in payloads if int(payload.get("account_voice_profile_version") or 0) > 0),
        "material_trace_count": len(material_traces),
        "material_trace_samples": material_traces[:MATERIAL_TRACE_SAMPLE_LIMIT],
        "open_action_counts": open_action_counts(session, task.id),
        "quality_rejection_counts": dict(stats.get("quality_rejection_counts") or {}),
        "online_summary": task_account_online_summary(session, task),
        "recent_action_samples": action_samples(recent_actions[:8]),
    }


def diagnostic_task_stats(session, task: Task) -> dict[str, Any]:
    stats = dict(task.stats or {})
    if task.type == "group_ai_chat" and hard_hourly_enabled(task):
        return hard_hourly_stats(session, task, now_local(), stats)
    return stats


def task_snapshots(session, since: datetime) -> list[dict[str, Any]]:
    return [task_snapshot(session, task, since) for task in active_group_tasks(session)]


def hard_hourly_gate_blockers(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for snapshot in snapshots:
        stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), dict) else {}
        if str(snapshot.get("status") or "") not in ACTIVE_TASK_STATUSES:
            continue
        if not stats.get("hard_hourly_target_enabled"):
            continue
        goal = _safe_int(stats.get("hard_hourly_goal"))
        success = _safe_int(stats.get("hard_hourly_success_count"))
        status = str(stats.get("hard_hourly_status") or "")
        if goal <= 0 or (success >= goal and status == "met"):
            continue
        blockers.append(_hard_hourly_blocker(snapshot, stats, goal, success, status))
    return blockers[:TASK_LIMIT]


def _hard_hourly_blocker(snapshot: dict[str, Any], stats: dict[str, Any], goal: int, success: int, status: str) -> dict[str, Any]:
    return {
        "task_id": str(snapshot.get("task_id") or ""),
        "name": str(snapshot.get("name") or ""),
        "status": str(snapshot.get("status") or ""),
        "bucket": str(stats.get("hard_hourly_bucket") or ""),
        "goal": goal,
        "success_count": success,
        "future_open_count": _safe_int(stats.get("hard_hourly_open_count")),
        "overdue_open_count": _safe_int(stats.get("hard_hourly_overdue_open_count")),
        "deficit": _safe_int(stats.get("hard_hourly_deficit")),
        "hard_hourly_status": status,
        "blockers": dict(stats.get("hard_hourly_last_blockers") or {}),
        "reason": "hard_hourly_not_met",
    }


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def online_gate_blockers(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for snapshot in snapshots:
        summary = snapshot.get("online_summary") or {}
        desired_count = int(summary.get("desired_count") or 0)
        if desired_count <= 0:
            continue
        online_count = int(summary.get("online_count") or 0)
        counts = {key: int(summary.get(key) or 0) for key in ONLINE_BLOCK_KEYS}
        non_online_count = max(desired_count - online_count, 0)
        if non_online_count <= 0 and not any(counts.values()):
            continue
        blockers.append(
            {
                "task_id": str(snapshot.get("task_id") or ""),
                "name": str(snapshot.get("name") or ""),
                "status": str(snapshot.get("status") or ""),
                "desired_count": desired_count,
                "online_count": online_count,
                "non_online_count": non_online_count,
                "samples": list(summary.get("samples") or [])[:ONLINE_FAILURE_SAMPLE_LIMIT],
                **counts,
            }
        )
    return blockers


def wait_for_online_gate(session, since: datetime) -> list[dict[str, Any]]:
    deadline = now_local() + timedelta(seconds=ONLINE_SETTLE_SECONDS)
    while True:
        snapshots = task_snapshots(session, since)
        blockers = online_gate_blockers(snapshots)
        if not blockers:
            return snapshots
        payload = {
            "remaining_seconds": max(int((deadline - now_local()).total_seconds()), 0),
            "blocker_count": len(blockers),
            "blockers": blockers[:TASK_LIMIT],
        }
        json_line("AI_GROUP_QUALITY_ONLINE_WAIT", payload)
        if now_local() >= deadline:
            json_line("AI_GROUP_QUALITY_ONLINE_FAILURE_DETAILS", online_failure_details(session, blockers, now_local()))
            json_line("AI_GROUP_QUALITY_ONLINE_GATE_FAILED", payload)
            raise SystemExit("AI group online quality gate failed")
        time.sleep(ONLINE_SETTLE_POLL_SECONDS)
        session.expire_all()


def online_failure_details(session, blockers: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    task_ids = [str(item.get("task_id") or "") for item in blockers if item.get("task_id")]
    if not task_ids:
        return []
    tasks = list(session.scalars(select(Task).where(Task.id.in_(task_ids))).all())
    return [task_online_failure_detail(session, task, now) for task in tasks]


def task_online_failure_detail(session, task: Task, now: datetime) -> dict[str, Any]:
    rows = _task_desired_online_rows(session, task)
    failures = [_online_failure_row(state, account, now) for state, account in rows if _online_failure_bucket(state, now)]
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status,
        "failure_count": len(failures),
        "bucket_counts": dict(Counter(row["bucket"] for row in failures)),
        "failure_type_counts": dict(Counter(row["failure_type"] or "none" for row in failures)),
        "account_status_counts": dict(Counter(row["account_status"] or "unknown" for row in failures)),
        "sample_rows": failures[:ONLINE_FAILURE_SAMPLE_LIMIT],
    }


def _task_desired_online_rows(session, task: Task) -> list[tuple[TgAccountOnlineState, TgAccount]]:
    rows = session.execute(
        select(TgAccountOnlineState, TgAccount)
        .join(TgAccount, TgAccount.id == TgAccountOnlineState.account_id)
        .where(TgAccountOnlineState.tenant_id == task.tenant_id, TgAccountOnlineState.desired_online.is_(True))
    ).all()
    return [(state, account) for state, account in rows if _has_task_source(state, task.id)]


def _has_task_source(state: TgAccountOnlineState, task_id: str) -> bool:
    sources = state.desired_sources if isinstance(state.desired_sources, list) else []
    for source in sources:
        if isinstance(source, dict) and _source_matches_task(source, task_id):
            return True
    return False


def _source_matches_task(source: dict[str, Any], task_id: str) -> bool:
    source_id = str(source.get("source_id") or "")
    return source.get("source_type") == "task" and (source_id == task_id or source_id.startswith(f"{task_id}:"))


def _online_failure_row(state: TgAccountOnlineState, account: TgAccount, now: datetime) -> dict[str, Any]:
    return {
        "account_id": state.account_id,
        "display_name": account.display_name,
        "account_status": account.status,
        "health_score": account.health_score,
        "bucket": _online_failure_bucket(state, now),
        "online_status": state.online_status,
        "failure_type": state.failure_type,
        "failure_detail": preview_text(state.failure_detail),
        "last_probe_at": state.last_probe_at,
        "next_probe_at": state.next_probe_at,
        "stale_after_at": state.stale_after_at,
    }


def _online_failure_bucket(state: TgAccountOnlineState, now: datetime) -> str:
    current_time = as_beijing(now) or now
    stale_after = as_beijing(state.stale_after_at)
    if stale_after and stale_after <= current_time:
        return "stale"
    if state.online_status == "online":
        return ""
    if state.failure_type in {"session_invalid", "login_required", "relogin_required"}:
        return "relogin_required"
    if state.online_status in {"blocked", "proxy_failed", "restricted"}:
        return "blocked"
    return state.online_status or "offline"


def recent_task_actions(session, task_id: str, since: datetime) -> list[Action]:
    return list(
        session.scalars(
            select(Action)
            .where(
                Action.task_id == task_id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.scheduled_at >= since,
            )
            .order_by(Action.scheduled_at.desc(), Action.created_at.desc())
            .limit(ACTION_LIMIT)
        )
    )


def open_action_counts(session, task_id: str) -> dict[str, int]:
    rows = session.execute(
        select(Action.status, func.count(Action.id)).where(
            Action.task_id == task_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(("pending", "claiming", "executing", "retryable_failed")),
        ).group_by(Action.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def action_samples(actions: list[Action]) -> list[dict[str, Any]]:
    return [
        {
            "id": action.id,
            "status": action.status,
            "account_id": action.account_id,
            "scheduled_at": iso(action.scheduled_at),
            "executed_at": iso(action.executed_at),
            "memory_id": str((action.payload or {}).get("ai_message_memory_id") or "")[:36],
            "profile_version": int((action.payload or {}).get("account_voice_profile_version") or 0),
            "quality_decision": str((action.payload or {}).get("human_quality_decision") or ""),
            "generation_source": str((action.payload or {}).get("generation_source") or ""),
            "material_intent": _payload_material_intent(action.payload or {}),
            "material_matched_tags": _payload_material_tags(action.payload or {}),
            "material_candidate_count": _payload_material_candidate_count(action.payload or {}),
            "material_id": _payload_material_id(action.payload or {}),
            "material_failure_reason": _payload_material_failure_reason(action.payload or {}),
            "text": preview_text((action.payload or {}).get("message_text")),
        }
        for action in actions
    ]


def material_trace_samples(actions: list[Action]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for action in actions:
        payload = action.payload or {}
        intent = _payload_material_intent(payload)
        tags = _payload_material_tags(payload)
        if not intent and not tags:
            continue
        samples.append(
            {
                "action_id": action.id,
                "status": action.status,
                "account_id": action.account_id,
                "material_intent": intent,
                "material_matched_tags": tags,
                "material_candidate_count": _payload_material_candidate_count(payload),
                "material_ok": _payload_rule_trace(payload).get("material_ok"),
                "material_id": _payload_material_id(payload),
                "material_failure_reason": _payload_material_failure_reason(payload),
                "text": preview_text(payload.get("message_text")),
            }
        )
    return samples


def _payload_rule_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("rule_trace") if isinstance(payload, dict) else {}
    return trace if isinstance(trace, dict) else {}


def _payload_material_intent(payload: dict[str, Any]) -> str:
    return str(_payload_rule_trace(payload).get("material_intent") or "").strip()


def _payload_material_tags(payload: dict[str, Any]) -> list[str]:
    tags = _payload_rule_trace(payload).get("material_matched_tags") or []
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags if str(tag).strip()]


def _payload_material_candidate_count(payload: dict[str, Any]) -> int:
    try:
        return int(_payload_rule_trace(payload).get("material_candidate_count") or 0)
    except (TypeError, ValueError):
        return 0


def _payload_material_id(payload: dict[str, Any]) -> int | None:
    value = _payload_rule_trace(payload).get("material_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _payload_material_failure_reason(payload: dict[str, Any]) -> str:
    return str(_payload_rule_trace(payload).get("material_failure_reason") or "").strip()


def recent_action_duplicate_snapshot(session, since: datetime) -> dict[str, Any]:
    actions = list(
        session.scalars(
            select(Action)
            .where(Action.task_type == "group_ai_chat", Action.action_type == "send_message", Action.scheduled_at >= since)
            .order_by(Action.scheduled_at.desc(), Action.created_at.desc())
            .limit(ACTION_LIMIT)
        )
    )
    return recent_action_duplicate_summary(actions)


def recent_action_duplicate_summary(actions: list[Action]) -> dict[str, Any]:
    grouped = _group_actions_by_text(actions)
    repeated_texts = [
        {"text": preview_text(text), "count": len(items)}
        for text, items in sorted(grouped.items(), key=lambda row: len(row[1]), reverse=True)
        if len(items) > 1
    ][:10]
    return {
        "action_count": len(actions),
        "repeated_texts": repeated_texts,
        "duplicate_blockers": _duplicate_blockers(grouped),
        "sent_duplicate_observations": _sent_duplicate_observations(grouped),
        "quality_payload_blockers": _quality_payload_blockers(actions),
        "status_counts": dict(Counter(action.status for action in actions)),
    }


def _group_actions_by_text(actions: list[Action]) -> dict[str, list[Action]]:
    grouped: dict[str, list[Action]] = {}
    for action in actions:
        text = normalized_text((action.payload or {}).get("message_text"))
        if not text:
            continue
        grouped.setdefault(text, []).append(action)
    return grouped


def _duplicate_blockers(grouped: dict[str, list[Action]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for text, actions in grouped.items():
        effective = [action for action in actions if action.status in EFFECTIVE_DUPLICATE_STATUSES]
        if len(effective) <= 1 or not any(action.status in OPEN_DUPLICATE_STATUSES for action in effective):
            continue
        status_counts = dict(Counter(action.status for action in effective))
        blockers.append(
            {
                "text": preview_text(text),
                "effective_count": len(effective),
                "status_counts": dict(sorted(status_counts.items())),
                "action_ids": [str(action.id) for action in effective[:10]],
            }
        )
    return sorted(blockers, key=lambda item: int(item["effective_count"]), reverse=True)[:10]


def _sent_duplicate_observations(grouped: dict[str, list[Action]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for text, actions in grouped.items():
        sent = [action for action in actions if action.status in SENT_DUPLICATE_STATUSES]
        if len(sent) <= 1:
            continue
        status_counts = dict(Counter(action.status for action in sent))
        observations.append(
            {
                "text": preview_text(text),
                "sent_count": len(sent),
                "status_counts": dict(sorted(status_counts.items())),
                "action_ids": [str(action.id) for action in sent[:10]],
            }
        )
    return sorted(observations, key=lambda item: int(item["sent_count"]), reverse=True)[:10]


def _quality_payload_blockers(actions: list[Action]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for action in actions:
        if action.status not in EFFECTIVE_DUPLICATE_STATUSES:
            continue
        missing_fields = _missing_quality_payload_fields(action.payload or {})
        if not missing_fields:
            continue
        blockers.append(
            {
                "action_id": str(action.id),
                "account_id": getattr(action, "account_id", None),
                "status": str(action.status),
                "missing_fields": missing_fields,
                "text": preview_text((action.payload or {}).get("message_text")),
            }
        )
    return blockers[:QUALITY_PAYLOAD_BLOCKER_LIMIT]


def _missing_quality_payload_fields(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in QUALITY_PAYLOAD_REQUIRED_FIELDS:
        if not _quality_payload_field_present(payload, field):
            missing.append(field)
    return missing


def _quality_payload_field_present(payload: dict[str, Any], field: str) -> bool:
    if field == "account_voice_profile_version":
        return int(payload.get(field) or 0) > 0
    return bool(str(payload.get(field) or "").strip())


def main() -> None:
    captured_at = now_local()
    since = captured_at - timedelta(hours=WINDOW_HOURS)
    with SessionLocal() as session:
        json_line("AI_GROUP_QUALITY_WORKERS", worker_snapshot(session, captured_at))
        json_line("AI_GROUP_QUALITY_VOICE_PROFILES", voice_profile_snapshot(session))
        json_line("AI_GROUP_QUALITY_MEMORY", memory_status_snapshot(session, captured_at))
        recent_duplicates = recent_action_duplicate_snapshot(session, since)
        json_line("AI_GROUP_QUALITY_RECENT_ACTIONS", recent_duplicates)
        if recent_duplicates["duplicate_blockers"]:
            json_line("AI_GROUP_QUALITY_RECENT_DUPLICATE_GATE_FAILED", recent_duplicates)
            raise SystemExit("AI group recent duplicate quality gate failed")
        if recent_duplicates["quality_payload_blockers"]:
            json_line("AI_GROUP_QUALITY_PAYLOAD_GATE_FAILED", recent_duplicates)
            raise SystemExit("AI group quality payload gate failed")
        snapshots = wait_for_online_gate(session, since)
        for snapshot in snapshots:
            json_line("AI_GROUP_QUALITY_TASK", snapshot)
        hard_hourly_blockers = hard_hourly_gate_blockers(snapshots)
        if hard_hourly_blockers:
            payload = {"blocker_count": len(hard_hourly_blockers), "blockers": hard_hourly_blockers}
            json_line("AI_GROUP_QUALITY_HARD_HOURLY_GATE_FAILED", payload)
            raise SystemExit("AI group hard hourly quality gate failed")
        json_line("AI_GROUP_QUALITY_DONE", {"captured_at": iso(captured_at), "window_hours": WINDOW_HOURS})


if __name__ == "__main__":
    main()
