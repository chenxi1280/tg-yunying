from __future__ import annotations

from .dispatch_claim_types import DEFAULT_DISPATCHER_SCOPE, DispatchClaimBinding


def dispatcher_scope(settings) -> str:
    value = str(
        getattr(settings, "dispatcher_claim_scope", DEFAULT_DISPATCHER_SCOPE) or ""
    ).strip()
    return value or DEFAULT_DISPATCHER_SCOPE


def dispatcher_claim_capacity(settings, requested_limit: int) -> int:
    configured_limit = max(
        1,
        int(getattr(settings, "action_claim_limit", requested_limit) or requested_limit),
    )
    scope_capacity = int(getattr(settings, "dispatcher_scope_capacity", 0) or 0)
    if scope_capacity > 0:
        return min(configured_limit, scope_capacity)
    concurrency = int(
        getattr(settings, "dispatcher_concurrency", configured_limit)
        or configured_limit
    )
    return min(configured_limit, max(1, concurrency))


def binding_metadata(binding: DispatchClaimBinding) -> dict[str, object]:
    return {
        "dispatch_claim_class": binding.claim_class,
        "dispatch_reservation_id": binding.reservation_id,
        "dispatch_claim_window_id": binding.window_id,
        "dispatch_claim_shard_allocation_id": binding.shard_allocation_id,
        "dispatch_claim_scope": binding.dispatcher_scope,
        "dispatch_claim_shard": {
            "total": binding.shard_total,
            "index": binding.shard_index,
        },
        "dispatch_allocation_epoch": binding.allocation_epoch,
        "dispatch_reservation_reason": binding.reservation_reason,
        "dispatch_urgency_score": binding.urgency_score,
        "dispatch_unserved_strict_classes": list(binding.unserved_strict_classes),
    }


__all__ = ["binding_metadata", "dispatcher_claim_capacity", "dispatcher_scope"]
