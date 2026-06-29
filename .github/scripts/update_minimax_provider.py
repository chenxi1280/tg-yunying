from __future__ import annotations

import json
import os
from dataclasses import dataclass

from sqlalchemy import or_, select

from app.ai_gateway import AiGateway, AiProviderCredentials, normalize_ai_model_name
from app.database import SessionLocal
from app.models import AiProvider, AiProviderHealthStatus, TenantAiSetting
from app.models.enums import now
from app.security import encrypt_secret


DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL_NAME = "MiniMax-M3"


@dataclass(frozen=True)
class MinimaxProviderConfig:
    api_key: str
    base_url: str
    model_name: str
    tenant_id: int


def main() -> int:
    config = _config_from_env()
    ok, detail = _check_provider(config)
    if not ok:
        raise RuntimeError(f"MiniMax provider check failed: {detail}")
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
        provider = _existing_provider(session)
        created = provider is None
        if provider is None:
            provider = AiProvider(provider_name="MiniMax", provider_type="openai_compatible")
            session.add(provider)
        _apply_provider_config(provider, config)
        session.flush()
        default_updated = _set_default_provider(session, config.tenant_id, provider.id)
        session.commit()
        session.refresh(provider)
        return _result_payload(provider, created, default_updated, config.tenant_id)


def _existing_provider(session):
    return session.scalar(
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
    )


def _apply_provider_config(provider: AiProvider, config: MinimaxProviderConfig) -> None:
    provider.provider_name = "MiniMax"
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
    provider.notes = "MiniMax CN OpenAI-compatible; key updated by GitHub Actions."
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


def _result_payload(
    provider: AiProvider,
    created: bool,
    default_updated: bool,
    tenant_id: int,
) -> dict[str, object]:
    return {
        "provider_id": provider.id,
        "created": created,
        "provider_name": provider.provider_name,
        "base_url": provider.base_url,
        "model_name": provider.model_name,
        "health_status": provider.health_status,
        "last_error": provider.last_error,
        "tenant_id": tenant_id,
        "tenant_default_updated": default_updated,
    }


if __name__ == "__main__":
    raise SystemExit(main())
