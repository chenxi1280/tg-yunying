from __future__ import annotations

from app.models import AccountStatus, TgAuthorizationDrOperation, TgLoginFlow
from app.services._common import _now


def deferred_manual_blocker(session, operations: dict) -> str:
    operation = operations.get("b")
    if not _waiting_two_fa_b(session, operation, operations):
        return ""
    return "two_fa_required"


def deferred_reconcile_blocker(operation) -> str:
    if not operation:
        return "remote_authorization_unproven"
    if operation.remote_call_state == "unknown" and operation.remote_effect_started_at:
        return "remote_authorization_unproven"
    return operation.blocker_code or "remote_authorization_unproven"


def mark_deferred_operation_manual(session, operation_id: str, blocker: str) -> None:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation:
        return
    operation.status = "manual_required"
    operation.remote_call_state = "reconciled_hold"
    operation.blocker_code = blocker
    operation.reconcile_status = "quarantined"
    operation.finished_at = _now()
    operation.operation_version += 1


def _waiting_two_fa_b(session, operation, operations: dict) -> bool:
    flow = session.get(TgLoginFlow, operation.login_flow_id) if operation and operation.login_flow_id else None
    return bool(
        operation
        and operation.operation_type == "provision_standby_1"
        and operation.status == "deferred_reconcile"
        and operation.remote_call_state == "unknown"
        and operation.reconcile_status == "quarantined"
        and operation.remote_effect_started_at
        and operation.login_flow_id
        and operation.login_challenge_sent_at
        and operation.login_code_message_id
        and operation.login_code_received_at
        and not operation.candidate_authorization_id
        and operations.get("c") is None
        and operations.get("e4") is None
        and flow
        and flow.status == AccountStatus.WAITING_2FA.value
        and flow.temporary_session_ciphertext
        and flow.phone_code_hash_ciphertext
        and flow.authorization_id is None
        and flow.superseded_by_flow_id is None
    )


__all__ = [
    "deferred_manual_blocker",
    "deferred_reconcile_blocker",
    "mark_deferred_operation_manual",
]
