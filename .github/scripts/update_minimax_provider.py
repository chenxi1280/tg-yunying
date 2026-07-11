from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from sqlalchemy import or_, select

from app.ai_gateway import AiGateway, AiProviderCredentials, normalize_ai_model_name
from app.database import SessionLocal
from app.models import AiProvider, AiProviderHealthStatus, TenantAiSetting
from app.models.enums import now
from app.security import encrypt_secret


DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL_NAME = "MiniMax-M3"
FALLBACK_MODEL_NAME = "MiniMax-M2.5"


@dataclass(frozen=True)
class MinimaxProviderConfig:
    api_key: str
    base_url: str
    model_name: str
    tenant_id: int


def main() -> int:
    config = _config_from_env()
    for candidate in _provider_configs(config):
        ok, detail = _check_provider(candidate)
        if not ok:
            raise RuntimeError(f"MiniMax provider check failed for {candidate.model_name}: {detail}")
    payload = _upsert_provider(config)
    print("MINIMAX_PROVIDER_UPDATE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _config_from_env() -> MinimaxProviderConfig:
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY is required")
    return MinimaxProviderConfig(
        api_key=api_key,
        base_url=os.getenv("MINIMAX_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        model_name=normalize_ai_model_name(os.getenv("MINIMAX_MODEL_NAME", DEFAULT_MODEL_NAME)),
        tenant_id=int(os.getenv("MINIMAX_TENANT_ID", "1")),
    )


def _check_provider(config: MinimaxProviderConfig) -> tuple[bool, str]:
    credentials = AiProviderCredentials(
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url=config.base_url,
        model_name=config.model_name,
        api_key=config.api_key,
    )
    return AiGateway().check(credentials)


def _upsert_provider(config: MinimaxProviderConfig) -> dict[str, object]:
    with SessionLocal() as session:
        primary_config, fallback_config = _provider_configs(config)
        primary, primary_created = _upsert_model_provider(session, primary_config)
        fallback, fallback_created = _upsert_model_provider(session, fallback_config)
        default_updated = _set_default_provider(session, config.tenant_id, primary.id)
        session.commit()
        session.refresh(primary)
        session.refresh(fallback)
        return {
            "primary": _result_payload(primary, primary_created),
            "fallback": _result_payload(fallback, fallback_created),
            "tenant_id": config.tenant_id,
            "tenant_default_updated": default_updated,
        }


def _provider_configs(config: MinimaxProviderConfig) -> tuple[MinimaxProviderConfig, MinimaxProviderConfig]:
    primary = replace(config, model_name=DEFAULT_MODEL_NAME)
    fallback = replace(config, model_name=FALLBACK_MODEL_NAME)
    return primary, fallback


def _upsert_model_provider(session, config: MinimaxProviderConfig) -> tuple[AiProvider, bool]:
    provider = _existing_provider(session, config.model_name)
    created = provider is None
    if provider is None:
        provider = AiProvider(provider_name=f"MiniMax {config.model_name}", provider_type="openai_compatible")
        session.add(provider)
    _apply_provider_config(provider, config)
    session.flush()
    return provider, created


def _existing_provider(session, model_name: str):
    providers = session.scalars(
        select(AiProvider)
        .where(
            or_(
                AiProvider.provider_name.ilike("%minimax%"),
                AiProvider.base_url.ilike("%minimax%"),
                AiProvider.base_url.ilike("%minimaxi%"),
                AiProvider.model_name.ilike("%MiniMax%"),
            )
        )
        .order_by(AiProvider.id.asc())
    ).all()
    normalized = normalize_ai_model_name(model_name)
    return next((provider for provider in providers if normalize_ai_model_name(provider.model_name) == normalized), None)


def _apply_provider_config(provider: AiProvider, config: MinimaxProviderConfig) -> None:
    provider.provider_name = f"MiniMax {config.model_name}"
    provider.provider_type = "openai_compatible"
    provider.base_url = config.base_url
    provider.model_name = config.model_name
    provider.api_key_ciphertext = encrypt_secret(config.api_key)
    provider.api_key_header = "Authorization"
    provider.currency = "CNY"
    provider.is_billable = True
    provider.is_active = True
    provider.health_status = AiProviderHealthStatus.HEALTHY.value
    provider.last_check_at = now()
    provider.last_error = ""
    provider.notes = f"MiniMax CN OpenAI-compatible {config.model_name}; key updated by GitHub Actions."
    provider.updated_at = now()


def _set_default_provider(session, tenant_id: int, provider_id: int) -> bool:
    setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id))
    if not setting:
        return False
    setting.default_provider_id = provider_id
    setting.ai_enabled = True
    setting.fallback_to_mock = False
    setting.updated_at = now()
    return True


def _result_payload(provider: AiProvider, created: bool) -> dict[str, object]:
    return {
        "provider_id": provider.id,
        "created": created,
        "provider_name": provider.provider_name,
        "base_url": provider.base_url,
        "model_name": provider.model_name,
        "health_status": provider.health_status,
        "last_error": provider.last_error,
    }


if __name__ == "__main__":
    raise SystemExit(main())
