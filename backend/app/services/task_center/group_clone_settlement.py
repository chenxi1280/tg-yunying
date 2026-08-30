from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneMessagePart,
    CloneSequencerHeadCase,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
    TelegramGatewayMutationIdentity,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services._common import _now

from .payloads import GroupCloneMutationPayload, GroupCloneSendPayload


def project_group_clone_result(session: Session, action: Action) -> None:
    if action.task_type != "group_clone" or action.action_type not in {
        "group_clone_send", "group_clone_mutation",
    }:
        return
    payload = _clone_payload(action)
    obligation = session.get(CloneDeliveryObligation, payload.obligation_id)
    identity = session.get(TelegramGatewayMutationIdentity, payload.gateway_mutation_identity_id)
    attempt = _latest_attempt(session, action)
    if obligation is None or identity is None:
        raise RuntimeError("group_clone_settlement_contract_missing")
    if attempt is None:
        if action.status not in {"failed", "skipped", "cancelled"}:
            raise RuntimeError("group_clone_settlement_attempt_missing")
        obligation.state = "failed_terminal"
        identity.state = "allocated"
        _ensure_failed_head_case(session, action, obligation)
        return
    if action.status == "success":
        _confirm_remote_mutation(
            session, action, payload=payload, obligation=obligation,
            identity=identity, attempt=attempt,
        )
        return
    if action.status == "unknown_after_send":
        obligation.state = "unknown_after_send"
        identity.state = "unknown"
        return
    if action.status in {"failed", "skipped", "cancelled"}:
        _settle_terminal_failure(session, action, obligation=obligation, identity=identity, attempt=attempt)


def _confirm_remote_mutation(session, action, *, payload, obligation, identity, attempt) -> None:
    expected_kind = _expected_fact_kind(action)
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.attempt_id == attempt.id,
        FulfillmentRemoteFact.fact_kind == expected_kind,
    ))
    if fact is None:
        raise RuntimeError("group_clone_remote_fact_missing")
    obligation.state = "succeeded"
    obligation.resolved_at = fact.observed_at
    identity.state = "closed"
    action.result = {**dict(action.result or {}), "remote_fact_id": fact.fact_id}
    if action.action_type == "group_clone_send":
        _record_outbound_mapping(session, action, payload=payload, identity=identity, attempt=attempt)
    if action.action_type == "group_clone_send" and _message_part(session, obligation.id) is None:
        session.add(_new_message_part(
            session, action, payload=payload, obligation=obligation,
            identity=identity, attempt=attempt, fact=fact,
        ))
    if action.action_type == "group_clone_mutation":
        _settle_lifecycle_side_effect(session, action, payload=payload, attempt=attempt)
        if payload.resume_obligation_after_success:
            obligation.state = "observed"
            obligation.resolved_at = None


def _clone_payload(action):
    if action.action_type == "group_clone_send":
        return GroupCloneSendPayload.model_validate(action.payload or {})
    return GroupCloneMutationPayload.model_validate(action.payload or {})


def _expected_fact_kind(action) -> str:
    if action.action_type == "group_clone_send":
        return "clone_message_observed"
    mutation = str((action.payload or {}).get("mutation_kind") or "")
    return {
        "editMessage": "clone_edit_observed",
        "deleteMessages": "clone_delete_observed",
        "pinMessage": "clone_pin_observed",
        "unpinMessage": "clone_pin_observed",
        "createForumTopic": "clone_topic_observed",
        "editForumTopic": "clone_topic_observed",
        "deleteForumTopic": "clone_topic_observed",
    }[mutation]


def _settle_lifecycle_side_effect(session, action, *, payload, attempt) -> None:
    from app.models.group_clone import CloneTopicMap

    if payload.mutation_kind not in {
        "createForumTopic", "editForumTopic", "deleteForumTopic",
    }:
        return
    topic = session.scalar(select(CloneTopicMap).where(
        CloneTopicMap.task_id == action.task_id,
        CloneTopicMap.epoch == action.task_lifecycle_epoch,
        CloneTopicMap.source_top_message_id == payload.source_message_id,
    ))
    if topic is None:
        raise RuntimeError("group_clone_topic_map_missing")
    if payload.mutation_kind == "deleteForumTopic":
        topic.state = "deleted"
        return
    topic.target_top_message_id = int(attempt.remote_message_id or payload.target_message_ids[0])
    topic.state = "ready"
    topic.revision += 1


