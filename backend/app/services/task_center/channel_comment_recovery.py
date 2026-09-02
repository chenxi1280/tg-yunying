from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentRecoveryManifest,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskCommentCapacityReservation,
)
from app.services._common import audit

from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_discussion_contracts import current_group_binding
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .source_pacing_release import release_source_pacing_admissions_before_gateway


RETIRE_PRE_GATEWAY = "retire_pre_gateway_future_materialization"
CLOSE_NO_EFFECT_UNKNOWN = "close_authoritative_no_effect_unknown"
RECOVERY_KINDS = frozenset({RETIRE_PRE_GATEWAY, CLOSE_NO_EFFECT_UNKNOWN})


@dataclass(frozen=True)
class RecoveryPreviewRequest:
    task_id: str
    expected_deployed_sha: str
    recovery_kind: str
    operator_id: str
    approval_reference: str
    previewed_at: datetime
    expires_at: datetime
    exact_action_ids: tuple[str, ...] = ()
    authoritative_no_effect_evidence: dict[str, str] | None = None


@dataclass(frozen=True)
class RecoveryApplyRequest:
    manifest_id: str
    expected_preview_hash: str
    current_deployed_sha: str
    operator_id: str
    approval_reference: str
    applied_at: datetime


def preview_channel_comment_recovery(
    session: Session,
    request: RecoveryPreviewRequest,
) -> ChannelCommentRecoveryManifest:
    _validate_preview_request(request)
    task = session.get(Task, request.task_id)
    if task is None or task.type != "channel_comment":
        raise ValueError("channel_comment_recovery_task_not_found")
    snapshot = _state_snapshot(session, task)
    evidence = dict(request.authoritative_no_effect_evidence or {})
    eligible = _eligible_action_ids(snapshot, request.recovery_kind, evidence=evidence)
    exact_ids = tuple(sorted(request.exact_action_ids or tuple(eligible)))
    if not exact_ids or not set(exact_ids).issubset(eligible):
        raise ValueError("channel_comment_recovery_exact_action_scope_invalid")
    identity = _manifest_identity(request, snapshot, exact_ids=exact_ids, evidence=evidence)
    manifest = ChannelCommentRecoveryManifest(
        tenant_id=task.tenant_id,
        task_id=task.id,
        recovery_kind=request.recovery_kind,
        expected_deployed_sha=request.expected_deployed_sha,
        expected_task_status=task.status,
        expected_task_config_revision=task.config_revision,
        expected_task_lifecycle_epoch=task.task_lifecycle_epoch,
        expected_target_reference_revision=int(snapshot["target"]["reference_revision"] or 0),
        expected_binding_id=snapshot["binding"].get("id"),
        expected_binding_revision=snapshot["binding"].get("revision"),
        action_set_hash=_stable_hash(snapshot["actions"]),
        exact_action_ids_json=list(exact_ids),
        recovery_evidence_json=evidence,
        state_snapshot_json=snapshot,
        preview_hash=_stable_hash(identity),
        manifest_state="previewed",
        operator_id=request.operator_id,
        approval_reference=request.approval_reference,
        previewed_at=request.previewed_at,
        expires_at=request.expires_at,
    )
    session.add(manifest)
    session.flush()
    return manifest


def apply_channel_comment_recovery(
    session: Session,
    request: RecoveryApplyRequest,
) -> ChannelCommentRecoveryManifest:
    manifest = session.scalar(select(ChannelCommentRecoveryManifest).where(
        ChannelCommentRecoveryManifest.id == request.manifest_id,
    ).with_for_update())
    _validate_apply_request(manifest, request)
    task = session.scalar(select(Task).where(Task.id == manifest.task_id).with_for_update())
    if task is None:
        raise ValueError("channel_comment_recovery_task_not_found")
    snapshot = _state_snapshot(session, task)
    _require_snapshot_match(manifest, task, snapshot)
    actions = _locked_exact_actions(session, manifest)
    if manifest.recovery_kind == RETIRE_PRE_GATEWAY:
        _retire_pre_gateway_actions(session, actions)
    elif manifest.recovery_kind == CLOSE_NO_EFFECT_UNKNOWN:
        _close_authoritative_no_effect(session, actions, manifest.recovery_evidence_json)
    else:
        raise ValueError("channel_comment_recovery_kind_invalid")
    manifest.manifest_state = "applied"
    manifest.applied_at = request.applied_at
    session.flush()
    _record_readback(session, manifest, task)
    audit(
        session,
        tenant_id=task.tenant_id,
        actor=request.operator_id,
        action="频道评论存量恢复 apply",
        target_type="channel_comment_recovery_manifest",
        target_id=manifest.id,
        detail=f"kind={manifest.recovery_kind};preview_hash={manifest.preview_hash}",
    )
    return manifest


