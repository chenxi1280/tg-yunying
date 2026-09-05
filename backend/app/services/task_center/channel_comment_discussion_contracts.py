from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentListenerErrorEvent,
    ChannelDiscussionGroupBinding,
    ChannelDiscussionGroupProbeEvent,
    ChannelDiscussionThreadBinding,
    ChannelDiscussionThreadProbeEvent,
    ChannelMessageSourceRevision,
    DiscussionMembershipFact,
    OperationTarget,
    TgAccount,
)


GROUNDING_CONTRACT_VERSION = "channel_comment_business_grounding_v1_2"
AUTHORITATIVE_GROUP_STAGE = "channels_get_full_channel"
AUTHORITATIVE_THREAD_STAGE = "discussion_message_lookup"
MEMBERSHIP_READY_STATUSES = frozenset({"joined", "already_joined"})
MEMBERSHIP_FORBIDDEN_STATUSES = frozenset({"restricted", "banned", "inaccessible"})


@dataclass(frozen=True)
class GroupProbeObservation:
    tenant_id: int
    channel_target_id: int
    target_reference_revision: int
    channel_peer_id: str
    probe_request_id: str
    probe_status: str
    probe_stage: str
    observed_at: datetime
    fresh_until_at: datetime | None = None
    account_id: int | None = None
    discussion_target_id: int | None = None
    discussion_peer_id: str | None = None
    error_code: str = ""
    evidence_json: dict | None = None


@dataclass(frozen=True)
class ThreadProbeObservation:
    tenant_id: int
    source_revision_id: str
    group_binding_id: str
    probe_request_id: str
    probe_status: str
    probe_stage: str
    observed_at: datetime
    fresh_until_at: datetime | None = None
    discussion_peer_id: str | None = None
    thread_root_message_id: int | None = None
    error_code: str = ""
    evidence_json: dict | None = None


@dataclass(frozen=True)
class MembershipObservation:
    tenant_id: int
    account_id: int
    group_binding_id: str
    discussion_peer_id: str
    membership_status: str
    can_send: bool
    observed_at: datetime
    fresh_until_at: datetime | None
    evidence_json: dict | None = None


@dataclass(frozen=True)
class EnrollmentRequest:
    tenant_id: int
    task_id: str
    expected_config_revision: int
    expected_lifecycle_epoch: int
    group_binding_id: str
    enabled_at: datetime
    operator_id: str
    approval_reference: str


def record_group_probe(
    session: Session,
    observation: GroupProbeObservation,
) -> ChannelDiscussionGroupBinding | None:
    _lock_group_owner(session, observation)
    existing_event = session.scalar(select(ChannelDiscussionGroupProbeEvent).where(
        ChannelDiscussionGroupProbeEvent.tenant_id == observation.tenant_id,
        ChannelDiscussionGroupProbeEvent.channel_target_id == observation.channel_target_id,
        ChannelDiscussionGroupProbeEvent.probe_request_id == observation.probe_request_id,
    ))
    if existing_event is not None:
        return current_group_binding(session, observation.tenant_id, observation.channel_target_id)
    event = _new_group_probe_event(observation)
    session.add(event)
    session.flush()
    if observation.probe_status != "success":
        return current_group_binding(session, observation.tenant_id, observation.channel_target_id)
    _require_authoritative_group_observation(observation)
    return _record_group_binding(session, observation, event)


def _new_group_probe_event(observation: GroupProbeObservation) -> ChannelDiscussionGroupProbeEvent:
    return ChannelDiscussionGroupProbeEvent(
        tenant_id=observation.tenant_id,
        channel_target_id=observation.channel_target_id,
        target_reference_revision=observation.target_reference_revision,
        probe_request_id=observation.probe_request_id,
        probe_status=observation.probe_status,
        probe_stage=observation.probe_stage,
        account_id=observation.account_id,
        observed_linked_chat_id=observation.discussion_peer_id,
        observed_at=observation.observed_at,
        fresh_until_at=observation.fresh_until_at,
        evidence_json=dict(observation.evidence_json or {}),
        error_code=observation.error_code,
    )


def _require_authoritative_group_observation(observation: GroupProbeObservation) -> None:
    if observation.probe_stage != AUTHORITATIVE_GROUP_STAGE:
        raise ValueError("discussion_group_probe_not_authoritative")
    if not observation.channel_peer_id:
        raise ValueError("discussion_group_channel_peer_missing")


