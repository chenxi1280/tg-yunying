from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AiProvider,
    Tenant,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from scripts import configure_ai_provider_failover as script


pytestmark = pytest.mark.no_postgres


def test_guarded_provider_enable_and_route_activation(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    _seed(factory)
    provider_ids = (5, 2)

    preview = script._providers_operation(_options("providers-preview", provider_ids))
    applied = script._providers_operation(_options(
        "providers-apply",
        provider_ids,
        fingerprint=preview["fingerprint"],
    ))
    assert applied["applied"] is True

    with factory() as session:
        for provider in session.scalars(select(AiProvider)):
            provider.health_status = "健康"
        session.commit()

    route_preview = script._route_operation(_options("route-preview", provider_ids))
    route_applied = script._route_operation(_options(
        "route-apply",
        provider_ids,
        fingerprint=route_preview["fingerprint"],
    ))

    assert route_applied["applied"] is True
    with factory() as session:
        setting = session.scalar(select(TenantAiSetting))
        active = session.scalar(select(TenantAiProviderRouteSet).where(
            TenantAiProviderRouteSet.status == "active",
        ))
        items = session.scalars(select(TenantAiProviderRouteItem).where(
            TenantAiProviderRouteItem.route_set_id == active.id,
        ).order_by(TenantAiProviderRouteItem.priority)).all()
        assert setting.ai_provider_route_fallback_enabled is True
        assert [item.provider_id for item in items] == [5, 2]


def _options(operation: str, provider_ids: tuple[int, ...], *, fingerprint: str = ""):
    return script.Options(
        operation,
        1,
        provider_ids,
        fingerprint,
        "pytest",
        "incident-test",
    )


def _seed(factory) -> None:  # noqa: ANN001
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=5))
        session.add_all((
            AiProvider(
                id=5,
                provider_name="MiniMax M3",
                base_url="https://minimax.invalid",
                model_name="MiniMax-M3",
                api_key_ciphertext="cipher",
                credential_enabled=True,
                is_active=True,
                health_status="健康",
            ),
            AiProvider(
                id=2,
                provider_name="DeepSeek",
                base_url="https://deepseek.invalid",
                model_name="deepseek-chat",
                api_key_ciphertext="cipher",
                credential_enabled=False,
                is_active=False,
                health_status="禁用",
            ),
        ))
        old = TenantAiProviderRouteSet(
            tenant_id=1,
            purpose=script.ROUTE_PURPOSE,
            revision=1,
            status="active",
            content_hash="a" * 64,
        )
        session.add(old)
        session.flush()
        session.add(TenantAiProviderRouteItem(
            route_set_id=old.id,
            priority=1,
            provider_id=5,
            model_name="MiniMax-M3",
        ))
        session.commit()
