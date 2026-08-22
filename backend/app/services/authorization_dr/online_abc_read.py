from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from app.models import (
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)

from .contracts import AuthorizationDrError


UNKNOWN_OPERATION_STATUSES = {"provision_reconcile_unknown", "reconcile_unknown"}
TERMINAL_FAILURES = {"failed", "manual_required", "migration_rolled_back_forward"}


def render_online_abc_status(session, batch_id: str) -> dict:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    if not batch:
        raise AuthorizationDrError("online_abc_batch_not_found", "Online ABC batch is unavailable")
    items = _items(session, batch.id)
    slots = _slots(session, batch.id)
    account_counts = dict(Counter(item.outcome for item in items))
    b_counts = _slot_counts(slots, "standby_1")
    c_counts = _slot_counts(slots, "standby_2")
    return {
        "batch_id": batch.id,
        "tenant_id": batch.tenant_id,
        "status": batch.status,
        "target_count": batch.target_count,
        "target_set_fingerprint": batch.target_set_fingerprint,
        "deployed_release_sha": batch.deployed_release_sha,
        "execution_release_sha": batch.execution_release_sha or batch.deployed_release_sha,
        "account_outcome_counts": account_counts,
        "standby_1_outcome_counts": b_counts,
        "standby_2_outcome_counts": c_counts,
        "conservation": _conservation(batch.target_count, account_counts, b_counts, c_counts),
        "items": [_item_status(item, slots) for item in items],
        "observation_started_at": _iso(batch.observation_started_at),
        "observation_closes_at": _iso(batch.observation_closes_at),
    }


def _items(session, batch_id: str):
    return list(session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)))


def _slots(session, batch_id: str):
    return list(session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.batch_id == batch_id,
    )))


def _item_status(item, slots) -> dict:
    result = {slot.logical_slot: slot for slot in slots if slot.item_id == item.id}
    return {
        "item_id": item.id, "ordinal": item.ordinal, "account_id": item.account_id,
        "status": item.status, "outcome": item.outcome,
        "primary_probe_outcome": item.primary_probe_outcome,
        "standby_1_outcome": result["standby_1"].outcome,
        "standby_2_outcome": result["standby_2"].outcome,
        "blocker_code": item.blocker_code,
    }


def _slot_counts(slots, logical_slot: str) -> dict:
    return dict(Counter(slot.outcome for slot in slots if slot.logical_slot == logical_slot))


def _conservation(target_count, account_counts, b_counts, c_counts) -> dict:
    totals = [sum(counts.values()) for counts in (account_counts, b_counts, c_counts)]
    return {
        "account_total": totals[0], "standby_1_total": totals[1],
        "standby_2_total": totals[2], "expected_total": target_count,
        "valid": all(total == target_count for total in totals),
    }


def _iso(value) -> str:
    return value.isoformat() if value else ""


def item_operations_complete(session, item, operations: dict) -> bool:
    slots = {slot.logical_slot: slot for slot in session.scalars(
        select(TgAuthorizationOnlineAbcSlotResult).where(
            TgAuthorizationOnlineAbcSlotResult.item_id == item.id,
        )
    )}
    b_done = slots["standby_1"].outcome in {"already_qualified", "succeeded"}
    c_done = slots["standby_2"].outcome in {"already_qualified", "succeeded"}
    e4_done = operations["e4"] and operations["e4"].status == "succeeded"
    return bool(b_done and c_done and e4_done)


def operation_outcome(status: str) -> str:
    if status == "succeeded":
        return "succeeded"
    if status in UNKNOWN_OPERATION_STATUSES:
        return "reconcile_unknown"
    if status in TERMINAL_FAILURES:
        return status
    return "running"


__all__ = ["item_operations_complete", "operation_outcome", "render_online_abc_status"]
