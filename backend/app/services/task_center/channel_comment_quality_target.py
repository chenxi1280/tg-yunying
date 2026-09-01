from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentPlanContract,
    ChannelCommentQualityTargetRevision,
    ChannelMessageSourceRevision,
)

from .ai_generator import _extract_channel_post_aspects


GROUNDING_TARGET_BPS = 8500
SEMANTIC_CAPACITY_POLICY_VERSION = "channel_comment_semantic_capacity_v1"
SPEECH_ACTS = (
    "reaction", "specific_question", "cautious_verification", "concise_agreement",
)


def build_quality_target_component(
    source: ChannelMessageSourceRevision,
    owned_ordinals: list[int],
    *,
    comment_grounding_revision: int,
) -> dict:
    owned = sorted({int(value) for value in owned_ordinals})
    variants = _semantic_variants(source)
    raw_count = math.ceil(len(owned) * GROUNDING_TARGET_BPS / 10000)
    capacity_count = min(len(owned), len(variants))
    grounded_count = min(raw_count, capacity_count)
    raw_ordinals = owned[:raw_count]
    groundable_ordinals = owned[:capacity_count]
    grounding_ordinals = owned[:grounded_count]
    planned_fallback = [value for value in owned if value not in grounding_ordinals]
    specs = _assignment_specs(variants, grounding_ordinals)
    component = {
        "comment_grounding_revision": int(comment_grounding_revision),
        "source_revision_id": source.id,
        "owned_ordinal_ids": owned,
        "raw_grounding_ordinal_ids": raw_ordinals,
        "groundable_ordinal_ids": groundable_ordinals,
        "grounding_ordinal_ids": grounding_ordinals,
        "planned_fallback_ordinal_ids": planned_fallback,
        "teacher_binding_ordinal_ids": _teacher_ordinals(specs),
        "primary_aspect_by_ordinal": _aspect_by_ordinal(specs),
        "semantic_capacity_policy_version": SEMANTIC_CAPACITY_POLICY_VERSION,
        "semantic_capacity_result_hash": _hash(variants),
    }
    return _finalize_component(component)


def freeze_initial_quality_target(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    *,
    component: dict | None = None,
) -> ChannelCommentQualityTargetRevision:
    if plan.initial_quality_target_revision_id:
        return current_quality_target(session, plan)
    initial = component or build_quality_target_component(
        source,
        list(range(1, int(plan.required_distinct_account_count) + 1)),
        comment_grounding_revision=1,
    )
    target = _new_target(plan, [initial], revision=1, supersedes_id=None)
    session.add(target)
    session.flush()
    plan.initial_quality_target_revision_id = target.id
    plan.current_quality_target_revision_id = target.id
    session.flush()
    return target


def append_quality_target_revision(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    *,
    immutable_ordinals: set[int],
) -> ChannelCommentQualityTargetRevision:
    current = current_quality_target(session, plan)
    historical = [
        partitioned
        for component in current.component_targets_json
        if (partitioned := _partition_component(component, immutable_ordinals))
    ]
    all_ordinals = set(range(1, int(plan.required_distinct_account_count) + 1))
    movable = sorted(all_ordinals - immutable_ordinals)
    if movable:
        historical.append(build_quality_target_component(
            source, movable,
            comment_grounding_revision=_next_grounding_revision(current),
        ))
    target = _new_target(
        plan,
        historical,
        revision=int(current.quality_target_revision) + 1,
        supersedes_id=current.id,
    )
    session.add(target)
    session.flush()
    plan.current_quality_target_revision_id = target.id
    session.flush()
    return target


def current_quality_target(
    session: Session,
    plan: ChannelCommentPlanContract,
) -> ChannelCommentQualityTargetRevision:
    if not plan.current_quality_target_revision_id:
        raise ValueError("channel_comment_quality_target_missing")
    target = session.get(
        ChannelCommentQualityTargetRevision,
        plan.current_quality_target_revision_id,
    )
    if target is None or target.plan_contract_id != plan.id:
        raise ValueError("channel_comment_quality_target_scope_invalid")
    _validate_target(plan, target)
    return target


def target_component_for_ordinal(
    target: ChannelCommentQualityTargetRevision,
    ordinal: int,
) -> dict:
    components = [
        row for row in target.component_targets_json
        if int(ordinal) in {int(value) for value in row["owned_ordinal_ids"]}
    ]
    if len(components) != 1:
        raise ValueError("channel_comment_quality_component_owner_invalid")
    return components[0]


def quality_assignment_content(
    source: ChannelMessageSourceRevision,
    component: dict,
    ordinal: int,
) -> dict:
    grounding = [int(value) for value in component["grounding_ordinal_ids"]]
    if int(ordinal) not in grounding:
        raise ValueError("channel_comment_quality_assignment_not_required")
    variants = _semantic_variants(source)
    spec = variants[grounding.index(int(ordinal))]
    return {
        "evidence_text": source.source_text_snapshot,
        "evidence_hash": source.source_content_hash,
        "primary_aspect_code": spec["aspect_code"],
        "primary_aspect_text": spec["aspect_text"],
        "teacher_name": spec["teacher_name"],
        "speech_act": spec["speech_act"],
    }


