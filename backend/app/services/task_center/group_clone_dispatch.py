from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Action,
    ExecutionAttempt,
    Task,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
    TgGroupAccount,
)
from app.models.telegram_authorities import TelegramAuthorizationTransportState
from app.services._common import _now
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
    TelegramGatewayMutationIdentity,
)

from .group_mutation_authority import verify_gateway_admission
from .payloads import GroupCloneMutationPayload, GroupCloneSendPayload

TERMINAL_SEQUENCE_STATES = (
    "succeeded",
    "degraded",
    "filtered",
    "cancelled",
    "superseded",
)


@dataclass(frozen=True)
class CloneDispatchContract:
    task: Task
    obligation: CloneDeliveryObligation
    identity: TelegramGatewayMutationIdentity
    route: CloneTargetRouteSnapshot
    execution: CloneTargetExecutionSnapshot
    authorization: TgAccountAuthorization


def validate_clone_dispatch(
    session: Session,
    action: Action,
    *,
    account: TgAccount,
    payload: GroupCloneSendPayload | GroupCloneMutationPayload,
) -> CloneDispatchContract:
    rows = _load_contract_rows(session, payload)
    _validate_task_action(action, account, payload=payload, rows=rows)
    _validate_route_execution(payload, rows)
    _validate_identity(payload, rows)
    _validate_authorization(session, account, rows)
    _validate_transport(session, action, payload=payload, rows=rows)
    _validate_sequence_head(session, action, rows)
    allowed, reason = verify_gateway_admission(
        session,
        action.tenant_id,
        target_peer_type=payload.target_peer_type,
        target_peer_id=payload.target_peer_id,
        writer_kind="group_clone",
        writer_id=action.task_id,
    )
    if not allowed:
        raise ValueError(f"group_clone_gateway_authority_blocked: {reason}")
    return rows


def _load_contract_rows(session, payload) -> CloneDispatchContract:
    obligation = session.get(CloneDeliveryObligation, payload.obligation_id)
    identity = session.get(TelegramGatewayMutationIdentity, payload.gateway_mutation_identity_id)
    route = session.get(CloneTargetRouteSnapshot, payload.route_snapshot_id)
    execution = session.get(CloneTargetExecutionSnapshot, payload.execution_snapshot_id)
    authorization = session.get(TgAccountAuthorization, execution.authorization_id) if execution else None
    task = session.get(Task, obligation.task_id) if obligation else None
    if not all((task, obligation, identity, route, execution, authorization)):
        raise ValueError("group_clone_frozen_contract_missing")
    return CloneDispatchContract(task, obligation, identity, route, execution, authorization)


def _validate_task_action(action, account, *, payload, rows) -> None:
    task = rows.task
    if task.type != "group_clone" or task.status != "running":
        raise ValueError("group_clone_task_not_running")
    if int(task.task_lifecycle_epoch or 1) != int(rows.obligation.epoch):
        raise ValueError("group_clone_task_epoch_mismatch")
    if action.task_id != task.id or action.tenant_id != task.tenant_id:
        raise ValueError("group_clone_action_task_scope_mismatch")
    if action.account_id != account.id or action.obligation_id != payload.obligation_id:
        raise ValueError("group_clone_action_identity_mismatch")


def _validate_route_execution(payload, rows) -> None:
    route = rows.route
    execution = rows.execution
    expected_role = getattr(payload, "execution_role", "sender")
    if route.id != execution.route_snapshot_id or execution.execution_role != expected_role:
        raise ValueError("group_clone_execution_snapshot_mismatch")
    if (route.target_peer_type, route.target_peer_id) != (
        payload.target_peer_type,
        payload.target_peer_id,
    ):
        raise ValueError("group_clone_target_snapshot_mismatch")
    if rows.obligation.route_binding_snapshot_id != route.id:
        raise ValueError("group_clone_obligation_route_mismatch")
    if rows.obligation.execution_target_binding_snapshot_id != execution.id:
        raise ValueError("group_clone_obligation_execution_mismatch")


def _validate_identity(payload, rows) -> None:
    identity = rows.identity
    expected_kind = getattr(payload, "mutation_kind", "sendMessage")
    if identity.obligation_id != rows.obligation.id or identity.mutation_kind != expected_kind:
        raise ValueError("group_clone_mutation_identity_scope_mismatch")
    if identity.random_id != payload.random_id:
        raise ValueError("group_clone_random_id_mismatch")
    if expected_kind in {"sendMessage", "createForumTopic"} and not identity.random_id:
        raise ValueError("group_clone_random_id_missing")
    if (identity.target_peer_type, identity.target_peer_id) != (
        payload.target_peer_type,
        payload.target_peer_id,
    ):
        raise ValueError("group_clone_mutation_target_mismatch")
    if identity.state not in {"allocated", "attempt_bound"}:
        raise ValueError(f"group_clone_mutation_identity_not_callable:{identity.state}")


