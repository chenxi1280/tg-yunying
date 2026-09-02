from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, ChannelCommentGroundingEnrollment, ChannelDiscussionGroupBinding, Task

from .channel_comment_discussion_contracts import EnrollmentRequest, GROUNDING_CONTRACT_VERSION
from .channel_comment_discussion_freshness import group_binding_fresh


@dataclass(frozen=True)
class EnrollmentCloseRequest:
    tenant_id: int
    task_id: str
    enrollment_id: str
    expected_config_revision: int
    expected_lifecycle_epoch: int
    closed_at: datetime
    operator_id: str
    approval_reference: str


def activate_grounding_enrollment(
    session: Session,
    request: EnrollmentRequest,
) -> ChannelCommentGroundingEnrollment:
    task = _locked_task(session, request.tenant_id, request.task_id)
    _validate_enrollment_task(task, request)
    binding = session.get(ChannelDiscussionGroupBinding, request.group_binding_id)
    _validate_enrollment_binding(
        session, task, binding=binding, observed_at=request.enabled_at,
    )
    existing = active_grounding_enrollment(session, task)
    if existing is not None:
        _require_matching_enrollment(existing, binding, request)
        return existing
    enrollment = _new_enrollment(session, task, binding=binding, request=request)
    _write_audit(session, enrollment, "频道评论 Grounding Enrollment 激活")
    return enrollment


def close_grounding_enrollment(
    session: Session,
    request: EnrollmentCloseRequest,
) -> ChannelCommentGroundingEnrollment:
    task = _locked_task(session, request.tenant_id, request.task_id)
    _validate_close_task(task, request)
    enrollment = session.get(ChannelCommentGroundingEnrollment, request.enrollment_id)
    if enrollment is None or enrollment.task_id != task.id:
        raise ValueError("channel_comment_grounding_enrollment_not_found")
    if enrollment.enrollment_state == "closed":
        return enrollment
    if enrollment.enrollment_state != "active":
        raise ValueError("channel_comment_grounding_enrollment_state_invalid")
    enrollment.enrollment_state = "closed"
    enrollment.closed_at = request.closed_at
    _write_audit(session, enrollment, "频道评论 Grounding Enrollment 关闭")
    session.flush()
    return enrollment


def active_grounding_enrollment(
    session: Session,
    task: Task,
) -> ChannelCommentGroundingEnrollment | None:
    return session.scalar(select(ChannelCommentGroundingEnrollment).where(
        ChannelCommentGroundingEnrollment.task_id == task.id,
        ChannelCommentGroundingEnrollment.task_config_revision == task.config_revision,
        ChannelCommentGroundingEnrollment.enrollment_state == "active",
    ))


def latest_grounding_enrollment(
    session: Session,
    task: Task,
) -> ChannelCommentGroundingEnrollment | None:
    return session.scalar(select(ChannelCommentGroundingEnrollment).where(
        ChannelCommentGroundingEnrollment.task_id == task.id,
    ).order_by(ChannelCommentGroundingEnrollment.enrollment_revision.desc()))


def _locked_task(session: Session, tenant_id: int, task_id: str) -> Task | None:
    return session.scalar(select(Task).where(
        Task.id == task_id,
        Task.tenant_id == tenant_id,
    ).with_for_update())


def _validate_enrollment_task(task: Task | None, request: EnrollmentRequest) -> None:
    if task is None or task.type != "channel_comment":
        raise ValueError("channel_comment_task_not_found")
    _validate_task_revision(task, request.expected_config_revision, request.expected_lifecycle_epoch)
    if task.status != "running":
        raise ValueError("channel_comment_enrollment_task_not_running")
    _require_approval(request.operator_id, request.approval_reference)
    _validate_activation_config(dict(task.type_config or {}))


def _validate_close_task(task: Task | None, request: EnrollmentCloseRequest) -> None:
    if task is None or task.type != "channel_comment":
        raise ValueError("channel_comment_task_not_found")
    _validate_task_revision(task, request.expected_config_revision, request.expected_lifecycle_epoch)
    _require_approval(request.operator_id, request.approval_reference)


def _validate_task_revision(task: Task, config_revision: int, lifecycle_epoch: int) -> None:
    if task.config_revision != config_revision:
        raise ValueError("channel_comment_enrollment_config_drift")
    if task.task_lifecycle_epoch != lifecycle_epoch:
        raise ValueError("channel_comment_enrollment_epoch_drift")


def _require_approval(operator_id: str, approval_reference: str) -> None:
    if not operator_id.strip() or not approval_reference.strip():
        raise ValueError("channel_comment_enrollment_approval_required")


