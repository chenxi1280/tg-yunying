from __future__ import annotations

import hashlib
import json
from collections import Counter

from sqlalchemy import select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)

from .online_abc_exception_state import (
    e4_remote_id,
    operation_snapshots,
    primary_snapshot,
    slot_snapshots,
)
from .online_abc_operations import online_abc_item_operations


def canonical_deferred_manifest(session, batch_id: str, *, runtime_release_sha: str) -> dict:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    rows = [
        _manifest_row(session, batch, item)
        for item in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
            TgAuthorizationOnlineAbcItem.batch_id == batch.id,
            TgAuthorizationOnlineAbcItem.outcome == "deferred_reconcile",
        ).order_by(TgAuthorizationOnlineAbcItem.ordinal))
    ]
    payload = {
        "schema": "abc_deferred_recovery_manifest_v1",
        "batch": _batch_manifest(batch, runtime_release_sha),
        "runtime": _runtime_manifest(session),
        "rows": rows,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "manifest_hash": hashlib.sha256(text.encode()).hexdigest(),
        "row_count": len(rows),
        "groups": _manifest_groups(rows),
    }


def _manifest_row(session, batch, item) -> dict:
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    operations = online_abc_item_operations(session, batch, item)
    context = type("Context", (), {"account": account, "primary": primary, "item": item})()
    return {
        "item": [item.id, item.account_id, item.ordinal, item.version, item.status, item.outcome, item.blocker_code],
        "primary": primary_snapshot(context),
        "slots": slot_snapshots(_slots(session, item.id)),
        "operations": operation_snapshots(session, operations),
        "e4_remote_id_present": bool(operations["e4"] and e4_remote_id(session, operations["e4"].id)),
    }


def _manifest_groups(rows: list[dict]) -> list[dict]:
    groups = Counter()
    for row in rows:
        problem = next((op for op in reversed(row["operations"]) if len(op) > 2 and op[3] != "succeeded"), None)
        key = (
            problem[0] if problem else "",
            problem[3] if problem else "",
            problem[4] if problem else "",
            row["item"][6],
        )
        groups[key] += 1
    return [
        {"slot": key[0], "operation_status": key[1], "remote_call_state": key[2], "blocker": key[3], "count": count}
        for key, count in sorted(groups.items())
    ]


def _batch_manifest(batch, release_sha: str) -> dict:
    return {
        "id": batch.id, "version": batch.version, "status": batch.status,
        "target_count": batch.target_count, "deployed_release_sha": batch.deployed_release_sha,
        "execution_release_sha": batch.execution_release_sha, "runtime_release_sha": release_sha,
    }


def _runtime_manifest(session) -> dict:
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    nodes = session.scalars(select(AuthorizationDrExecutionNode).order_by(AuthorizationDrExecutionNode.id))
    return {
        "mode": runtime.mode if runtime else "",
        "scope": runtime.claim_scope_operation_id if runtime else "",
        "version": runtime.version if runtime else 0,
        "nodes": [[node.id, node.region_code, node.status, node.active_client_count, node.runtime_image_sha]
                  for node in nodes],
    }


def _slots(session, item_id: str) -> dict:
    return {
        slot.logical_slot: slot
        for slot in session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
            TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
        ))
    }


__all__ = ["canonical_deferred_manifest"]
