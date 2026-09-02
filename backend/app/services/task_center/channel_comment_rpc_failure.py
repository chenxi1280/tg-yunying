from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Action, ChannelDiscussionGroupBinding, ChannelDiscussionThreadBinding

from .channel_comment_discussion_contracts import (
    MembershipObservation,
    ThreadProbeObservation,
    record_membership_fact,
    record_thread_probe,
)


NEGATIVE_MEMBERSHIP_FRESHNESS = timedelta(minutes=30)
MEMBERSHIP_FAILURE_STATUS = {
    "discussion_membership_required": "not_participant",
    "discussion_send_forbidden": "restricted",
    "discussion_access_rejected_for_account": "inaccessible",
    "account_banned_in_discussion": "banned",
}


def project_comment_pre_mutation_failure(
    session: Session,
    action: Action,
    *,
    payload,
    result,
    attempt_id: str,
    observed_at: datetime,
) -> dict:
    failure_code = str(result.failure_type or "")
    if result.ok or result.remote_mutation_started is not False:
        return {}
    if failure_code == "source_comment_identity_reprobe_required":
        _record_source_reprobe(
            session, action, payload=payload,
            attempt_id=attempt_id, observed_at=observed_at,
        )
        return {"source_comment_reprobe_required": True}
    membership_status = MEMBERSHIP_FAILURE_STATUS.get(failure_code)
    if membership_status is None:
        return {}
    fact = _record_negative_membership(
        session, action, payload=payload,
        failure_code=failure_code, membership_status=membership_status,
        attempt_id=attempt_id, observed_at=observed_at,
    )
    return {"discussion_membership_remote_fact": _membership_fact_payload(fact, payload)}


def _record_negative_membership(
    session: Session,
    action: Action,
    *,
    payload,
    failure_code: str,
    membership_status: str,
    attempt_id: str,
    observed_at: datetime,
):
    binding = session.get(ChannelDiscussionGroupBinding, payload.discussion_group_binding_id)
    if binding is None or binding.identity_hash != payload.discussion_group_identity_hash:
        raise RuntimeError("discussion_failure_binding_identity_missing")
    return record_membership_fact(session, MembershipObservation(
        tenant_id=action.tenant_id, account_id=int(action.account_id),
        group_binding_id=binding.id, discussion_peer_id=str(binding.discussion_peer_id),
        membership_status=membership_status, can_send=False, observed_at=observed_at,
        fresh_until_at=observed_at + NEGATIVE_MEMBERSHIP_FRESHNESS,
        evidence_json={
            "action_id": action.id, "execution_attempt_id": attempt_id,
            "failure_code": failure_code, "remote_mutation_started": False,
        },
    ))


def _record_source_reprobe(
    session: Session,
    action: Action,
    *,
    payload,
    attempt_id: str,
    observed_at: datetime,
) -> None:
    thread = session.get(ChannelDiscussionThreadBinding, payload.discussion_thread_binding_id)
    if thread is None or thread.identity_hash != payload.discussion_thread_identity_hash:
        raise RuntimeError("discussion_failure_thread_identity_missing")
    record_thread_probe(session, ThreadProbeObservation(
        tenant_id=action.tenant_id, source_revision_id=payload.source_revision_id,
        group_binding_id=payload.discussion_group_binding_id,
        probe_request_id=f"post-comment-reprobe:{attempt_id}",
        probe_status="reprobe_required", probe_stage="post_comment_rpc_reject",
        observed_at=observed_at, discussion_peer_id=payload.discussion_peer_id,
        thread_root_message_id=payload.thread_root_message_id,
        error_code="source_comment_identity_reprobe_required",
        evidence_json={"action_id": action.id, "execution_attempt_id": attempt_id},
    ))


def _membership_fact_payload(fact, payload) -> dict:
    return {
        "fact_id": fact.id, "account_id": fact.account_id,
        "discussion_peer_id": fact.discussion_peer_id,
        "discussion_group_binding_id": fact.group_binding_id,
        "discussion_group_binding_revision": payload.discussion_group_binding_revision,
        "membership_status": fact.membership_status, "can_send": fact.can_send,
        "observed_at": fact.observed_at.isoformat(),
        "fresh_until_at": fact.fresh_until_at.isoformat() if fact.fresh_until_at else None,
    }


__all__ = ["project_comment_pre_mutation_failure"]