def _validate_authorization(session, account, rows) -> None:
    authorization = rows.authorization
    execution = rows.execution
    if execution.account_id != account.id or authorization.account_id != account.id:
        raise ValueError("group_clone_execution_account_mismatch")
    if account.status != AccountStatus.ACTIVE.value or account.deleted_at is not None:
        raise ValueError("group_clone_account_not_online")
    if not authorization.is_current or authorization.status != "active":
        raise ValueError("group_clone_authorization_not_current")
    if authorization.slot_generation != execution.session_generation:
        raise ValueError("group_clone_session_generation_mismatch")
    if not authorization.telegram_user_id_digest:
        raise ValueError("canonical_telegram_account_peer_id_unproven")
    _validate_target_membership(
        session, account, route=rows.route, execution_role=execution.execution_role,
    )


def _validate_target_membership(session, account, *, route, execution_role) -> None:
    group = session.get(TgGroup, route.target_internal_group_id)
    if group is None or group.tenant_id != route.tenant_id:
        raise ValueError("group_clone_target_group_missing")
    if str(group.tg_peer_id) != route.target_peer_id:
        raise ValueError("group_clone_target_canonical_peer_drift")
    link = session.scalar(select(TgGroupAccount).where(
        TgGroupAccount.tenant_id == route.tenant_id,
        TgGroupAccount.group_id == group.id,
        TgGroupAccount.account_id == account.id,
        TgGroupAccount.can_send.is_(True),
    ))
    if link is None:
        raise ValueError("group_clone_target_membership_or_send_permission_missing")
    if execution_role == "target_control" and str(link.permission_label).lower() not in {
        "管理员", "群主", "admin", "administrator", "owner",
    }:
        raise ValueError("group_clone_target_control_permission_missing")


def _validate_transport(session, action, *, payload, rows) -> None:
    target_key = f"{payload.target_peer_type}:{payload.target_peer_id}"
    blocked = session.scalar(select(TelegramAuthorizationTransportState.id).where(
        TelegramAuthorizationTransportState.tenant_id == action.tenant_id,
        TelegramAuthorizationTransportState.authorization_id == rows.authorization.id,
        TelegramAuthorizationTransportState.session_generation == rows.execution.session_generation,
        TelegramAuthorizationTransportState.blocked_until > _now(),
        (
            (TelegramAuthorizationTransportState.scope_type == "global")
            | (
                (TelegramAuthorizationTransportState.scope_type == "target_slowmode")
                & (TelegramAuthorizationTransportState.target_peer_key == target_key)
            )
        ),
    ).limit(1))
    if blocked:
        raise ValueError("group_clone_authorization_transport_blocked")


def _validate_sequence_head(session, action, rows) -> None:
    previous = list(session.scalars(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == rows.task.id,
        CloneDeliveryObligation.epoch == rows.obligation.epoch,
        CloneDeliveryObligation.sequencer_id < rows.obligation.sequencer_id,
        CloneDeliveryObligation.state.not_in(TERMINAL_SEQUENCE_STATES),
    ).order_by(CloneDeliveryObligation.sequencer_id)))
    if any(not _visible_gap_accepted(session, item) for item in previous):
        raise ValueError("group_clone_sequencer_head_blocked")
    started = session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
        ExecutionAttempt.status.in_(("gateway_call_started", "result_unknown", "success")),
    ).limit(1))
    if started:
        raise ValueError("group_clone_mutation_already_started")


def _visible_gap_accepted(session, obligation) -> bool:
    if obligation.state not in {"failed_terminal", "remote_reconcile_only"}:
        return False
    from app.models.group_clone import CloneSequencerHeadCase

    state = session.scalar(select(CloneSequencerHeadCase.state).where(
        CloneSequencerHeadCase.obligation_id == obligation.id,
        CloneSequencerHeadCase.case_kind.in_(("failed_terminal", "unknown_deadline_closed")),
    ))
    return state == "visible_gap_accepted"


__all__ = ["CloneDispatchContract", "validate_clone_dispatch"]