def readback_channel_comment_recovery(session: Session, manifest_id: str) -> dict:
    manifest = session.get(ChannelCommentRecoveryManifest, manifest_id)
    if manifest is None or manifest.manifest_state != "applied":
        raise ValueError("channel_comment_recovery_manifest_not_applied")
    task = session.get(Task, manifest.task_id)
    if task is None:
        raise ValueError("channel_comment_recovery_task_not_found")
    payload = _readback_payload(session, manifest, task)
    readback_hash = _stable_hash(payload)
    if readback_hash != manifest.readback_hash:
        raise RuntimeError("channel_comment_recovery_independent_readback_mismatch")
    return {
        "manifest_id": manifest.id,
        "preview_hash": manifest.preview_hash,
        "readback_hash": readback_hash,
        "task_id": task.id,
        "recovery_kind": manifest.recovery_kind,
        "exact_action_count": len(manifest.exact_action_ids_json or []),
    }


def _validate_preview_request(request: RecoveryPreviewRequest) -> None:
    if request.recovery_kind not in RECOVERY_KINDS:
        raise ValueError("channel_comment_recovery_kind_invalid")
    if not request.expected_deployed_sha or not request.operator_id or not request.approval_reference:
        raise ValueError("channel_comment_recovery_authority_incomplete")
    if _wall(request.expires_at) <= _wall(request.previewed_at):
        raise ValueError("channel_comment_recovery_expiry_invalid")


def _validate_apply_request(manifest, request: RecoveryApplyRequest) -> None:
    if manifest is None or manifest.manifest_state != "previewed":
        raise ValueError("channel_comment_recovery_manifest_not_previewed")
    if manifest.preview_hash != request.expected_preview_hash:
        raise ValueError("channel_comment_recovery_preview_hash_drift")
    if manifest.expected_deployed_sha != request.current_deployed_sha:
        raise ValueError("channel_comment_recovery_deployed_sha_drift")
    if manifest.operator_id != request.operator_id or manifest.approval_reference != request.approval_reference:
        raise ValueError("channel_comment_recovery_approval_drift")
    if _wall(manifest.expires_at) < _wall(request.applied_at):
        raise ValueError("channel_comment_recovery_manifest_expired")


def _state_snapshot(session: Session, task: Task) -> dict:
    actions = list(session.scalars(select(Action).where(
        Action.task_id == task.id,
    ).order_by(Action.id)))
    action_ids = [action.id for action in actions]
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
    ).order_by(ExecutionAttempt.id))) if action_ids else []
    obligations = list(session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.task_id == task.id,
    ).order_by(CommentFulfillmentObligation.id)))
    target_id = int((task.type_config or {}).get("target_channel_id") or 0)
    target = session.get(OperationTarget, target_id) if target_id else None
    binding = current_group_binding(session, task.tenant_id, target_id) if target_id else None
    return {
        "task": _task_snapshot(task),
        "target": _target_snapshot(target),
        "binding": _binding_snapshot(binding),
        "actions": [_action_snapshot(action, attempts) for action in actions],
        "attempts": [_attempt_snapshot(attempt) for attempt in attempts],
        "obligations": [_obligation_snapshot(item) for item in obligations],
    }


def _task_snapshot(task: Task) -> dict:
    return {
        "id": task.id,
        "status": task.status,
        "config_revision": task.config_revision,
        "lifecycle_epoch": task.task_lifecycle_epoch,
    }


