from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    PostSendVisibilityObservation,
    PostSendVisibilityPolicyRevision,
)
from app.services._common import _now


def ensure_visibility_policy(
    session: Session,
    tenant_id: int,
) -> PostSendVisibilityPolicyRevision:
    policy = session.scalar(
        select(PostSendVisibilityPolicyRevision)
        .where(
            PostSendVisibilityPolicyRevision.tenant_id == tenant_id,
            PostSendVisibilityPolicyRevision.state == "active",
        )
        .with_for_update()
    )
    if policy is not None:
        return policy
    policy = PostSendVisibilityPolicyRevision(
        tenant_id=tenant_id,
        revision=1,
        normal_window_seconds=15,
        elevated_window_seconds=90,
    )
    session.add(policy)
    session.flush()
    return policy


def visibility_window_seconds(
    session: Session,
    action: Action,
    *,
    elevated: bool,
) -> int:
    policy = ensure_visibility_policy(session, action.tenant_id)
    value = (
        policy.elevated_window_seconds
        if elevated
        else policy.normal_window_seconds
    )
    if int(value or 0) <= 0:
        raise RuntimeError("post_send_visibility_policy_window_invalid")
    return int(value)


def open_visibility_observation(
    session: Session,
    action: Action,
    *,
    attempt: ExecutionAttempt,
    remote_message_id: str,
    target_peer: str,
    window_seconds: int,
    observed_at: datetime | None = None,
) -> PostSendVisibilityObservation:
    policy = ensure_visibility_policy(session, action.tenant_id)
    existing = session.scalar(
        select(PostSendVisibilityObservation).where(
            PostSendVisibilityObservation.action_id == action.id,
            PostSendVisibilityObservation.attempt_id == attempt.id,
            PostSendVisibilityObservation.policy_revision_id == policy.id,
        )
    )
    if existing is not None:
        return existing
    opened_at = observed_at or _now()
    observation = PostSendVisibilityObservation(
        tenant_id=action.tenant_id,
        policy_revision_id=policy.id,
        action_id=action.id,
        attempt_id=attempt.id,
        remote_message_id=remote_message_id,
        target_peer=target_peer,
        accepted_content_hash=_accepted_content_hash(action),
        deadline_at=opened_at + timedelta(seconds=window_seconds),
    )
    session.add(observation)
    session.flush()
    return observation


def settle_visibility_observation(
    session: Session,
    action: Action,
    *,
    state: str,
    terminal_reason: str,
    checked_at: datetime | None = None,
) -> PostSendVisibilityObservation | None:
    observation = session.scalar(
        select(PostSendVisibilityObservation)
        .where(PostSendVisibilityObservation.action_id == action.id)
        .order_by(PostSendVisibilityObservation.created_at.desc())
        .limit(1)
    )
    if observation is None:
        return None
    if observation.state != "visibility_pending":
        return observation
    observation.state = state
    observation.terminal_reason = terminal_reason
    observation.checked_at = checked_at or _now()
    session.flush()
    from .negative_outcome_events import observe_visibility_outcome

    observe_visibility_outcome(session, action, observation)
    return observation


def _accepted_content_hash(action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return str(
        payload.get("accepted_content_hash")
        or action.candidate_hash
        or ""
    )


__all__ = [
    "ensure_visibility_policy",
    "open_visibility_observation",
    "settle_visibility_observation",
    "visibility_window_seconds",
]