def _record_group_binding(
    session: Session,
    observation: GroupProbeObservation,
    event: ChannelDiscussionGroupProbeEvent,
) -> ChannelDiscussionGroupBinding:
    current = current_group_binding(session, observation.tenant_id, observation.channel_target_id)
    identity_hash = _group_identity_hash(observation)
    if current is not None and current.identity_hash == identity_hash:
        return current
    if current is not None:
        current.is_current = False
    binding = ChannelDiscussionGroupBinding(
        tenant_id=observation.tenant_id,
        channel_target_id=observation.channel_target_id,
        target_reference_revision=observation.target_reference_revision,
        binding_revision=int(current.binding_revision if current else 0) + 1,
        channel_peer_id=observation.channel_peer_id,
        discussion_target_id=observation.discussion_target_id,
        discussion_peer_id=observation.discussion_peer_id,
        identity_hash=identity_hash,
        binding_status="active" if observation.discussion_peer_id else "unbound",
        probe_event_id=event.id,
        supersedes_binding_id=current.id if current else None,
        is_current=True,
        observed_at=observation.observed_at,
        fresh_until_at=observation.fresh_until_at,
    )
    session.add(binding)
    session.flush()
    if current is not None:
        from .channel_comment_discussion_change import fence_group_binding_change

        fence_group_binding_change(
            session, current.id, binding.id, occurred_at=observation.observed_at,
        )
    return binding


def current_group_binding(
    session: Session,
    tenant_id: int,
    channel_target_id: int,
) -> ChannelDiscussionGroupBinding | None:
    return session.scalar(select(ChannelDiscussionGroupBinding).where(
        ChannelDiscussionGroupBinding.tenant_id == tenant_id,
        ChannelDiscussionGroupBinding.channel_target_id == channel_target_id,
        ChannelDiscussionGroupBinding.is_current.is_(True),
    ))


def record_thread_probe(
    session: Session,
    observation: ThreadProbeObservation,
) -> ChannelDiscussionThreadBinding | None:
    _lock_thread_owner(session, observation)
    existing_event = session.scalar(select(ChannelDiscussionThreadProbeEvent).where(
        ChannelDiscussionThreadProbeEvent.tenant_id == observation.tenant_id,
        ChannelDiscussionThreadProbeEvent.source_revision_id == observation.source_revision_id,
        ChannelDiscussionThreadProbeEvent.probe_request_id == observation.probe_request_id,
    ))
    if existing_event is not None:
        return current_thread_binding(session, observation.source_revision_id, observation.group_binding_id)
    event = _new_thread_probe_event(observation)
    session.add(event)
    session.flush()
    if observation.probe_status != "success":
        return current_thread_binding(session, observation.source_revision_id, observation.group_binding_id)
    _require_authoritative_thread_observation(observation)
    return _record_thread_binding(session, observation, event)


def _new_thread_probe_event(observation: ThreadProbeObservation) -> ChannelDiscussionThreadProbeEvent:
    return ChannelDiscussionThreadProbeEvent(
        tenant_id=observation.tenant_id,
        source_revision_id=observation.source_revision_id,
        group_binding_id=observation.group_binding_id,
        probe_request_id=observation.probe_request_id,
        probe_status=observation.probe_status,
        probe_stage=observation.probe_stage,
        observed_thread_root_message_id=observation.thread_root_message_id,
        observed_at=observation.observed_at,
        fresh_until_at=observation.fresh_until_at,
        evidence_json=dict(observation.evidence_json or {}),
        error_code=observation.error_code,
    )


def _require_authoritative_thread_observation(observation: ThreadProbeObservation) -> None:
    if observation.probe_stage != AUTHORITATIVE_THREAD_STAGE:
        raise ValueError("discussion_thread_probe_not_authoritative")
    if not observation.discussion_peer_id or not observation.thread_root_message_id:
        raise ValueError("discussion_thread_identity_missing")


def _record_thread_binding(
    session: Session,
    observation: ThreadProbeObservation,
    event: ChannelDiscussionThreadProbeEvent,
) -> ChannelDiscussionThreadBinding:
    current = current_thread_binding(session, observation.source_revision_id, observation.group_binding_id)
    identity_hash = _thread_identity_hash(observation)
    if current is not None and current.identity_hash == identity_hash:
        return current
    if current is not None:
        current.is_current = False
    binding = ChannelDiscussionThreadBinding(
        tenant_id=observation.tenant_id,
        source_revision_id=observation.source_revision_id,
        group_binding_id=observation.group_binding_id,
        thread_revision=int(current.thread_revision if current else 0) + 1,
        discussion_peer_id=str(observation.discussion_peer_id),
        thread_root_message_id=int(observation.thread_root_message_id),
        identity_hash=identity_hash,
        probe_event_id=event.id,
        supersedes_thread_binding_id=current.id if current else None,
        is_current=True,
        observed_at=observation.observed_at,
    )
    session.add(binding)
    session.flush()
    if current is not None:
        from .channel_comment_discussion_change import fence_thread_binding_change

        fence_thread_binding_change(
            session, current.id, binding.id, occurred_at=observation.observed_at,
        )
    return binding


def current_thread_binding(
    session: Session,
    source_revision_id: str,
    group_binding_id: str,
) -> ChannelDiscussionThreadBinding | None:
    return session.scalar(select(ChannelDiscussionThreadBinding).where(
        ChannelDiscussionThreadBinding.source_revision_id == source_revision_id,
        ChannelDiscussionThreadBinding.group_binding_id == group_binding_id,
        ChannelDiscussionThreadBinding.is_current.is_(True),
    ))


