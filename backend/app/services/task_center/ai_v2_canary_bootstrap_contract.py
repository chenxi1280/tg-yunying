from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteItemChoice:
    priority: int
    provider_id: int
    model_name: str
    timeout_ms: int
    rate_policy: dict
    concurrency_policy: dict


@dataclass(frozen=True)
class BootstrapChoices:
    deployed_sha: str = ""
    task_id: str = ""
    expected_task_revision: int = 0
    allowed_routes: tuple[str, ...] = ()
    attestation_ids: tuple[str, ...] = ()
    route_items: tuple[tuple[str, tuple[RouteItemChoice, ...]], ...] = ()
    max_cost_per_slot: float = 0.0
    daily_ai_budget: float = 0.0
    sampling_manifest_hash: str = ""
    requester: str = ""
    approver: str = ""
    approval_ref: str = ""

    @property
    def routes(self) -> dict[str, tuple[RouteItemChoice, ...]]:
        return dict(self.route_items)


def parse_choices(payload: dict) -> BootstrapChoices:
    routes = tuple(
        (str(purpose), tuple(RouteItemChoice(**item) for item in items))
        for purpose, items in sorted(dict(payload.get("route_items") or {}).items())
    )
    return BootstrapChoices(
        deployed_sha=str(payload.get("deployed_sha") or ""),
        task_id=str(payload.get("task_id") or ""),
        expected_task_revision=int(payload.get("expected_task_revision") or 0),
        allowed_routes=tuple(dict.fromkeys(payload.get("allowed_routes") or ())),
        attestation_ids=tuple(dict.fromkeys(payload.get("attestation_ids") or ())),
        route_items=routes,
        max_cost_per_slot=float(payload.get("max_cost_per_slot") or 0),
        daily_ai_budget=float(payload.get("daily_ai_budget") or 0),
        sampling_manifest_hash=str(payload.get("sampling_manifest_hash") or ""),
        requester=str(payload.get("requester") or ""),
        approver=str(payload.get("approver") or ""),
        approval_ref=str(payload.get("approval_ref") or ""),
    )


__all__ = ["BootstrapChoices", "RouteItemChoice", "parse_choices"]
