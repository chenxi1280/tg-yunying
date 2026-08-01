from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping

from app.models import Action, ExecutionAttempt


ACTION_SCOPE_PAYLOAD_KEYS = (
    "group_id",
    "chat_id",
    "chat_mode",
    "reply_to_message_id",
    "context_message_ids",
    "anchor_message_ids",
    "context_snapshot_message_id",
    "ai_message_memory_id",
    "content_scope_contract_version",
    "content_scope_tenant_id",
    "content_scope_group_id",
    "content_scope_task_id",
    "coverage_ledger_id",
    "search_click_obligation_id",
    "search_click_assignment_id",
    "gateway_request_id",
    "gateway_request_identity",
)
ACTION_RESULT_KEYS = (
    "dispatch_claim_active",
    "dispatch_claim_scope",
    "dispatch_claim_window_id",
    "dispatch_claim_shard_allocation_id",
    "dispatch_reservation_id",
    "gateway_call_state",
    "gateway_call_started_at",
    "gateway_request_id",
    "gateway_request_identity",
    "gateway_request_fingerprint",
    "gateway_target_fingerprint",
    "remote_message_id",
    "remote_fact_id",
    "telegram_msg_id",
    "error_code",
)
BODY_PAYLOAD_KEYS = (
    "message_text",
    "content",
    "caption",
    "media_id",
    "media_path",
    "media_url",
    "media_segments",
    "source_media_asset_ids",
    "planned_material_kind",
)
HASH_SCHEMA_VERSION = "runtime_state_hash_v1"


def action_state_hash(action: Action) -> str:
    return _action_state_hash(action, ignored_recovery_claim_token="")


def remote_reconcile_action_state_hash(action: Action) -> str:
    return _action_state_hash(action, ignored_recovery_claim_token="")


def action_state_hash_without_recovery_claim(
    action: Action,
    *,
    claim_token: str,
) -> str:
    if not claim_token:
        raise ValueError("recovery_claim_token_required")
    return _action_state_hash(
        action,
        ignored_recovery_claim_token=claim_token,
    )


def _action_state_hash(
    action: Action,
    *,
    ignored_recovery_claim_token: str,
) -> str:
    payload = _mapping(action.payload)
    result = _mapping(action.result)
    claim_owner, claim_token, claim_expires_at = _claim_state(
        action,
        ignored_recovery_claim_token=ignored_recovery_claim_token,
    )
    snapshot = {
        "schema_version": HASH_SCHEMA_VERSION,
        "identity": {
            "id": action.id,
            "tenant_id": action.tenant_id,
            "task_id": action.task_id,
            "task_type": action.task_type,
            "action_type": action.action_type,
        },
        "state": {
            "status": action.status,
            "account_id": action.account_id,
            "scheduled_at": _utc_text(action.scheduled_at),
            "executed_at": _utc_text(action.executed_at),
            "claim_owner": claim_owner,
            "claim_token": claim_token,
            "claim_expires_at": _utc_text(claim_expires_at),
            "lease_owner": action.lease_owner,
            "lease_expires_at": _utc_text(action.lease_expires_at),
            "primary_quantity_slot_id": action.primary_quantity_slot_id,
            "content_mix_cycle_slot_id": action.content_mix_cycle_slot_id,
            "content_mix_slot_attempt": action.content_mix_slot_attempt,
            "retry_count": action.retry_count,
        },
        "classification_payload": _selected(payload, ACTION_SCOPE_PAYLOAD_KEYS),
        "payload_fingerprint": canonical_state_hash(payload),
        "result_contract": _selected(result, ACTION_RESULT_KEYS),
        "body_fingerprint": _body_fingerprint(payload),
    }
    return canonical_state_hash(snapshot)


def _claim_state(
    action: Action,
    *,
    ignored_recovery_claim_token: str,
) -> tuple[str, str, datetime | None]:
    if (
        ignored_recovery_claim_token
        and action.claim_token == ignored_recovery_claim_token
        and str(action.claim_owner or "").startswith("recovery:")
    ):
        return "", "", None
    return action.claim_owner or "", action.claim_token or "", action.claim_expires_at


def execution_attempt_state_hash(attempt: ExecutionAttempt) -> str:
    result = _mapping(attempt.result_snapshot)
    snapshot = {
        "schema_version": HASH_SCHEMA_VERSION,
        "identity": {
            "id": attempt.id,
            "tenant_id": attempt.tenant_id,
            "action_id": attempt.action_id,
            "attempt_no": attempt.attempt_no,
            "worker_id": attempt.worker_id,
            "account_id": attempt.account_id,
        },
        "state": {
            "status": attempt.status,
            "before_call_at": _utc_text(attempt.before_call_at),
            "gateway_call_started_at": _utc_text(
                attempt.gateway_call_started_at,
            ),
            "after_call_at": _utc_text(attempt.after_call_at),
            "remote_message_id": attempt.remote_message_id,
            "failure_type": attempt.failure_type,
            "failure_detail_fingerprint": _text_fingerprint(
                attempt.failure_detail,
            ),
            "result_fingerprint": canonical_state_hash(result),
        },
    }
    return canonical_state_hash(snapshot)


def canonical_state_hash(payload: object) -> str:
    encoded = json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _selected(
    values: Mapping[str, object],
    keys: tuple[str, ...],
) -> dict[str, object]:
    return {
        key: _canonical_value(values[key])
        for key in keys
        if key in values
    }


def _body_fingerprint(payload: Mapping[str, object]) -> str:
    return canonical_state_hash(_selected(payload, BODY_PAYLOAD_KEYS))


def _text_fingerprint(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = [
    "HASH_SCHEMA_VERSION",
    "action_state_hash_without_recovery_claim",
    "action_state_hash",
    "canonical_state_hash",
    "execution_attempt_state_hash",
    "remote_reconcile_action_state_hash",
]