def _validate_activation_config(config: dict) -> None:
    flags = ("ai_two_stage_enabled", "ai_content_route_v2_enabled", "channel_comment_grounding_v1_enabled")
    if not all(config.get(name) is True for name in flags):
        raise ValueError("channel_comment_grounding_activation_incomplete")
    if int(config.get("rolling_window_days") or 0) != 3 or int(config.get("daily_comment_cap") or 0) <= 0:
        raise ValueError("channel_comment_grounding_window_or_cap_invalid")
    if not config.get("ai_model") or config.get("ai_model") == config.get("ai_semantic_reviewer_model"):
        raise ValueError("channel_comment_reviewer_model_not_independent")
    if not config.get("ai_content_policy_version_id") or not config.get("ai_content_allowed_routes"):
        raise ValueError("channel_comment_canonical_route_incomplete")
    weights = int(config.get("unicode_emoji_weight_bps") or 0) + int(config.get("image_meme_weight_bps") or 0)
    if weights != 10000:
        raise ValueError("comment_fallback_weights_must_total_10000")


def _validate_enrollment_binding(
    session: Session,
    task: Task,
    *,
    binding: ChannelDiscussionGroupBinding | None,
    observed_at: datetime,
) -> None:
    if binding is None or binding.tenant_id != task.tenant_id or not binding.is_current:
        raise ValueError("discussion_binding_blocked")
    if binding.binding_status != "active" or not binding.discussion_peer_id:
        raise ValueError("channel_comment_discussion_unbound")
    if not group_binding_fresh(session, binding, observed_at):
        raise ValueError("discussion_binding_stale")


def _require_matching_enrollment(
    enrollment: ChannelCommentGroundingEnrollment,
    binding: ChannelDiscussionGroupBinding,
    request: EnrollmentRequest,
) -> None:
    expected = (binding.id, binding.binding_revision, binding.identity_hash, request.enabled_at)
    actual = (
        enrollment.group_binding_id,
        enrollment.group_binding_revision,
        enrollment.group_binding_identity_hash,
        enrollment.enabled_at,
    )
    if expected != actual:
        raise ValueError("channel_comment_enrollment_identity_conflict")


def _new_enrollment(
    session: Session,
    task: Task,
    *,
    binding: ChannelDiscussionGroupBinding,
    request: EnrollmentRequest,
) -> ChannelCommentGroundingEnrollment:
    snapshot = _activation_snapshot(task, binding, request.enabled_at)
    enrollment = ChannelCommentGroundingEnrollment(
        tenant_id=task.tenant_id, task_id=task.id,
        task_config_revision=task.config_revision, task_lifecycle_epoch=task.task_lifecycle_epoch,
        enrollment_revision=_next_revision(session, task.id), enabled_at=request.enabled_at,
        contract_version=GROUNDING_CONTRACT_VERSION, contracts_hash=_stable_hash(snapshot["contracts"]),
        group_binding_id=binding.id, group_binding_revision=binding.binding_revision,
        group_binding_identity_hash=binding.identity_hash, activation_hash=_stable_hash(snapshot),
        operator_id=request.operator_id, approval_reference=request.approval_reference,
        enrollment_state="active",
    )
    session.add(enrollment)
    session.flush()
    return enrollment


def _next_revision(session: Session, task_id: str) -> int:
    current = session.scalar(select(func.max(ChannelCommentGroundingEnrollment.enrollment_revision)).where(
        ChannelCommentGroundingEnrollment.task_id == task_id,
    ))
    return int(current or 0) + 1


def _activation_snapshot(task: Task, binding: ChannelDiscussionGroupBinding, enabled_at: datetime) -> dict:
    config = dict(task.type_config or {})
    return {
        "task_id": task.id, "config_revision": task.config_revision,
        "lifecycle_epoch": task.task_lifecycle_epoch, "enabled_at": enabled_at.isoformat(),
        "binding_id": binding.id, "binding_revision": binding.binding_revision,
        "binding_identity_hash": binding.identity_hash,
        "contracts": {
            "grounding": GROUNDING_CONTRACT_VERSION,
            "content_policy": config.get("ai_content_policy_version_id"),
            "routes": list(config.get("ai_content_allowed_routes") or []),
        },
    }


def _write_audit(session: Session, enrollment: ChannelCommentGroundingEnrollment, action: str) -> None:
    session.add(AuditLog(
        tenant_id=enrollment.tenant_id, actor=enrollment.operator_id[:100], action=action,
        target_type="channel_comment_grounding_enrollment", target_id=enrollment.id,
        detail=json.dumps({
            "approval_reference": enrollment.approval_reference,
            "activation_hash": enrollment.activation_hash,
            "enrollment_state": enrollment.enrollment_state,
        }, ensure_ascii=False, sort_keys=True),
    ))


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "EnrollmentCloseRequest", "activate_grounding_enrollment",
    "active_grounding_enrollment", "close_grounding_enrollment",
    "latest_grounding_enrollment",
]
