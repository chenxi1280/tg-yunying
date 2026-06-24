from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiProvider, AiProviderHealthStatus
from app.services.ai_config import check_ai_provider


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

        monkeypatch.setattr("app.services.ai_config.ai_provider_credentials", lambda _provider: object())

        def check_without_open_transaction(_credentials):
            assert not session.in_transaction()
            return True, "ok"

        monkeypatch.setattr("app.services.ai_config.ai_gateway.check", check_without_open_transaction)

        checked = check_ai_provider(session, provider_id, "pytest")

        assert checked.health_status == AiProviderHealthStatus.HEALTHY.value
        assert checked.last_error == ""
