from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderRateLimited
from app.database import Base
from app.models import AiProvider, AiProviderHealthStatus
from app.schemas.ai_config import AiProviderCreate, AiProviderUpdate
from app.services.ai_config import (
    check_ai_provider,
    create_ai_provider,
    update_ai_provider,
)


pytestmark = pytest.mark.no_postgres


def test_check_ai_provider_releases_transaction_during_external_check(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        provider = AiProvider(
            provider_name="pytest",
            provider_type="openai_compatible",
            base_url="https://example.test",
            model_name="pytest-model",
            api_key_ciphertext="ciphertext",
            health_status=AiProviderHealthStatus.HEALTHY.value,
        )
        session.add(provider)
        session.commit()
        provider_id = provider.id

        monkeypatch.setattr(
            "app.services.ai_config.ai_provider_credentials",
            lambda _provider, **_kwargs: object(),
        )

        def check_without_open_transaction(_credentials):
            assert not session.in_transaction()
            return True, "ok"

        monkeypatch.setattr("app.services.ai_config.ai_gateway.check", check_without_open_transaction)

        checked = check_ai_provider(session, provider_id, "pytest")

        assert checked.health_status == AiProviderHealthStatus.HEALTHY.value
        assert checked.last_error == ""


def test_check_route_credential_without_making_it_legacy_active(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        provider = AiProvider(
            provider_name="mimo",
            base_url="https://example.test",
            model_name="mimo-v2",
            api_key_ciphertext="ciphertext",
            credential_enabled=True,
            is_active=False,
            health_status=AiProviderHealthStatus.DISABLED.value,
        )
        session.add(provider)
        session.commit()
        monkeypatch.setattr(
            "app.services.ai_config.ai_provider_credentials",
            lambda _provider, **_kwargs: object(),
        )
        monkeypatch.setattr(
            "app.services.ai_config.ai_gateway.check",
            lambda _credentials: (True, "ok"),
        )

        checked = check_ai_provider(session, provider.id, "pytest")

        assert checked.health_status == AiProviderHealthStatus.HEALTHY.value
        assert checked.is_active is False
        assert checked.credential_enabled is True


def test_check_ai_provider_keeps_rate_limited_provider_healthy(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        provider = AiProvider(
            provider_name="mimo",
            base_url="https://example.test",
            model_name="mimo-v2",
            api_key_ciphertext="ciphertext",
            credential_enabled=True,
            is_active=True,
            health_status=AiProviderHealthStatus.UNHEALTHY.value,
        )
        session.add(provider)
        session.commit()
        monkeypatch.setattr(
            "app.services.ai_config.ai_provider_credentials",
            lambda _provider, **_kwargs: object(),
        )

        def rate_limited(_credentials):
            raise AiProviderRateLimited(429, "temporary capacity", 30)

        monkeypatch.setattr("app.services.ai_config.ai_gateway.check", rate_limited)

        checked = check_ai_provider(session, provider.id, "pytest")

        assert checked.health_status == AiProviderHealthStatus.HEALTHY.value
        assert "temporary capacity" in checked.last_error


def test_provider_credential_state_is_independent_and_explicit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.ai_config.encrypt_secret", lambda value: value)
    with Session(engine) as session:
        provider = create_ai_provider(session, AiProviderCreate(
            provider_name="mimo",
            base_url="https://example.test",
            model_name="mimo-v2",
            api_key="secret",
            credential_enabled=True,
            is_active=False,
        ), "pytest")

        assert provider.credential_enabled is True
        assert provider.is_active is False
        assert provider.health_status == AiProviderHealthStatus.UNHEALTHY.value
        assert provider.last_error == "供应商配置已变更，必须重新检查"

        with pytest.raises(ValueError, match="active provider requires"):
            update_ai_provider(
                session,
                provider.id,
                AiProviderUpdate(credential_enabled=False, is_active=True),
                "pytest",
            )


def test_provider_identity_change_requires_new_health_check_and_can_be_disabled(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.ai_config.encrypt_secret", lambda value: value)
    with Session(engine) as session:
        provider = AiProvider(
            provider_name="mimo",
            base_url="https://old.example.test",
            model_name="mimo-v1",
            api_key_ciphertext="secret",
            credential_enabled=True,
            is_active=True,
            health_status=AiProviderHealthStatus.HEALTHY.value,
        )
        session.add(provider)
        session.commit()

        updated = update_ai_provider(
            session,
            provider.id,
            AiProviderUpdate(model_name="mimo-v2"),
            "pytest",
        )

        assert updated.health_status == AiProviderHealthStatus.UNHEALTHY.value
        assert updated.last_error == "供应商配置已变更，必须重新检查"
        disabled = update_ai_provider(
            session,
            provider.id,
            AiProviderUpdate(is_active=False),
            "pytest",
        )

        assert disabled.is_active is False
