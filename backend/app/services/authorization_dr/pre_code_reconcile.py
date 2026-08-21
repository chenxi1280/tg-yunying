from __future__ import annotations

import hashlib
import json

from app.models import TgAuthorizationDrOperation, TgLoginFlow

from .contracts import AuthorizationDrError


PRE_CODE_FAILURE_KIND = "pre_code_submission_failure"
PRE_CODE_FAILURE_SIGNATURE = "password_2fa_preceded_code_v1"


def build_pre_code_failure_evidence(
    session,
    operation_id: str,
    *,
    tenant_id: int,
    event_digest: str,
    source_ref: str,
    runtime_image_sha: str,
) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    flow = _failure_flow(session, operation)
    return {
        "kind": PRE_CODE_FAILURE_KIND,
        "blocker_code": operation.blocker_code,
        "defect_signature": PRE_CODE_FAILURE_SIGNATURE,
        "event_digest": event_digest,
        "flow_state_digest": _flow_state_digest(operation, flow),
        "login_flow_id": flow.id,
        "node_id": operation.owner_node_id,
        "owner_epoch": operation.owner_epoch,
        "runtime_image_sha": runtime_image_sha,
        "source_ref": source_ref,
    }


def validate_pre_code_failure(session, operation, evidence: dict) -> dict:
    required = {
        "kind", "blocker_code", "defect_signature", "event_digest", "flow_state_digest",
        "login_flow_id", "node_id", "owner_epoch", "runtime_image_sha", "source_ref",
    }
    if set(evidence) != required:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Reconcile evidence fields are invalid")
    _require_common_evidence(operation, evidence)
    flow = _failure_flow(session, operation)
    valid = (
        evidence["kind"] == PRE_CODE_FAILURE_KIND
        and evidence["blocker_code"] == "AuthKeyUnregisteredError"
        and evidence["blocker_code"] == operation.blocker_code
        and evidence["defect_signature"] == PRE_CODE_FAILURE_SIGNATURE
        and int(evidence["login_flow_id"]) == flow.id
        and evidence["flow_state_digest"] == _flow_state_digest(operation, flow)
    )
    if not valid:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Pre-code failure evidence changed")
    return {key: evidence[key] for key in sorted(required)}


def close_pre_code_flow(session, operation) -> None:
    flow = session.get(TgLoginFlow, operation.login_flow_id)
    if not flow:
        raise AuthorizationDrError("reconcile_frozen_fact_conflict", "Login flow disappeared")
    flow.status = "异常"
    flow.failure_type = PRE_CODE_FAILURE_KIND
    flow.failure_detail = "Gateway attempted 2FA before submitting the bound login code"
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    flow.code_preview = None
    flow.flow_version += 1


def _failure_flow(session, operation):
    flow = session.get(TgLoginFlow, operation.login_flow_id)
    valid = (
        operation.operation_type == "provision_standby_1"
        and operation.candidate_authorization_id is None
        and bool(operation.login_code_message_id)
        and operation.login_code_received_at is not None
        and flow is not None
        and flow.status == "等待验证码"
        and bool(flow.temporary_session_ciphertext)
        and bool(flow.phone_code_hash_ciphertext)
    )
    if not valid:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Pre-code failure flow state changed")
    return flow


def _flow_state_digest(operation, flow) -> str:
    payload = [
        operation.id, operation.operation_version, operation.blocker_code,
        operation.login_code_message_id, str(operation.login_code_received_at),
        flow.id, flow.flow_version, flow.status, bool(flow.temporary_session_ciphertext),
        bool(flow.phone_code_hash_ciphertext), str(flow.challenge_sent_at),
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _require_common_evidence(operation, evidence: dict) -> None:
    identity_matches = (
        evidence["node_id"] == operation.owner_node_id
        and evidence["owner_epoch"] == operation.owner_epoch
    )
    source_ref_valid = 0 < len(str(evidence["source_ref"])) <= 160
    digests_valid = _is_lower_hex(str(evidence["event_digest"]), (64,)) and _is_lower_hex(
        str(evidence["runtime_image_sha"]), (40, 64)
    )
    if not identity_matches or not source_ref_valid:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Evidence does not match operation owner facts")
    if not digests_valid:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Evidence digest or runtime SHA is invalid")


def _is_lower_hex(value: str, lengths: tuple[int, ...]) -> bool:
    return len(value) in lengths and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "PRE_CODE_FAILURE_KIND",
    "build_pre_code_failure_evidence",
    "close_pre_code_flow",
    "validate_pre_code_failure",
]