def _target_snapshot(target) -> dict:
    if target is None:
        return {"id": None, "reference_revision": 0}
    return {"id": target.id, "reference_revision": target.reference_revision, "peer": target.tg_peer_id}


def _binding_snapshot(binding) -> dict:
    if binding is None:
        return {"id": None, "revision": None, "identity_hash": ""}
    return {"id": binding.id, "revision": binding.binding_revision, "identity_hash": binding.identity_hash}


def _action_snapshot(action: Action, attempts: list[ExecutionAttempt]) -> dict:
    own_attempts = [item for item in attempts if item.action_id == action.id]
    result = dict(action.result or {})
    return {
        "id": action.id,
        "type": action.action_type,
        "status": action.status,
        "claim_open": bool(action.claim_owner or action.claim_token or action.lease_owner),
        "attempt_count": len(own_attempts),
        "gateway_started": any(item.gateway_call_started_at for item in own_attempts),
        "typed_fact_hash": _typed_fact_hash(result),
        "payload_hash": _stable_hash(action.payload or {}),
    }


def _attempt_snapshot(attempt: ExecutionAttempt) -> dict:
    return {
        "id": attempt.id,
        "action_id": attempt.action_id,
        "status": attempt.status,
        "gateway_started": bool(attempt.gateway_call_started_at),
        "remote_message_id": attempt.remote_message_id or "",
        "failure_type": attempt.failure_type or "",
    }


def _obligation_snapshot(item: CommentFulfillmentObligation) -> dict:
    return {
        "id": item.id,
        "status": item.status,
        "current_action_id": item.current_action_id,
        "remote_comment_id": item.remote_comment_id,
    }


def _eligible_action_ids(snapshot: dict, kind: str, *, evidence: dict) -> set[str]:
    if snapshot["task"]["status"] != "paused":
        raise ValueError("channel_comment_recovery_task_must_remain_paused")
    if kind == RETIRE_PRE_GATEWAY:
        return {
            row["id"] for row in snapshot["actions"]
            if row["type"] == "post_comment"
            and row["status"] in {"pending", "retryable_failed"}
            and not row["claim_open"] and not row["attempt_count"] and not row["typed_fact_hash"]
        }
    return {
        row["id"] for row in snapshot["actions"]
        if row["type"] == "post_comment" and row["status"] == "unknown_after_send"
        and row["id"] in evidence and bool(str(evidence[row["id"]]).strip())
    }


def _manifest_identity(request, snapshot: dict, *, exact_ids: tuple[str, ...], evidence: dict) -> dict:
    return {
        "task_id": request.task_id,
        "sha": request.expected_deployed_sha,
        "kind": request.recovery_kind,
        "snapshot_hash": _stable_hash(snapshot),
        "exact_action_ids": exact_ids,
        "evidence_hash": _stable_hash(evidence),
        "operator": request.operator_id,
        "approval": request.approval_reference,
        "previewed_at": request.previewed_at.isoformat(),
        "expires_at": request.expires_at.isoformat(),
    }


def _require_snapshot_match(manifest, task: Task, snapshot: dict) -> None:
    expected = (
        manifest.expected_task_status,
        manifest.expected_task_config_revision,
        manifest.expected_task_lifecycle_epoch,
        manifest.expected_target_reference_revision,
        manifest.expected_binding_id,
        manifest.expected_binding_revision,
        manifest.action_set_hash,
        _stable_hash(manifest.state_snapshot_json),
    )
    actual = (
        task.status,
        task.config_revision,
        task.task_lifecycle_epoch,
        int(snapshot["target"]["reference_revision"] or 0),
        snapshot["binding"].get("id"),
        snapshot["binding"].get("revision"),
        _stable_hash(snapshot["actions"]),
        _stable_hash(snapshot),
    )
    if expected != actual:
        raise ValueError("channel_comment_recovery_snapshot_drift")


