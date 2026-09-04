from app.services.automation_identity import AUTOMATION_IDENTITY_POLICY_VERSION
from app.ai_http_transport import TRANSPORT_POLICY_REVISION

from .ai_provider_routes import (
    COMMENT_REALIZE_PURPOSE, COMMENT_REVIEW_PURPOSE, COMMENT_ROUTE_PURPOSE,
    GROUP_REVIEW_PURPOSE, GROUP_ROUTE_PURPOSE, REALIZE_PURPOSE_BY_MODE,
)
from .engagement_timing_measurements import timing_hash
from .engagement_timing_path import TimingExecutionPath
from .ai_generation_stage_config import fallback_stages


def generation_execution_path(job, *, adapter: str, config: dict) -> TimingExecutionPath:
    if not job.prompt_contract_version or not job.example_set_version:
        raise ValueError("generation_timing_content_contract_missing")
    if adapter == "channel_comment":
        purposes = {"router": COMMENT_ROUTE_PURPOSE, "realizer": COMMENT_REALIZE_PURPOSE, "reviewer": COMMENT_REVIEW_PURPOSE}
    elif adapter == "group_ai_chat" and job.content_mode in REALIZE_PURPOSE_BY_MODE:
        purposes = {"router": GROUP_ROUTE_PURPOSE, "realizer": REALIZE_PURPOSE_BY_MODE[job.content_mode], "reviewer": GROUP_REVIEW_PURPOSE}
    else:
        raise ValueError("generation_timing_content_mode_invalid")
    policy = {
        "contract": "generation_preparation_v1", "prompt": job.prompt_contract_version,
        "examples": job.example_set_version, "voice": job.voice_profile_version,
        "identity": AUTOMATION_IDENTITY_POLICY_VERSION, "two_stage": bool(config.get("ai_two_stage_enabled")),
        "transport": TRANSPORT_POLICY_REVISION,
        "batch_size": len(config.get("generation_slots") or ()) or 1,
        "fallback_stages": list(fallback_stages(config)),
        "grok_fallback": bool(config.get("_ai_group_grok_fallback_enabled", False)),
    }
    routes = tuple((role, _route_identity(job.provider_route_snapshots, purpose)) for role, purpose in sorted(purposes.items()))
    return TimingExecutionPath(f"generation_preparation_v1:{timing_hash(policy)}", routes)


def _route_identity(snapshots: dict, purpose: str) -> str:
    snapshot = dict((snapshots or {}).get(purpose) or {})
    identity = str(snapshot.get("route_set_id") or "")
    revision = int(snapshot.get("revision") or 0)
    content_hash = str(snapshot.get("content_hash") or "")
    if not identity or revision <= 0 or not content_hash:
        raise ValueError(f"generation_timing_route_snapshot_missing:{purpose}")
    return f"{identity}:{revision}:{content_hash}"