def quality_target_projection(
    target: ChannelCommentQualityTargetRevision,
) -> dict:
    components = list(target.component_targets_json)
    owned = _flatten_ordinals(components, "owned_ordinal_ids")
    classified = (
        _flatten_ordinals(components, "grounding_ordinal_ids")
        + _flatten_ordinals(components, "planned_fallback_ordinal_ids")
    )
    states = {str(row["semantic_capacity_state"]) for row in components}
    return {
        "quality_target_current_revision": int(target.quality_target_revision),
        "quality_target_effective_revision": int(target.quality_target_revision),
        "quality_target_revision_state": target.target_state,
        "quality_target_component_count": len(components),
        "quality_target_component_set_hash": target.component_set_hash,
        "quality_target_unassigned_ordinal_count": len(set(owned) - set(classified)),
        "applicable_grounding_ordinal_count": len(owned),
        "unadjusted_grounding_target_count": _sum(components, "unadjusted_grounding_target_count"),
        "groundable_capacity_count": _sum(components, "groundable_capacity_count"),
        "grounding_required_count": int(target.aggregate_grounding_required_count),
        "planned_fallback_target_count": int(target.aggregate_planned_fallback_count),
        "teacher_required_count": _sum(components, "teacher_binding_required_count"),
        "primary_aspect_required_count": _sum(
            components, "primary_aspect_required_distinct_count",
        ),
        "semantic_capacity_state": _aggregate_capacity_state(states),
    }


def _new_target(
    plan: ChannelCommentPlanContract,
    components: list[dict],
    *,
    revision: int,
    supersedes_id: str | None,
) -> ChannelCommentQualityTargetRevision:
    canonical = sorted(
        (_finalize_component(dict(row)) for row in components),
        key=lambda row: (int(row["comment_grounding_revision"]), row["quality_component_key"]),
    )
    return ChannelCommentQualityTargetRevision(
        tenant_id=plan.tenant_id,
        plan_contract_id=plan.id,
        quality_target_revision=revision,
        supersedes_quality_target_revision_id=supersedes_id,
        component_targets_json=canonical,
        aggregate_grounding_required_count=_sum(canonical, "grounding_required_count"),
        aggregate_planned_fallback_count=_sum(canonical, "planned_fallback_count"),
        component_set_hash=_hash(canonical),
        target_state="frozen",
    )


def _partition_component(component: dict, immutable_ordinals: set[int]) -> dict | None:
    owned = [
        int(value) for value in component["owned_ordinal_ids"]
        if int(value) in immutable_ordinals
    ]
    if not owned:
        return None
    result = dict(component)
    for key in (
        "owned_ordinal_ids", "raw_grounding_ordinal_ids", "groundable_ordinal_ids",
        "grounding_ordinal_ids", "planned_fallback_ordinal_ids",
        "teacher_binding_ordinal_ids",
    ):
        result[key] = [int(value) for value in component.get(key, []) if int(value) in owned]
    result["primary_aspect_by_ordinal"] = {
        str(key): value
        for key, value in dict(component.get("primary_aspect_by_ordinal") or {}).items()
        if int(key) in owned
    }
    return _finalize_component(result)


def _finalize_component(component: dict) -> dict:
    owned = [int(value) for value in component["owned_ordinal_ids"]]
    grounding = [int(value) for value in component["grounding_ordinal_ids"]]
    fallback = [int(value) for value in component["planned_fallback_ordinal_ids"]]
    component["owned_ordinal_count"] = len(owned)
    component["owned_ordinal_ids_hash"] = _hash(owned)
    component["unadjusted_grounding_target_count"] = len(
        component["raw_grounding_ordinal_ids"],
    )
    component["groundable_capacity_count"] = len(component["groundable_ordinal_ids"])
    component["grounding_required_count"] = len(grounding)
    component["planned_fallback_count"] = len(fallback)
    component["teacher_binding_required_count"] = len(
        component.get("teacher_binding_ordinal_ids") or [],
    )
    component["primary_aspect_required_distinct_count"] = len(set(
        dict(component.get("primary_aspect_by_ordinal") or {}).values(),
    ))
    component["semantic_capacity_state"] = _capacity_state(
        component["unadjusted_grounding_target_count"],
        component["groundable_capacity_count"],
    )
    component["quality_component_key"] = _hash({
        "grounding_revision": component["comment_grounding_revision"],
        "source_revision_id": component["source_revision_id"],
        "owned_ordinal_ids_hash": component["owned_ordinal_ids_hash"],
    })
    return component


