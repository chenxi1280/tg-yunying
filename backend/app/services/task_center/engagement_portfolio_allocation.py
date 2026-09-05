"""Pure request identity and deterministic portfolio allocation."""
from __future__ import annotations

import hashlib
import json

from app.models import AccountPortfolioLoadReservation, Task, TaskDayLedger


POLICY_REVISION = "portfolio_account_budget_v1"


def _demand_hash(
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    request: dict,
) -> str:
    return _hash({
        "policy": POLICY_REVISION,
        "task": task.id,
        "day": str(ledger.obligation_local_date),
        "class": action_class,
        "identity": demand_identity,
        **request,
    })


def _allocation_for_request(
    rows: list[AccountPortfolioLoadReservation],
    request: dict,
) -> dict[int, int]:
    frozen = {row.account_id: int(row.reserved_units) for row in rows}
    fixed = {
        int(key): int(value)
        for key, value in request["requested_units_by_account"].items()
    }
    if fixed:
        return _positive({
            account_id: min(units, frozen.get(account_id, 0))
            for account_id, units in fixed.items()
        })
    candidates = {int(item) for item in request["candidate_account_ids"]}
    remaining = int(request["requested_units"])
    allocation: dict[int, int] = {}
    for account_id in sorted(frozen):
        if account_id not in candidates or remaining <= 0:
            continue
        units = min(frozen[account_id], remaining)
        allocation[account_id] = units
        remaining -= units
    return allocation


def _normalized_request(
    total_units: int,
    candidate_ids: list[int] | None,
    requested_by_account: dict[int, int] | None,
) -> dict:
    fixed = {
        int(account_id): max(0, int(units))
        for account_id, units in (requested_by_account or {}).items()
        if int(units) > 0
    }
    candidates = sorted({int(item) for item in (candidate_ids or fixed.keys())})
    requested = sum(fixed.values()) if fixed else max(0, int(total_units))
    return {
        "requested_units": requested,
        "candidate_account_ids": candidates,
        "requested_units_by_account": {str(key): value for key, value in fixed.items()},
    }


def _distribute(
    task_id: str,
    request: dict,
    capacities: dict[int, int],
    requested: int,
) -> dict[int, int]:
    ordered = sorted(
        capacities,
        key=lambda account_id: _hash(
            {"task": task_id, "request": request, "account": account_id}
        ),
    )
    allocation = {account_id: 0 for account_id in ordered}
    remaining = requested
    while remaining > 0:
        progressed = False
        for account_id in ordered:
            if allocation[account_id] >= capacities[account_id]:
                continue
            allocation[account_id] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    return _positive(allocation)


def _positive(values: dict[int, int]) -> dict[int, int]:
    return {key: value for key, value in values.items() if value > 0}


def _hash(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