def record_membership_fact(
    session: Session,
    observation: MembershipObservation,
) -> DiscussionMembershipFact:
    _validate_membership_observation(observation)
    _lock_membership_owner(session, observation)
    current = current_membership_fact(
        session,
        tenant_id=observation.tenant_id,
        account_id=observation.account_id,
        discussion_peer_id=observation.discussion_peer_id,
        group_binding_id=observation.group_binding_id,
    )
    if current is not None:
        current.is_current = False
    values = asdict(observation)
    values["evidence_json"] = dict(observation.evidence_json or {})
    fact = DiscussionMembershipFact(
        **values,
        fact_revision=int(current.fact_revision if current else 0) + 1,
        supersedes_fact_id=current.id if current else None,
        is_current=True,
    )
    session.add(fact)
    session.flush()
    return fact


def _lock_group_owner(session: Session, observation: GroupProbeObservation) -> None:
    owner = session.scalar(select(OperationTarget.id).where(
        OperationTarget.id == observation.channel_target_id,
        OperationTarget.tenant_id == observation.tenant_id,
    ).with_for_update())
    if owner is None:
        raise ValueError("discussion_channel_target_missing")


def _lock_thread_owner(session: Session, observation: ThreadProbeObservation) -> None:
    owner = session.scalar(select(ChannelMessageSourceRevision.id).where(
        ChannelMessageSourceRevision.id == observation.source_revision_id,
        ChannelMessageSourceRevision.tenant_id == observation.tenant_id,
    ).with_for_update())
    if owner is None:
        raise ValueError("discussion_source_revision_missing")


def _lock_membership_owner(session: Session, observation: MembershipObservation) -> None:
    owner = session.scalar(select(TgAccount.id).where(
        TgAccount.id == observation.account_id,
        TgAccount.tenant_id == observation.tenant_id,
    ).with_for_update())
    if owner is None:
        raise ValueError("discussion_membership_account_missing")


def _validate_membership_observation(observation: MembershipObservation) -> None:
    allowed = MEMBERSHIP_READY_STATUSES | MEMBERSHIP_FORBIDDEN_STATUSES | {"not_participant", "unknown"}
    if observation.membership_status not in allowed:
        raise ValueError("discussion_membership_status_invalid")
    if observation.can_send and observation.membership_status not in MEMBERSHIP_READY_STATUSES:
        raise ValueError("discussion_membership_can_send_invalid")


def current_membership_fact(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    discussion_peer_id: str,
    group_binding_id: str,
) -> DiscussionMembershipFact | None:
    return session.scalar(select(DiscussionMembershipFact).where(
        DiscussionMembershipFact.tenant_id == tenant_id,
        DiscussionMembershipFact.account_id == account_id,
        DiscussionMembershipFact.discussion_peer_id == discussion_peer_id,
        DiscussionMembershipFact.group_binding_id == group_binding_id,
        DiscussionMembershipFact.is_current.is_(True),
    ))


def current_membership_facts(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    discussion_peer_id: str,
    group_binding_id: str,
) -> dict[int, DiscussionMembershipFact]:
    if not account_ids:
        return {}
    rows = session.scalars(select(DiscussionMembershipFact).where(
        DiscussionMembershipFact.tenant_id == tenant_id,
        DiscussionMembershipFact.account_id.in_(account_ids),
        DiscussionMembershipFact.discussion_peer_id == discussion_peer_id,
        DiscussionMembershipFact.group_binding_id == group_binding_id,
        DiscussionMembershipFact.is_current.is_(True),
    ))
    return {row.account_id: row for row in rows}


def membership_ready(
    fact: DiscussionMembershipFact | None,
    now_value: datetime,
) -> bool:
    return bool(
        fact is not None
        and fact.membership_status in MEMBERSHIP_READY_STATUSES
        and fact.can_send
        and fact.fresh_until_at is not None
        and _wall(fact.fresh_until_at) >= _wall(now_value)
    )


def _group_identity_hash(observation: GroupProbeObservation) -> str:
    return _stable_hash({
        "channel_peer_id": observation.channel_peer_id,
        "discussion_peer_id": observation.discussion_peer_id,
        "binding_status": "active" if observation.discussion_peer_id else "unbound",
    })


def _thread_identity_hash(observation: ThreadProbeObservation) -> str:
    return _stable_hash({
        "discussion_peer_id": observation.discussion_peer_id,
        "thread_root_message_id": observation.thread_root_message_id,
        "group_binding_id": observation.group_binding_id,
    })


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "AUTHORITATIVE_GROUP_STAGE", "AUTHORITATIVE_THREAD_STAGE",
    "EnrollmentRequest", "GROUNDING_CONTRACT_VERSION",
    "GroupProbeObservation",
    "MembershipObservation",
    "ThreadProbeObservation",
    "current_group_binding",
    "current_membership_fact",
    "current_membership_facts",
    "current_thread_binding",
    "membership_ready",
    "record_group_probe",
    "record_membership_fact",
    "record_thread_probe",
]
