from __future__ import annotations

from sqlalchemy import select

from app.models import TgLoginFlow

from .contracts import AuthorizationDrError


def interrupted_login_flows(session, operation) -> tuple[TgLoginFlow, ...]:
    if operation.remote_effect_started_at is None:
        return ()
    rows = session.scalars(select(TgLoginFlow).where(
        TgLoginFlow.tenant_id == operation.tenant_id,
        TgLoginFlow.account_id == operation.account_id,
        TgLoginFlow.authorization_role == "standby_1",
        TgLoginFlow.developer_app_id == operation.developer_app_id,
        TgLoginFlow.created_at >= operation.remote_effect_started_at,
    ).order_by(TgLoginFlow.created_at, TgLoginFlow.id))
    return tuple(rows)


def require_empty_interrupted_intent(operation, flows: tuple[TgLoginFlow, ...]) -> None:
    if not flows:
        return
    if len(flows) != 1 or not _empty_intent(operation, flows[0]):
        raise AuthorizationDrError(
            "online_abc_release_interrupted_state_invalid",
            "Interrupted B login flow has a downstream or ambiguous effect",
        )


def flow_snapshot(flow: TgLoginFlow | None) -> dict | None:
    if flow is None:
        return None
    return {
        "id": flow.id,
        "method": flow.method,
        "status": flow.status,
        "flow_version": flow.flow_version,
        "created_at": str(flow.created_at),
        "authorization_role": flow.authorization_role,
        "developer_app_id": flow.developer_app_id,
        "proxy_id": flow.proxy_id,
        "authorization_id": flow.authorization_id,
        "superseded_by_flow_id": flow.superseded_by_flow_id,
        "batch_login_attempt_id": flow.batch_login_attempt_id,
        "batch_login_generation": flow.batch_login_generation,
        "challenge_sent": bool(flow.challenge_sent_at),
        "code_expiry_present": bool(flow.code_expires_at),
        "temporary_session_present": bool(flow.temporary_session_ciphertext),
        "phone_code_hash_present": bool(flow.phone_code_hash_ciphertext),
        "code_preview_present": bool(flow.code_preview),
        "qr_payload_present": bool(flow.qr_payload),
        "failure_type": flow.failure_type,
        "failure_detail_present": bool(flow.failure_detail),
        "remote_error_type": flow.remote_error_type,
        "trace_id_present": bool(flow.trace_id),
    }


def close_empty_interrupted_intent(
    operation, flow: TgLoginFlow | None, *, blocker_code: str, interruption_ref: str,
) -> None:
    if flow is None:
        return
    operation.login_flow_id = flow.id
    flow.status = "superseded"
    flow.flow_version += 1
    flow.failure_type = blocker_code
    flow.failure_detail = f"interruption_ref={interruption_ref}"


def _empty_intent(operation, flow: TgLoginFlow) -> bool:
    return all((
        flow.tenant_id == operation.tenant_id,
        flow.account_id == operation.account_id,
        flow.method == "code",
        flow.status == "intent_persisted",
        flow.flow_version == 1,
        flow.authorization_role == "standby_1",
        flow.developer_app_id == operation.developer_app_id,
        flow.proxy_id is None,
        flow.authorization_id is None,
        flow.superseded_by_flow_id is None,
        flow.batch_login_attempt_id is None,
        flow.batch_login_generation == 0,
        flow.challenge_sent_at is None,
        flow.code_expires_at is None,
        flow.temporary_session_ciphertext is None,
        flow.phone_code_hash_ciphertext is None,
        flow.code_preview is None,
        flow.qr_payload is None,
        not flow.failure_type,
        not flow.failure_detail,
        not flow.remote_error_type,
        not flow.trace_id,
    ))


__all__ = [
    "close_empty_interrupted_intent",
    "flow_snapshot",
    "interrupted_login_flows",
    "require_empty_interrupted_intent",
]
