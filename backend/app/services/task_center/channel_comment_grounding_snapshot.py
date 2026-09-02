from __future__ import annotations

import hashlib
from datetime import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentGroundingSnapshot,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    Task,
)

from .channel_comment_grounding_extractor import (
    EXTRACTOR_VERSION,
    GROUNDING_CONTRACT_VERSION,
    GROUNDING_POLICY_VERSION,
    SEMANTIC_CAPACITY_POLICY_VERSION,
    extract_grounding_facts,
)


@dataclass(frozen=True)
class GroundingSnapshotDraft:
    content_route: str
    content_route_revision: int
    facts: dict


def build_initial_grounding_draft(
    task: Task,
    source: ChannelMessageSourceRevision,
) -> GroundingSnapshotDraft:
    route = _canonical_route(task)
    facts = extract_grounding_facts(
        source.source_text_snapshot,
        source.source_published_at,
        content_route=route,
        timezone_name=str((task.type_config or {}).get("timezone") or "Asia/Shanghai"),
    )
    return GroundingSnapshotDraft(route, int(task.config_revision or 0), facts)


def freeze_initial_grounding_snapshot(
    session: Session,
    task: Task,
    *,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    draft: GroundingSnapshotDraft,
) -> ChannelCommentGroundingSnapshot:
    return _freeze_snapshot(
        session, task, plan=plan, source=source,
        draft=draft, revision=1, supersedes_id=None,
    )


def append_grounding_snapshot(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
) -> ChannelCommentGroundingSnapshot:
    previous = latest_grounding_snapshot(session, plan.id)
    if previous is None:
        raise ValueError("channel_comment_grounding_snapshot_missing")
    existing = session.scalar(select(ChannelCommentGroundingSnapshot).where(
        ChannelCommentGroundingSnapshot.comment_plan_contract_id == plan.id,
        ChannelCommentGroundingSnapshot.source_revision_id == source.id,
        ChannelCommentGroundingSnapshot.grounding_policy_version
        == previous.grounding_policy_version,
    ))
    if existing is not None:
        return existing
    task = session.get(Task, plan.task_id)
    if task is None:
        raise ValueError("channel_comment_grounding_task_missing")
    facts = extract_grounding_facts(
        source.source_text_snapshot,
        source.source_published_at,
        content_route=previous.content_route,
        timezone_name=str((task.type_config or {}).get("timezone") or "Asia/Shanghai"),
    )
    draft = GroundingSnapshotDraft(
        previous.content_route,
        previous.content_route_revision,
        facts,
    )
    return _freeze_snapshot(
        session, task, plan=plan, source=source,
        draft=draft,
        revision=int(previous.comment_grounding_revision) + 1,
        supersedes_id=previous.id,
    )


def latest_grounding_snapshot(
    session: Session,
    plan_contract_id: str,
) -> ChannelCommentGroundingSnapshot | None:
    return session.scalar(select(ChannelCommentGroundingSnapshot).where(
        ChannelCommentGroundingSnapshot.comment_plan_contract_id == plan_contract_id,
    ).order_by(ChannelCommentGroundingSnapshot.comment_grounding_revision.desc()))


def assignment_eligible_variants(
    facts: dict,
    *,
    latest_safe_send_at: datetime,
) -> list[dict]:
    evidence = {
        str(row["evidence_id"]): row
        for row in facts.get("aspect_evidence_json", [])
    }
    return [
        dict(variant)
        for variant in facts.get("semantic_variant_units_json", [])
        if _evidence_valid_through(
            evidence.get(str(variant.get("primary_evidence_id") or "")),
            latest_safe_send_at,
        )
    ]


def _evidence_valid_through(evidence: dict | None, deadline: datetime) -> bool:
    if evidence is None:
        return False
    valid_until = evidence.get("valid_until")
    if not valid_until:
        return True
    expiry = datetime.fromisoformat(str(valid_until))
    if expiry.tzinfo is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=expiry.tzinfo)
    if expiry.tzinfo is None and deadline.tzinfo is not None:
        expiry = expiry.replace(tzinfo=deadline.tzinfo)
    return expiry >= deadline


def _freeze_snapshot(
    session: Session,
    task: Task,
    *,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    draft: GroundingSnapshotDraft,
    revision: int,
    supersedes_id: str | None,
) -> ChannelCommentGroundingSnapshot:
    message = session.get(ChannelMessage, plan.channel_message_id)
    if message is None or source.channel_message_id != message.id:
        raise ValueError("channel_comment_grounding_source_scope_invalid")
    _validate_source_fact(source, message)
    facts = draft.facts
    snapshot = ChannelCommentGroundingSnapshot(
        comment_plan_contract_id=plan.id,
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        source_remote_message_id=source.source_remote_message_id,
        source_revision_id=source.id,
        comment_grounding_revision=revision,
        supersedes_snapshot_id=supersedes_id,
        grounding_contract_version=GROUNDING_CONTRACT_VERSION,
        grounding_policy_version=GROUNDING_POLICY_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        content_route=draft.content_route,
        content_route_revision=draft.content_route_revision,
        source_content_hash=source.source_content_hash,
        source_state=str(facts["source_state"]),
        teacher_state=str(facts["teacher_state"]),
        teacher_candidates_json=list(facts["teacher_candidates_json"]),
        aspect_evidence_json=list(facts["aspect_evidence_json"]),
        evidence_blocks_json=list(facts["evidence_blocks_json"]),
        semantic_capacity_policy_version=SEMANTIC_CAPACITY_POLICY_VERSION,
        semantic_variant_units_json=list(facts["semantic_variant_units_json"]),
        groundable_capacity_count=int(facts["groundable_capacity_count"]),
        extraction_audit_json=dict(facts["extraction_audit_json"]),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _validate_source_fact(
    source: ChannelMessageSourceRevision,
    message: ChannelMessage,
) -> None:
    if source.channel_target_id != message.channel_target_id:
        raise ValueError("channel_comment_grounding_source_target_mismatch")
    if (
        not source.source_published_at
        or not source.source_published_at_fact_id
        or not source.source_content_hash
    ):
        raise ValueError("source_revision_unproven")
    actual_hash = hashlib.sha256(source.source_text_snapshot.encode("utf-8")).hexdigest()
    if actual_hash != source.source_content_hash:
        raise ValueError("source_revision_content_hash_mismatch")
    if source.truncation_state == "transport_truncated":
        return
    if source.truncation_state != "complete":
        raise ValueError("source_revision_truncation_state_invalid")


def _canonical_route(task: Task) -> str:
    config = dict(task.type_config or {})
    allowed = tuple(str(item) for item in config.get("ai_content_allowed_routes") or ())
    explicit = str(config.get("ai_content_context_route") or "").strip()
    if explicit:
        if explicit not in allowed:
            raise ValueError("content_route_not_allowed")
        return explicit
    if len(allowed) == 1:
        return allowed[0]
    raise ValueError("content_route_unresolved")


__all__ = [
    "GroundingSnapshotDraft",
    "append_grounding_snapshot",
    "assignment_eligible_variants",
    "build_initial_grounding_draft",
    "freeze_initial_grounding_snapshot",
    "latest_grounding_snapshot",
]