def _validate_target(
    plan: ChannelCommentPlanContract,
    target: ChannelCommentQualityTargetRevision,
) -> None:
    components = list(target.component_targets_json)
    if target.target_state != "frozen" or not components:
        raise ValueError("channel_comment_quality_target_state_invalid")
    for component in components:
        _validate_component(component)
    owned = _flatten_ordinals(components, "owned_ordinal_ids")
    expected = list(range(1, int(plan.required_distinct_account_count) + 1))
    if sorted(owned) != expected or len(owned) != len(set(owned)):
        raise ValueError("channel_comment_quality_component_set_invalid")
    if target.component_set_hash != _hash(components):
        raise ValueError("channel_comment_quality_component_hash_invalid")
    if int(target.aggregate_grounding_required_count) != _sum(
        components, "grounding_required_count",
    ):
        raise ValueError("channel_comment_quality_grounding_aggregate_invalid")
    if int(target.aggregate_planned_fallback_count) != _sum(
        components, "planned_fallback_count",
    ):
        raise ValueError("channel_comment_quality_fallback_aggregate_invalid")


def _validate_component(component: dict) -> None:
    owned = [int(value) for value in component.get("owned_ordinal_ids", [])]
    grounded = [int(value) for value in component.get("grounding_ordinal_ids", [])]
    fallback = [int(value) for value in component.get("planned_fallback_ordinal_ids", [])]
    if not owned or len(owned) != len(set(owned)):
        raise ValueError("channel_comment_quality_component_owned_invalid")
    if sorted(grounded + fallback) != sorted(owned) or set(grounded) & set(fallback):
        raise ValueError("channel_comment_quality_component_roles_invalid")
    canonical = _finalize_component(dict(component))
    if canonical != component:
        raise ValueError("channel_comment_quality_component_contract_invalid")


def _semantic_variants(source: ChannelMessageSourceRevision) -> list[dict]:
    evidence = _semantic_evidence_units(source.source_text_snapshot)
    return [
        {**unit, "speech_act": speech_act}
        for unit in evidence
        for speech_act in SPEECH_ACTS
    ]


def _semantic_evidence_units(source_text: str) -> list[dict]:
    if not source_text.strip():
        return []
    extracted = _extract_channel_post_aspects(source_text)
    units = []
    teacher = str(extracted.get("teacher_name") or "")
    if teacher:
        units.append({
            "aspect_code": "teacher_identity", "aspect_text": teacher,
            "teacher_name": teacher,
        })
    for aspect in extracted.get("aspects") or []:
        text = "/".join(str(value) for value in (aspect.get("matches") or [])[:3])
        units.append({
            "aspect_code": str(aspect.get("code") or "source_fact"),
            "aspect_text": text or str(aspect.get("label") or ""),
            "teacher_name": "",
        })
    if not units:
        units.append({
            "aspect_code": "source_fact", "aspect_text": source_text.strip()[:500],
            "teacher_name": "",
        })
    return _deduplicate_units(units)


def _deduplicate_units(units: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for unit in units:
        identity = (unit["aspect_code"], unit["aspect_text"], unit["teacher_name"])
        if identity in seen:
            continue
        result.append(unit)
        seen.add(identity)
    return result


def _assignment_specs(variants: list[dict], ordinals: list[int]) -> dict[int, dict]:
    return {ordinal: variants[index] for index, ordinal in enumerate(ordinals)}


def _teacher_ordinals(specs: dict[int, dict]) -> list[int]:
    return [ordinal for ordinal, spec in specs.items() if spec["teacher_name"]]


def _aspect_by_ordinal(specs: dict[int, dict]) -> dict[str, str]:
    return {str(ordinal): spec["aspect_code"] for ordinal, spec in specs.items()}


def _next_grounding_revision(target: ChannelCommentQualityTargetRevision) -> int:
    return max(
        (int(row["comment_grounding_revision"]) for row in target.component_targets_json),
        default=0,
    ) + 1


def _flatten_ordinals(components: list[dict], key: str) -> list[int]:
    return [int(value) for row in components for value in row.get(key, [])]


def _sum(components: list[dict], key: str) -> int:
    return sum(int(row.get(key) or 0) for row in components)


def _capacity_state(raw_count: int, capacity_count: int) -> str:
    if capacity_count == 0:
        return "none"
    return "sufficient" if capacity_count >= raw_count else "capacity_adjusted"


def _aggregate_capacity_state(states: set[str]) -> str:
    if states == {"none"}:
        return "none"
    if states <= {"sufficient"}:
        return "sufficient"
    return "capacity_adjusted"


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "SEMANTIC_CAPACITY_POLICY_VERSION",
    "append_quality_target_revision",
    "build_quality_target_component",
    "current_quality_target",
    "freeze_initial_quality_target",
    "quality_assignment_content",
    "quality_target_projection",
    "target_component_for_ordinal",
]
