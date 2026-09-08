"""Describe the real non-V2 provider selection without inventing V2 routes."""
from hashlib import sha256

from sqlalchemy import select

from app.models import AiProvider, TenantAiSetting

from .ai_generation_stage_config import fallback_stages
from .engagement_timing_measurements import timing_hash
from .engagement_timing_path import TimingExecutionPath


def legacy_generation_execution_path(session, task, *, job, config):
    from .ai_generator import (
        _group_chat_model, _resolve_group_generation_provider, _resolved_group_model_name,
    )

    setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == task.tenant_id))
    roles = {"realizer": config}
    if config.get("ai_two_stage_enabled"):
        roles["router"] = config
    if task.type == "channel_comment" or config.get("ai_two_stage_enabled"):
        roles["reviewer"] = {**config, "ai_model": config.get("ai_semantic_reviewer_model")}
    routes = []
    for role, selection in sorted(roles.items()):
        model = _group_chat_model(selection)
        if role == "reviewer" and not model:
            raise ValueError("generation_timing_legacy_reviewer_missing")
        stage = str(selection.get("_ai_fallback_stage") or "")
        provider, _ = _resolve_group_generation_provider(session, task.tenant_id, selection,
            setting=setting, model_name=model, stage=stage)
        if provider is None:
            raise ValueError(f"generation_timing_legacy_provider_missing:{role}")
        model = _resolved_group_model_name(provider, model, stage) or provider.model_name
        routes.append((role, _selection_identity(session, provider, model=model, config=selection)))
    policy = {"contract": "legacy_generation_job", "obligation_type": job.obligation_type,
        "prompt": job.prompt_contract_version, "examples": job.example_set_version,
        "voice": job.voice_profile_version, "two_stage": bool(config.get("ai_two_stage_enabled")),
        "fallback_stages": list(fallback_stages(config))}
    return TimingExecutionPath(f"legacy_generation_job:{timing_hash(policy)}", tuple(routes))


def _selection_identity(session, provider, *, model, config):
    ids = tuple(config.get("_ai_provider_route_provider_ids") or (provider.id,))
    models = {int(key): str(value) for key, value in dict(config.get("_ai_provider_route_models") or {}).items()}
    snapshots = []
    for provider_id in ids:
        candidate = provider if provider_id == provider.id else session.get(AiProvider, provider_id)
        if candidate is None:
            raise ValueError("generation_timing_legacy_route_provider_missing")
        snapshots.append({"provider_id": candidate.id, "model": models.get(candidate.id) or model,
            "credential_revision_hash": sha256(candidate.api_key_ciphertext.encode()).hexdigest(),
            "endpoint_hash": sha256(candidate.base_url.encode()).hexdigest()})
    return f"legacy_provider_selection:{timing_hash(snapshots)}"
