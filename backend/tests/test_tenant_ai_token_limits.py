from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiProvider, Tenant, TenantAiSetting
from app.schemas.ai_config import TenantAiSettingUpdate
from app.security import encrypt_secret
from app.services.ai_config import update_tenant_ai_setting


pytestmark = pytest.mark.no_postgres


def test_tenant_ai_setting_accepts_large_minimax_m3_token_limit():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        provider = _provider("MiniMax", "MiniMax-M3")
        session.add_all([Tenant(id=1, name="默认运营空间"), provider])
        session.flush()
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=provider.id, max_tokens=8192))
        session.commit()

        updated = update_tenant_ai_setting(
            session,
            1,
            TenantAiSettingUpdate(default_provider_id=provider.id, max_tokens=250000),
            "pytest",
        )

        assert updated.max_tokens == 250000


def test_tenant_ai_setting_rejects_minimax_token_limit_above_250k():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        provider = _provider("MiniMax", "MiniMax-M3")
        session.add_all([Tenant(id=1, name="默认运营空间"), provider])
        session.flush()
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=provider.id, max_tokens=8192))
        session.commit()

        with pytest.raises(ValueError, match="250000"):
            update_tenant_ai_setting(
                session,
                1,
                TenantAiSettingUpdate(default_provider_id=provider.id, max_tokens=250001),
                "pytest",
            )


def test_tenant_ai_setting_accepts_100k_non_minimax_token_limit():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        provider = _provider("DeepSeek", "deepseek-chat")
        session.add_all([Tenant(id=1, name="默认运营空间"), provider])
        session.flush()
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=provider.id, max_tokens=8192))
        session.commit()

        updated = update_tenant_ai_setting(
            session,
            1,
            TenantAiSettingUpdate(default_provider_id=provider.id, max_tokens=100000),
            "pytest",
        )

        assert updated.max_tokens == 100000


def test_tenant_ai_setting_rejects_non_minimax_token_limit_above_100k():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        provider = _provider("DeepSeek", "deepseek-chat")
        session.add_all([Tenant(id=1, name="默认运营空间"), provider])
        session.flush()
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=provider.id, max_tokens=8192))
        session.commit()

        with pytest.raises(ValueError, match="最大 Token"):
            update_tenant_ai_setting(
                session,
                1,
                TenantAiSettingUpdate(default_provider_id=provider.id, max_tokens=100001),
                "pytest",
            )


def _provider(provider_name: str, model_name: str) -> AiProvider:
    return AiProvider(
        provider_name=provider_name,
        provider_type="openai_compatible",
        base_url="https://example.test/v1",
        model_name=model_name,
        api_key_ciphertext=encrypt_secret("secret"),
        api_key_header="Authorization",
        health_status="健康",
    )