def _locked_exact_actions(session: Session, manifest) -> list[Action]:
    action_ids = list(manifest.exact_action_ids_json or [])
    actions = list(session.scalars(select(Action).where(
        Action.task_id == manifest.task_id,
        Action.id.in_(action_ids),
    ).order_by(Action.id).with_for_update()))
    if [action.id for action in actions] != sorted(action_ids):
        raise ValueError("channel_comment_recovery_exact_action_set_drift")
    return actions


def _retire_pre_gateway_actions(session: Session, actions: list[Action]) -> None:
    for action in actions:
        current = _action_snapshot(action, [])
        if current["status"] not in {"pending", "retryable_failed"} or current["claim_open"]:
            raise ValueError("channel_comment_recovery_action_drift")
        if session.scalar(select(ExecutionAttempt.id).where(ExecutionAttempt.action_id == action.id).limit(1)):
            raise ValueError("channel_comment_recovery_attempt_drift")
        payload = PostCommentPayload.model_validate(action.payload)
        invalidate_comment_generation_jobs(session, action, payload, reason=RETIRE_PRE_GATEWAY)
        release_action_pacing_reservation_before_gateway(session, action)
        release_source_pacing_admissions_before_gateway(session, action)
        _retire_action_obligation(session, action)
        action.status = "skipped"
        action.result = {**dict(action.result or {}), "error_code": RETIRE_PRE_GATEWAY}


def _retire_action_obligation(session: Session, action: Action) -> None:
    obligation = session.scalar(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.current_action_id == action.id,
    ).with_for_update())
    if obligation is None:
        return
    obligation.current_action_id = None
    obligation.status = "terminated"
    reservation = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation.id,
    ))
    if reservation and reservation.reservation_state in {"plan_reserved", "action_reserved"}:
        reservation.reservation_state = "released"


def _close_authoritative_no_effect(session: Session, actions: list[Action], evidence: dict) -> None:
    for action in actions:
        if action.status != "unknown_after_send" or not str(evidence.get(action.id) or "").strip():
            raise ValueError("channel_comment_recovery_no_effect_evidence_missing")
        action.status = "failed"
        action.result = {
            **dict(action.result or {}),
            "error_code": "authoritative_remote_no_effect",
            "authoritative_no_effect_evidence_ref": evidence[action.id],
        }
        obligation = session.scalar(select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.current_action_id == action.id,
        ).with_for_update())
        if obligation is not None:
            obligation.status = "closed_no_effect"
            _release_proven_no_effect_hold(session, obligation.id)


def _release_proven_no_effect_hold(session: Session, obligation_id: str) -> None:
    reservation = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation_id,
    ))
    if reservation is not None and reservation.reservation_state != "confirmed":
        reservation.reservation_state = "released"


def _record_readback(session: Session, manifest, task: Task) -> None:
    manifest.readback_hash = _stable_hash(_readback_payload(session, manifest, task))


def _readback_payload(session: Session, manifest, task: Task) -> dict:
    exact_ids = set(manifest.exact_action_ids_json or [])
    snapshot = _state_snapshot(session, task)
    actions = {row["id"]: row for row in snapshot["actions"] if row["id"] in exact_ids}
    expected_status = "skipped" if manifest.recovery_kind == RETIRE_PRE_GATEWAY else "failed"
    if set(actions) != exact_ids or any(row["status"] != expected_status for row in actions.values()):
        raise RuntimeError("channel_comment_recovery_readback_failed")
    return {
        "preview_hash": manifest.preview_hash,
        "task": snapshot["task"],
        "actions": actions,
        "obligations": snapshot["obligations"],
        "attempts": snapshot["attempts"],
    }


def _typed_fact_hash(result: dict) -> str:
    fact = result.get("channel_comment_remote_fact") or result.get("discussion_membership_remote_fact")
    return _stable_hash(fact) if fact else ""


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "CLOSE_NO_EFFECT_UNKNOWN",
    "RETIRE_PRE_GATEWAY",
    "RecoveryApplyRequest",
    "RecoveryPreviewRequest",
    "apply_channel_comment_recovery",
    "preview_channel_comment_recovery",
    "readback_channel_comment_recovery",
]
