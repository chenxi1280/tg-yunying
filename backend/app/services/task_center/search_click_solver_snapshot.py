from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import (
    DispatchClaimReservation,
    SearchClickSolverCarrierUnitBinding,
    SearchClickSolverProblemComponent,
    SearchClickSolverProblemSnapshot,
)

from .search_click_assignment_solver import SearchClickCandidatePath, SearchClickDemand
from .search_click_dispatch_allocation import SearchClickFulfillmentUnit

SOLVER_CONTRACT_VERSION = "search-click-assignment-v2"


@dataclass(frozen=True)
class SearchSolverSnapshot:
    problem_payload: dict
    carrier_payload: dict
    components: tuple[dict, ...]
    problem_hash: str
    input_hash: str
    demands: tuple[SearchClickDemand, ...]
    paths: tuple[SearchClickCandidatePath, ...]


def assemble_search_solver_snapshot(
    session: Session,
    units: tuple[SearchClickFulfillmentUnit, ...],
    *,
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> SearchSolverSnapshot:
    ordered_demands = tuple(
        sorted(demands, key=_demand_order)
    )
    ordered_paths = tuple(sorted(paths, key=lambda item: item.key))
    problem_payload = _problem_payload(ordered_demands, ordered_paths)
    components, component_by_obligation = _components(
        ordered_demands,
        ordered_paths,
    )
    carrier_payload = _carrier_payload(
        session,
        units,
        component_by_obligation=component_by_obligation,
    )
    problem_hash = _hash({
        "solver_contract_version": SOLVER_CONTRACT_VERSION,
        "components": list(components),
    })
    input_hash = _hash({
        "solver_contract_version": SOLVER_CONTRACT_VERSION,
        "solver_problem_hash": problem_hash,
        "carrier": carrier_payload,
    })
    return SearchSolverSnapshot(
        problem_payload,
        carrier_payload,
        components,
        problem_hash,
        input_hash,
        ordered_demands,
        ordered_paths,
    )


def _demand_order(item: SearchClickDemand) -> tuple:
    return (
        item.task_deadline_at.isoformat() if item.task_deadline_at else "",
        item.task_last_opportunity_at.isoformat()
        if item.task_last_opportunity_at
        else "",
        item.persistent_task_cursor,
        item.task_id,
        item.obligation_id,
    )


def persist_search_solver_snapshot(
    session: Session,
    epoch_id: str,
    snapshot: SearchSolverSnapshot,
) -> SearchClickSolverProblemSnapshot:
    row = SearchClickSolverProblemSnapshot(
        search_click_assignment_epoch_id=epoch_id,
        solver_contract_version=SOLVER_CONTRACT_VERSION,
        canonical_problem_payload=snapshot.problem_payload,
        canonical_carrier_payload=snapshot.carrier_payload,
        solver_problem_hash=snapshot.problem_hash,
        solver_input_hash=snapshot.input_hash,
    )
    session.add(row)
    session.flush()
    session.add_all([
        SearchClickSolverProblemComponent(
            search_click_solver_snapshot_id=row.id,
            stable_component_key=item["stable_component_key"],
            canonical_component_payload=item["payload"],
            solver_problem_component_hash=item["component_hash"],
        )
        for item in snapshot.components
    ])
    session.add_all([
        SearchClickSolverCarrierUnitBinding(
            search_click_solver_snapshot_id=row.id,
            dispatch_claim_reservation_id=item["reservation_id"],
            fulfillment_lane_claim_ordinal=item["ordinal"],
            obligation_id=item["obligation_id"],
            task_id=item["task_id"],
            stable_component_key=item["stable_component_key"],
            solver_problem_component_hash=item["solver_problem_component_hash"],
            canonical_binding_payload=item,
        )
        for item in snapshot.carrier_payload["units"]
    ])
    return row


def solver_component_hash_for_unit(
    session: Session,
    epoch_id: str,
    reservation_id: str,
    *,
    ordinal: int,
) -> str:
    snapshot = session.scalar(select(SearchClickSolverProblemSnapshot).where(
        SearchClickSolverProblemSnapshot.search_click_assignment_epoch_id
        == epoch_id
    ))
    if snapshot is None:
        raise RuntimeError("search_solver_snapshot_missing")
    value = session.scalar(select(
        SearchClickSolverCarrierUnitBinding.solver_problem_component_hash
    ).where(
        SearchClickSolverCarrierUnitBinding.search_click_solver_snapshot_id
        == snapshot.id,
        SearchClickSolverCarrierUnitBinding.dispatch_claim_reservation_id
        == reservation_id,
        SearchClickSolverCarrierUnitBinding.fulfillment_lane_claim_ordinal
        == ordinal,
    ))
    if not value:
        raise RuntimeError("search_solver_component_binding_missing")
    return value


def _problem_payload(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> dict:
    return {
        "solver_contract_version": SOLVER_CONTRACT_VERSION,
        "demands": [
            {
                "obligation_id": item.obligation_id,
                "task_id": item.task_id,
                "task_remaining_count": item.task_remaining_count,
                "task_deadline_at": (
                    item.task_deadline_at.isoformat()
                    if item.task_deadline_at
                    else None
                ),
                "task_last_opportunity_at": (
                    item.task_last_opportunity_at.isoformat()
                    if item.task_last_opportunity_at
                    else None
                ),
                "persistent_task_cursor": item.persistent_task_cursor,
                "task_cursor_version": item.task_cursor_version,
            }
            for item in demands
        ],
        "paths": [_path_payload(item) for item in paths],
    }


def _path_payload(path: SearchClickCandidatePath) -> dict:
    return {
        "key": path.key,
        "account_id": path.account_id,
        "authorization_id": path.authorization_id,
        "keyword_hash": path.keyword_hash,
        "proxy_route_id": path.proxy_route_id,
        "protocol_sample_version": path.protocol_sample_version,
        "hard_safe_remaining_capacity": path.hard_safe_remaining_capacity,
        "confirmed_click_count_today": path.confirmed_click_count_today,
        "last_click_opportunity_at": (
            path.last_click_opportunity_at.isoformat()
            if path.last_click_opportunity_at
            else None
        ),
        "persistent_account_cursor": path.persistent_account_cursor,
        "eligible_obligation_ids": sorted(path.eligible_obligation_ids),
        "resource_versions": [
            list(item) for item in sorted(path.resource_versions)
        ],
    }


def _component_key(problem_payload: dict) -> str:
    identity = {
        "solver_contract_version": SOLVER_CONTRACT_VERSION,
        "demands": problem_payload["demands"],
        "paths": [
            {
                "key": item["key"],
                "account_id": item["account_id"],
                "authorization_id": item["authorization_id"],
                "keyword_hash": item["keyword_hash"],
                "proxy_route_id": item["proxy_route_id"],
                "protocol_sample_version": item["protocol_sample_version"],
                "eligible_obligation_ids": item["eligible_obligation_ids"],
            }
            for item in problem_payload["paths"]
        ],
    }
    return _hash(identity)


def _components(
    demands: tuple[SearchClickDemand, ...],
    paths: tuple[SearchClickCandidatePath, ...],
) -> tuple[tuple[dict, ...], dict[str, tuple[str, str]]]:
    parent = {
        **{f"d:{item.obligation_id}": f"d:{item.obligation_id}" for item in demands},
        **{f"p:{item.key}": f"p:{item.key}" for item in paths},
    }
    _union_task_demands(parent, demands)
    _union_eligible_paths(parent, demands, paths)
    _union_shared_resources(parent, paths)
    grouped = _group_nodes(parent)
    components = tuple(sorted(
        (_component_from_nodes(nodes, demands, paths) for nodes in grouped),
        key=lambda item: item["stable_component_key"],
    ))
    binding = {
        demand["obligation_id"]: (
            component["stable_component_key"],
            component["component_hash"],
        )
        for component in components
        for demand in component["payload"]["demands"]
    }
    return components, binding


def _find(parent: dict[str, str], node: str) -> str:
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def _union(parent: dict[str, str], left: str, right: str) -> None:
    left_root, right_root = _find(parent, left), _find(parent, right)
    if left_root != right_root:
        parent[max(left_root, right_root)] = min(left_root, right_root)


def _union_task_demands(
    parent: dict[str, str],
    demands: tuple[SearchClickDemand, ...],
) -> None:
    first_by_task: dict[str, str] = {}
    for demand in demands:
        node = f"d:{demand.obligation_id}"
        first = first_by_task.setdefault(demand.task_id, node)
        _union(parent, first, node)


def _union_eligible_paths(parent, demands, paths) -> None:
    all_ids = tuple(item.obligation_id for item in demands)
    for path in paths:
        eligible = path.eligible_obligation_ids or all_ids
        for obligation_id in eligible:
            node = f"d:{obligation_id}"
            if node in parent:
                _union(parent, f"p:{path.key}", node)


def _union_shared_resources(
    parent: dict[str, str],
    paths: tuple[SearchClickCandidatePath, ...],
) -> None:
    first_by_resource: dict[tuple[str, str], str] = {}
    for path in paths:
        node = f"p:{path.key}"
        resources = {
            ("account", str(path.account_id)),
            ("authorization", str(path.authorization_id)),
            ("proxy_route", path.proxy_route_id),
            *[(kind, identity) for kind, identity, _ in path.resource_versions],
        }
        for resource in resources:
            first = first_by_resource.setdefault(resource, node)
            _union(parent, first, node)


def _group_nodes(parent: dict[str, str]) -> tuple[tuple[str, ...], ...]:
    result: dict[str, list[str]] = {}
    for node in parent:
        result.setdefault(_find(parent, node), []).append(node)
    return tuple(tuple(sorted(nodes)) for nodes in result.values())


def _component_from_nodes(nodes, demands, paths) -> dict:
    demand_ids = {node[2:] for node in nodes if node.startswith("d:")}
    path_keys = {node[2:] for node in nodes if node.startswith("p:")}
    payload = _problem_payload(
        tuple(item for item in demands if item.obligation_id in demand_ids),
        tuple(item for item in paths if item.key in path_keys),
    )
    key = _component_key(payload)
    return {
        "stable_component_key": key,
        "component_hash": _hash({
            "stable_component_key": key,
            **payload,
        }),
        "payload": payload,
    }


def _carrier_payload(
    session: Session,
    units: tuple[SearchClickFulfillmentUnit, ...],
    *,
    component_by_obligation: dict[str, tuple[str, str]],
) -> dict:
    rows = []
    for unit in sorted(
        units,
        key=lambda item: (item.reservation_id, item.fulfillment_lane_claim_ordinal),
    ):
        reservation = session.get(DispatchClaimReservation, unit.reservation_id)
        if reservation is None:
            raise RuntimeError("search_solver_reservation_missing")
        component_key, component_hash = component_by_obligation[
            unit.obligation_id
        ]
        rows.append({
            "window_id": unit.window_id,
            "dispatch_allocation_epoch": unit.dispatch_allocation_epoch,
            "reservation_id": unit.reservation_id,
            "reservation_version": reservation.version,
            "reserved_claims": reservation.reserved_claims,
            "bound_count": reservation.bound_count,
            "claimed_count": reservation.claimed_count,
            "released_count": reservation.released_count,
            "ordinal": unit.fulfillment_lane_claim_ordinal,
            "obligation_id": unit.obligation_id,
            "task_id": unit.task_id,
            "stable_component_key": component_key,
            "solver_problem_component_hash": component_hash,
        })
    return {"units": rows}


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "SOLVER_CONTRACT_VERSION",
    "SearchSolverSnapshot",
    "assemble_search_solver_snapshot",
    "persist_search_solver_snapshot",
    "solver_component_hash_for_unit",
]
