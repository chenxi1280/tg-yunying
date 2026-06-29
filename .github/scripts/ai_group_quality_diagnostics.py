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
    WorkerHeartbeat,
)
from app.services.account_online_projection import task_account_online_summary


WINDOW_HOURS = 24
MEMORY_DAYS = 30
TASK_LIMIT = 8
ACTION_LIMIT = 250
WORKER_FRESH_MINUTES = 5
TEXT_PREVIEW_LIMIT = 64
ONLINE_SETTLE_SECONDS = 300
ONLINE_SETTLE_POLL_SECONDS = 15
ONLINE_BLOCK_KEYS = (
    "stale_count",
    "missing_state_count",
    "blocked_count",
    "relogin_required_count",
    "offline_count",
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
    recent_actions = recent_task_actions(session, task.id, since)
    payloads = [action.payload or {} for action in recent_actions]
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status,
        "last_error": task.last_error,
        "next_run_at": task.next_run_at,
        "stats": task.stats or {},
        "topic_count": len(config.get("topic_directions") or []),
        "teacher_target_count": len(config.get("teacher_targets") or []),
        "legacy_topic_hint_present": bool(str(config.get("topic_hint") or "").strip()),
        "recent_send_count": len(recent_actions),
        "memory_payload_count": sum(1 for payload in payloads if payload.get("ai_message_memory_id")),
        "voice_profile_payload_count": sum(1 for payload in payloads if int(payload.get("account_voice_profile_version") or 0) > 0),
        "open_action_counts": open_action_counts(session, task.id),
        "quality_rejection_counts": dict((task.stats or {}).get("quality_rejection_counts") or {}),
        "online_summary": task_account_online_summary(session, task),
        "recent_action_samples": action_samples(recent_actions[:8]),
    }


def task_snapshots(session, since: datetime) -> list[dict[str, Any]]:
    return [task_snapshot(session, task, since) for task in active_group_tasks(session)]


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
            json_line("AI_GROUP_QUALITY_ONLINE_GATE_FAILED", payload)
            raise SystemExit("AI group online quality gate failed")
        time.sleep(ONLINE_SETTLE_POLL_SECONDS)
        session.expire_all()


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
            "text": preview_text((action.payload or {}).get("message_text")),
        }
        for action in actions
    ]


def recent_action_duplicate_snapshot(session, since: datetime) -> dict[str, Any]:
    actions = list(
        session.scalars(
            select(Action)
            .where(Action.task_type == "group_ai_chat", Action.action_type == "send_message", Action.scheduled_at >= since)
            .order_by(Action.scheduled_at.desc(), Action.created_at.desc())
            .limit(ACTION_LIMIT)
        )
    )
    texts = [preview_text((action.payload or {}).get("message_text")) for action in actions]
    counts = Counter(text for text in texts if text)
    repeats = [{"text": text, "count": int(count)} for text, count in counts.most_common(10) if count > 1]
    return {"action_count": len(actions), "repeated_texts": repeats, "status_counts": dict(Counter(action.status for action in actions))}


def main() -> None:
    captured_at = now_local()
    since = captured_at - timedelta(hours=WINDOW_HOURS)
    with SessionLocal() as session:
        json_line("AI_GROUP_QUALITY_WORKERS", worker_snapshot(session, captured_at))
        json_line("AI_GROUP_QUALITY_VOICE_PROFILES", voice_profile_snapshot(session))
        json_line("AI_GROUP_QUALITY_MEMORY", memory_status_snapshot(session, captured_at))
        json_line("AI_GROUP_QUALITY_RECENT_ACTIONS", recent_action_duplicate_snapshot(session, since))
        for snapshot in wait_for_online_gate(session, since):
            json_line("AI_GROUP_QUALITY_TASK", snapshot)
        json_line("AI_GROUP_QUALITY_DONE", {"captured_at": iso(captured_at), "window_hours": WINDOW_HOURS})


if __name__ == "__main__":
    main()
