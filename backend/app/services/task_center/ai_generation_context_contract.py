from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob, GroupContextMessage, Task
from app.services._common import _now


def freeze_generation_context_contract(
    session: Session,
    task: Task,
    action: Action,
    *,
    job: GenerationJob,
    evidence: dict[str, str],
    evidence_lines: tuple[str, ...],
    route: str,
    prompt_version: str,
    gate_config: dict,
) -> dict:
    payload = dict(action.payload or {})
    anchor_ids = _anchor_ids(payload)
    rows = _anchor_rows(session, task, anchor_ids)
    captured_at = _now()
    context_mode = (
        "silence"
        if all(line.startswith("群话题：") for line in evidence_lines)
        else "history"
    )
    contract = {
        "context_revision": int(job.context_snapshot_version or 0),
        "context_hash": str(job.context_snapshot_hash or ""),
        "captured_at": captured_at.isoformat(),
        "context_age_ms": _context_age_ms(captured_at, rows),
        "anchor_message_ids": anchor_ids,
        "anchor_author_ids": list(dict.fromkeys(row.sender_peer_id for row in rows if row.sender_peer_id)),
        "context_mode": context_mode,
        "allowed_facts": dict(evidence),
        "forbidden_claims": list(gate_config.get("forbidden_claim_categories") or ()),
        "task_topic_revision": int(payload.get("content_intent_config_revision") or task.config_revision or 1),
        "content_route": route,
        "route_reason": _route_reason(route, context_mode=context_mode),
        "voice_contract_version": str(payload.get("voice_profile_contract_version") or ""),
        "prompt_version": prompt_version,
    }
    job.evaluator_evidence = {
        **dict(job.evaluator_evidence or {}),
        "generation_contract": contract,
    }
    return contract


def _anchor_ids(payload: dict) -> list[int]:
    raw = payload.get("anchor_message_ids") or payload.get("context_message_ids") or []
    return list(dict.fromkeys(int(value) for value in raw if int(value or 0) > 0))


def _anchor_rows(
    session: Session,
    task: Task,
    anchor_ids: list[int],
) -> list[GroupContextMessage]:
    if not anchor_ids:
        return []
    rows = session.scalars(select(GroupContextMessage).where(
        GroupContextMessage.tenant_id == task.tenant_id,
        GroupContextMessage.id.in_(anchor_ids),
    )).all()
    by_id = {row.id: row for row in rows}
    return [by_id[row_id] for row_id in anchor_ids if row_id in by_id]


def _context_age_ms(captured_at: datetime, rows: list[GroupContextMessage]) -> int:
    timestamps = [row.sent_at or row.created_at for row in rows]
    if not timestamps:
        return 0
    latest = max(value.replace(tzinfo=None) for value in timestamps)
    captured = captured_at.replace(tzinfo=None)
    return max(0, int((captured - latest).total_seconds() * 1000))


def _route_reason(route: str, *, context_mode: str) -> str:
    if context_mode == "silence":
        return "silent_context_allowed_general_topic"
    return f"current_context_evidence:{route}"


__all__ = ["freeze_generation_context_contract"]