def _record_outbound_mapping(session, action, *, payload, identity, attempt) -> None:
    state_id = session.scalar(select(TelegramAuthorizationUpdateState.id).where(
        TelegramAuthorizationUpdateState.tenant_id == action.tenant_id,
        TelegramAuthorizationUpdateState.authorization_id == identity.authorization_id,
        TelegramAuthorizationUpdateState.session_generation == identity.session_generation,
    ))
    if not state_id:
        raise RuntimeError("group_clone_outbound_update_state_missing")
    from .telegram_update_ingress import record_outbound_random_id_mapping

    record_outbound_random_id_mapping(
        session,
        state_id,
        random_id=identity.random_id,
        target_peer_type=payload.target_peer_type,
        target_peer_id=payload.target_peer_id,
        remote_message_or_topic_id=attempt.remote_message_id,
        action_id=action.id,
        execution_attempt_id=attempt.id,
        gateway_mutation_identity_id=identity.id,
    )


def _new_message_part(session, action, *, payload, obligation, identity, attempt, fact):
    execution = session.get(CloneTargetExecutionSnapshot, payload.execution_snapshot_id)
    route = session.get(CloneTargetRouteSnapshot, payload.route_snapshot_id)
    if execution is None or route is None:
        raise RuntimeError("group_clone_settlement_snapshot_missing")
    request_identity = str((attempt.result_snapshot or {}).get("gateway_request_identity") or "")
    if not request_identity:
        raise RuntimeError("group_clone_gateway_request_identity_missing")
    return CloneMessagePart(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        epoch=obligation.epoch,
        obligation_id=obligation.id,
        action_id=action.id,
        attempt_id=attempt.id,
        remote_fact_id=fact.fact_id,
        source_message_id=payload.source_message_id,
        account_id=execution.account_id,
        authorization_id=execution.authorization_id,
        session_generation=execution.session_generation,
        execution_binding_hash=execution.execution_binding_hash,
        target_peer_type=route.target_peer_type,
        target_peer_id=route.target_peer_id,
        target_message_id=int(attempt.remote_message_id),
        target_top_message_id=payload.target_top_message_id,
        gateway_mutation_identity_id=identity.id,
        random_id=identity.random_id,
        gateway_request_identity=request_identity,
        remote_confirmed_at=fact.observed_at,
    )


def _settle_terminal_failure(session, action, *, obligation, identity, attempt) -> None:
    fact_kind = session.scalar(select(FulfillmentRemoteFact.fact_kind).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.attempt_id == attempt.id,
    ).order_by(FulfillmentRemoteFact.observed_at.desc()).limit(1))
    if attempt.gateway_call_started_at and fact_kind != "safely_not_executed":
        action.status = "unknown_after_send"
        obligation.state = "unknown_after_send"
        identity.state = "unknown"
        return
    obligation.state = "failed_terminal"
    identity.state = "allocated"
    _ensure_failed_head_case(session, action, obligation)


def _ensure_failed_head_case(session, action, obligation) -> None:
    existing = session.scalar(select(CloneSequencerHeadCase).where(
        CloneSequencerHeadCase.task_id == action.task_id,
        CloneSequencerHeadCase.epoch == obligation.epoch,
        CloneSequencerHeadCase.sequencer_id == obligation.sequencer_id,
        CloneSequencerHeadCase.case_kind == "failed_terminal",
    ))
    if existing:
        return
    task = session.get(Task, action.task_id)
    policy = ((task.type_config or {}).get("lifecycle", {}) if task else {}).get(
        "failure_order_policy",
        "fail_stop",
    )
    case = CloneSequencerHeadCase(
        task_id=action.task_id,
        epoch=obligation.epoch,
        sequencer_id=obligation.sequencer_id,
        obligation_id=obligation.id,
        case_kind="failed_terminal",
        failure_evidence=dict(action.result or {}),
        remote_mutation_started=False,
        policy_snapshot=policy,
        state="visible_gap_accepted" if policy == "continue_with_visible_gap" else "waiting_decision",
        decision_reason="policy_continue_with_visible_gap" if policy == "continue_with_visible_gap" else None,
    )
    session.add(case)
    session.flush()
    obligation.sequencer_head_case_id = case.id


def _latest_attempt(session, action):
    return session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))


def _message_part(session, obligation_id):
    return session.scalar(select(CloneMessagePart).where(
        CloneMessagePart.obligation_id == obligation_id,
        CloneMessagePart.part_index == 0,
    ))


__all__ = ["project_group_clone_result"]
