from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AiProvider, Tenant, TenantAiSetting
from app.security import decrypt_secret, encrypt_secret

pytestmark = pytest.mark.no_postgres


def test_update_minimax_provider_upserts_m3_and_m25_without_leaking_key(monkeypatch):
    module = _load_script()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    secret_key = "sk-cp-test-secret"

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=False, fallback_to_mock=True))
        session.add(
            AiProvider(
                provider_name="Old MiniMax",
                provider_type="openai_compatible",
                base_url="https://api.minimax.io/v1",
                model_name="MiniMax-M2.5",
                api_key_ciphertext=encrypt_secret("old-key"),
                health_status="异常",
            )
        )
        session.commit()

    monkeypatch.setenv("MINIMAX_API_KEY", secret_key)
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
    monkeypatch.setenv("MINIMAX_MODEL_NAME", "minimax m3")
    monkeypatch.setattr(module, "SessionLocal", session_factory)
    monkeypatch.setattr(module, "_check_provider", lambda _config: (True, "provider ready"))

    config = module._config_from_env()
    payload = module._upsert_provider(config)

    assert secret_key not in str(payload)
    assert payload["primary"]["model_name"] == "MiniMax-M3"
    assert payload["fallback"]["model_name"] == "MiniMax-M2.5"
    assert payload["tenant_default_updated"] is True
    with Session(engine) as session:
        providers = list(session.scalars(select(AiProvider).order_by(AiProvider.model_name.asc())))
        setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == 1))
        assert {provider.model_name for provider in providers} == {"MiniMax-M2.5", "MiniMax-M3"}
        assert all(decrypt_secret(provider.api_key_ciphertext) == secret_key for provider in providers)
        primary = next(provider for provider in providers if provider.model_name == "MiniMax-M3")
        assert setting.default_provider_id == primary.id
        assert setting.ai_enabled is True
        assert setting.fallback_to_mock is False


def test_update_minimax_provider_creates_configured_provider_before_flush(monkeypatch):
    module = _load_script()
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    secret_key = "sk-cp-test-secret"

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=False, fallback_to_mock=True))
        session.commit()

    monkeypatch.setenv("MINIMAX_API_KEY", secret_key)
    monkeypatch.setattr(module, "SessionLocal", session_factory)
    monkeypatch.setattr(module, "_check_provider", lambda _config: (True, "provider ready"))

    payload = module._upsert_provider(module._config_from_env())

    assert payload["primary"]["created"] is True
    assert payload["fallback"]["created"] is True
    with Session(engine) as session:
        providers = list(session.scalars(select(AiProvider).order_by(AiProvider.model_name.asc())))
        assert {provider.model_name for provider in providers} == {"MiniMax-M2.5", "MiniMax-M3"}
        assert all(provider.base_url == "https://api.minimaxi.com/v1" for provider in providers)
        assert all(decrypt_secret(provider.api_key_ciphertext) == secret_key for provider in providers)


def _load_script():
    path = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "update_minimax_provider.py"
    spec = importlib.util.spec_from_file_location("update_minimax_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
