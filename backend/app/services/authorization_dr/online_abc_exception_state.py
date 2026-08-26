from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from sqlalchemy import select

from app.models import (
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)

from .contracts import AuthorizationDrError
from .online_abc_operations import online_abc_item_operations
from .online_abc_primary import manual_primary_unchanged, primary_state


CLASS_DEFERRED_ISSUE = "deferred_issue"
CLASS_DEFERRED_RECONCILE = "deferred_reconcile"
EXCEPTION_ACTION = "收集 ABC frozen-N 首轮异常"


def list_online_abc_exceptions(session, batch_id: str) -> dict:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch or batch.selection_mode != "all_online_accounts":
        raise AuthorizationDrError("online_abc_batch_not_found", "Full frozen-N batch is unavailable")
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch.id,
        TgAuthorizationOnlineAbcItem.outcome.in_({"manual_required", "deferred_reconcile"}),
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)))
    entries = [_exception_entry(session, batch, item) for item in items]
    groups = Counter((entry["classification"], entry["blocker_code"]) for entry in entries)
    return {
        "batch_id": batch.id,
        "batch_status": batch.status,
        "target_count": batch.target_count,
        "manual_required_count": sum(entry["classification"] == CLASS_DEFERRED_ISSUE for entry in entries),
        "deferred_reconcile_count": sum(entry["unresolved"] for entry in entries),
        "unresolved_count": sum(entry["unresolved"] for entry in entries),
        "root_groups": [
            {"classification": key[0], "blocker_code": key[1], "count": count}
            for key, count in sorted(groups.items())
        ],
        "items": entries,
    }


def primary_snapshot(context) -> dict:
    account, primary, item = context.account, context.primary, context.item
    frozen_match = primary_state(account, primary, item) in {"frozen", "legacy_frozen", "qualified"}
    return {
        "authorization_id": primary.id,
        "frozen_match": frozen_match,
        "current_authorization_id": account.current_authorization_id,
        "account_status": account.status,
        "primary_app_id": primary.developer_app_id,
        "primary_logical_slot": primary.logical_slot,
        "primary_is_current": primary.is_current,
        "primary_is_slot_current": primary.is_slot_current,
        "primary_protected_from_cleanup": primary.protected_from_cleanup,
        "primary_status": primary.status,
        "primary_health_status": primary.health_status,
        "account_session_digest": hashlib.sha256((account.session_ciphertext or "").encode()).hexdigest(),
        "session_digest": hashlib.sha256((primary.session_ciphertext or "").encode()).hexdigest(),
        "expected_session_digest": item.primary_session_digest,
        "telegram_user_id_digest": primary.telegram_user_id_digest,
        "auth_key_fingerprint_digest": primary.auth_key_fingerprint_digest,
        "fact_version": primary.fact_version,
        "generations": [
            account.authorization_generation,
            account.authorization_fact_generation,
            account.connection_generation,
        ],
        "expected_generations": [
            item.authorization_generation,
            item.authorization_fact_generation,
            item.connection_generation,
        ],
    }


def require_exception_primaries_unchanged(session, batch_id: str) -> None:
    items = list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.outcome.in_({"manual_required", "deferred_reconcile"}),
    )))
    for item in items:
        row = _exception_audit(session, item.id)
        unchanged = _existing_manual_primary_unchanged(session, item) if not row else (
            _exception_primary_unchanged(session, item, row.detail or "")
        )
        if not unchanged:
            raise AuthorizationDrError("online_abc_exception_primary_drift", "Queued exception A changed")


def item_snapshot(item) -> list:
    return [item.id, item.ordinal, item.version, item.status, item.outcome, item.blocker_code]


def slot_snapshots(slots: dict) -> list:
    return [[name, row.id, row.version, row.outcome, row.operation_id, row.blocker_code]
            for name, row in sorted(slots.items())]


def operation_snapshots(session, operations: dict) -> list:
    return [_operation_snapshot(session, name, operation) for name, operation in sorted(operations.items())]


def e4_remote_id(session, operation_id: str) -> str:
    row = session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_dr_operation",
        AuditLog.target_id == operation_id,
        AuditLog.action == "完成 ABC canary E4",
    ).order_by(AuditLog.id.desc()).limit(1))
    match = re.search(r"primary_saved_message_id=([^;\s]+)", row.detail or "") if row else None
    return match.group(1) if match else ""


def audit_value(detail: str, key: str) -> str:
    match = re.search(rf"(?:^|; ){re.escape(key)}=([^;]*)(?:;|$)", detail or "")
    return match.group(1) if match else ""


