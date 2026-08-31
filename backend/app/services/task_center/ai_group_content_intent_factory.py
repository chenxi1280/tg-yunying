from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.models import (
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    Task,
)

from .ai_group_vocabulary_sampling import VOCABULARY_CATALOG_VERSION
from .ai_pacing import AiPacingAssignment


def new_intent_record(
    task: Task,
    plan: AiGroupContentAllocationPlan,
    *,
    assignment: AiPacingAssignment,
    item: dict,
    values: tuple[int, dict, str, Any, Any, list[str], list[str]],
    config_revision: int,
    config_snapshot_hash: str,
    target_reference_revision: int,
) -> AiGroupContentIntent:
    ordinal, slot, relation_kind, decision, sampled, surface_terms, normalized = values
    return AiGroupContentIntent(
        tenant_id=task.tenant_id,
        task_id=task.id,
        allocation_plan_id=plan.id,
        primary_quantity_slot_id=assignment.owner.id,
        normal_text_ordinal=ordinal,
        config_revision=config_revision,
        config_snapshot_hash=config_snapshot_hash,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        target_reference_revision=target_reference_revision,
        relation_kind=relation_kind,
        act_type=str(slot.get("act_type") or item.get("act_type") or ""),
        stance=str(slot.get("stance") or ""),
        topic_budget_eligible=decision.is_eligible_by_ordinal,
        topic_mode=decision.topic_mode,
        topic_direction_snapshot=dict(decision.topic_direction or {}),
        teacher_target_snapshot=dict(
            slot.get("teacher_target") or item.get("teacher_target") or {}
        ),
        topic_capacity_reservation_id=(
            str(uuid4()) if decision.topic_mode == "configured_topic" else ""
        ),
        daily_vocabulary_theme_id=plan.daily_vocabulary_theme_id,
        daily_vocabulary_theme_effective_state=sampled.effective_state,
        vocabulary_catalog_version=VOCABULARY_CATALOG_VERSION,
        vocabulary_sample_ids=list(sampled.sample_ids),
        vocabulary_surface_terms=surface_terms,
        vocabulary_normalized_term_ids=normalized,
        vocabulary_candidate_count=sampled.candidate_count,
        vocabulary_reservation_id=str(uuid4()) if sampled.sample_ids else "",
    )


__all__ = ["new_intent_record"]
