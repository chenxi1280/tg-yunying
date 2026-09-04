from dataclasses import dataclass


TIMING_MEASUREMENT_REVISION = "elapsed_boundaries_v1"
CONTENT_ADAPTERS = frozenset({"group_ai_chat", "channel_comment"})
PASSIVE_ADAPTERS = frozenset({"channel_like", "channel_view"})


@dataclass(frozen=True)
class TimingExecutionPath:
    preparation_policy_revision: str
    provider_routes: tuple[tuple[str, str], ...]
    measurement_revision: str = TIMING_MEASUREMENT_REVISION

    def snapshot(self, *, adapter: str, lane: str) -> dict:
        stages = path_stages(adapter, lane)
        validate_provider_roles(adapter, lane, self.provider_routes)
        if not self.preparation_policy_revision.strip() or any(not route.strip() for _, route in self.provider_routes):
            raise ValueError("execution_timing_path_revision_missing")
        if self.measurement_revision != TIMING_MEASUREMENT_REVISION:
            raise ValueError("execution_timing_measurement_revision_unsupported")
        return {
            "preparation_policy_revision": self.preparation_policy_revision,
            "measurement_revision": self.measurement_revision,
            "provider_routes": dict(sorted(self.provider_routes)),
            "stages": list(stages),
        }


def validate_provider_roles(adapter: str, lane: str, routes: tuple[tuple[str, str], ...]) -> None:
    roles = [role for role, _ in routes]
    required = required_provider_roles(adapter, lane)
    optional = _optional_provider_roles(adapter, lane)
    if len(set(roles)) != len(roles) or not required <= set(roles) or set(roles) - required - optional:
        raise ValueError("execution_timing_provider_roles_invalid")


def _optional_provider_roles(adapter: str, lane: str) -> set[str]:
    if adapter not in CONTENT_ADAPTERS or lane not in {"response", "proactive"}:
        return set()
    return {"router", "repair", "reviewer"} if adapter == "group_ai_chat" else {"router"}


def required_provider_roles(adapter: str, lane: str) -> set[str]:
    if adapter in PASSIVE_ADAPTERS:
        return set()
    if lane == "classification":
        return {"classification"}
    return {"realizer", "reviewer"} if adapter == "channel_comment" else {"realizer"}


def path_stages(adapter: str, lane: str) -> tuple[str, ...]:
    if adapter in CONTENT_ADAPTERS and lane == "classification":
        return ("pre_materialization", "pre_provider", "post_classification", "claim_finalized")
    if adapter in CONTENT_ADAPTERS and lane in {"response", "proactive"}:
        if adapter == "channel_comment":
            return ("pre_materialization", "pre_provider", "reviewer_started", "ready_action", "gateway_call_issued")
        return ("pre_materialization", "pre_provider", "ready_action", "gateway_call_issued")
    if adapter in PASSIVE_ADAPTERS and lane == "passive":
        return ("pre_materialization", "ready_action", "gateway_call_issued")
    raise ValueError("execution_timing_adapter_lane_invalid")