def _exception_audit(session, item_id: str):
    return session.scalar(select(AuditLog).where(
        AuditLog.target_type == "tg_authorization_online_abc_items",
        AuditLog.target_id == item_id,
        AuditLog.action == EXCEPTION_ACTION,
    ).order_by(AuditLog.id.desc()).limit(1))


def _existing_manual_primary_unchanged(session, item) -> bool:
    if item.outcome != "manual_required":
        return False
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    return manual_primary_unchanged(account, primary, item)


def _exception_primary_unchanged(session, item, detail: str) -> bool:
    account = session.get(TgAccount, item.account_id)
    primary_id = _integer_audit_value(detail, "primary_id")
    current_id = _integer_audit_value(detail, "current_primary_id")
    primary = session.get(TgAccountAuthorization, primary_id) if primary_id else None
    generations = _json_list(detail, "primary_generations")
    if not account or not primary or len(generations) != 3:
        return False
    return bool(
        account.current_authorization_id == current_id
        and account.status == audit_value(detail, "account_status")
        and _digest(account.session_ciphertext) == audit_value(detail, "account_session_digest")
        and _digest(primary.session_ciphertext) == audit_value(detail, "primary_session_digest")
        and account.session_ciphertext == primary.session_ciphertext
        and str(primary.developer_app_id) == audit_value(detail, "primary_app_id")
        and primary.telegram_user_id_digest == audit_value(detail, "primary_uid_digest")
        and primary.auth_key_fingerprint_digest == audit_value(detail, "primary_auth_key_digest")
        and primary.is_current == _boolean_audit_value(detail, "primary_is_current")
        and primary.is_slot_current == _boolean_audit_value(detail, "primary_is_slot_current")
        and primary.protected_from_cleanup == _boolean_audit_value(detail, "primary_protected_from_cleanup")
        and primary.status == audit_value(detail, "primary_status")
        and primary.health_status == audit_value(detail, "primary_health_status")
        and [
            account.authorization_generation,
            account.authorization_fact_generation,
            account.connection_generation,
        ] == generations
    )


def _integer_audit_value(detail: str, key: str) -> int:
    value = audit_value(detail, key)
    return int(value) if value.isdigit() else 0


def _boolean_audit_value(detail: str, key: str) -> bool:
    return audit_value(detail, key).lower() == "true"


def _json_list(detail: str, key: str) -> list:
    try:
        value = json.loads(audit_value(detail, key))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _digest(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


def _operation_snapshot(session, name: str, operation) -> list:
    if not operation:
        return [name, None]
    stages = list(session.scalars(select(TgAuthorizationDrStageFact.stage).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
    ).order_by(TgAuthorizationDrStageFact.created_at, TgAuthorizationDrStageFact.id)))
    return [
        name, operation.id, operation.operation_version, operation.status,
        operation.remote_call_state, operation.reconcile_status, operation.blocker_code,
        operation.login_flow_id, operation.candidate_authorization_id,
        bool(operation.remote_effect_started_at), bool(operation.login_challenge_sent_at),
        bool(operation.login_code_message_id), bool(operation.login_code_received_at), stages,
    ]


def _exception_entry(session, batch, item) -> dict:
    operations = online_abc_item_operations(session, batch, item)
    rows = [operation for operation in operations.values() if operation and operation.status != "succeeded"]
    operation = rows[-1] if rows else None
    classification = CLASS_DEFERRED_RECONCILE if item.outcome == "deferred_reconcile" else CLASS_DEFERRED_ISSUE
    return {
        "ordinal": item.ordinal, "account_id": item.account_id,
        "classification": classification, "unresolved": classification == CLASS_DEFERRED_RECONCILE,
        "blocker_code": item.blocker_code, "operation_id": operation.id if operation else "",
        "operation_version": operation.operation_version if operation else 0,
        "operation_status": operation.status if operation else "",
        "remote_call_state": operation.remote_call_state if operation else "",
        "primary_authorization_id": item.primary_authorization_id,
        "primary_session_digest": item.primary_session_digest,
        "primary_generations": [
            item.authorization_generation, item.authorization_fact_generation, item.connection_generation,
        ],
    }


__all__ = [
    "audit_value", "e4_remote_id", "item_snapshot", "list_online_abc_exceptions",
    "operation_snapshots", "primary_snapshot", "require_exception_primaries_unchanged",
    "slot_snapshots",
]
