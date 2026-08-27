from __future__ import annotations

from .identity import CODE_SOURCE_HOST, CONFIG2_CODE_SOURCE_LABEL
from .state import PhaseClaim, load_claim


CONFIG2_MIN_REQUEST_INTERVAL_SECONDS = 70


def item_host_rate_policy(
    session_factory,
    claim: PhaseClaim,
    configured_min_interval: float,
) -> tuple[str, float]:
    with session_factory() as session:
        item, _ = load_claim(session, claim)
        return host_rate_policy(item.code_source_host, configured_min_interval)


def host_rate_policy(code_source_host: str, configured_min_interval: float) -> tuple[str, float]:
    scope_id = code_source_host or CODE_SOURCE_HOST
    if scope_id == CONFIG2_CODE_SOURCE_LABEL:
        return scope_id, max(configured_min_interval, CONFIG2_MIN_REQUEST_INTERVAL_SECONDS)
    return scope_id, configured_min_interval


__all__ = ["item_host_rate_policy"]
