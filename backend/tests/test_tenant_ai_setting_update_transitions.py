from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiProvider,
    AuditLog,
    Tenant,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from app.schemas.ai_config import TenantAiSettingUpdate
from app.services import ai_config


pytestmark = pytest.mark.no_postgres


def test_enabling_fallback_failure_is_audited() -> None:
    with Session(_engine()) as session:
        _seed_setting_route(session)

        with pytest.raises(ValueError, match="at least two healthy active providers"):
            ai_config.update_tenant_ai_setting(
                session,
                1,
                TenantAiSettingUpdate(ai_provider_route_fallback_enabled=True),
                "pytest",
            )

        audit = session.scalar(select(AuditLog).where(
            AuditLog.action == "更新客户AI配置失败",
        ))
        assert audit is not None
        assert "provider_route_candidates_insufficient" in audit.detail
        assert "ai_provider_route_fallback_enabled" in audit.detail


def test_unchanged_fallback_does_not_block_healthy_default_switch() -> None:
    with Session(_engine()) as session:
        _seed_setting_route(session, fallback_enabled=True)
        target = _provider(4, "MiniMax-M2.5")
        target.is_active = True
        session.add(target)
        session.commit()

        updated = ai_config.update_tenant_ai_setting(
            session,
            1,
            TenantAiSettingUpdate(
                default_provider_id=4,
                ai_provider_route_fallback_enabled=True,
            ),
            "pytest",
        )

        assert updated.default_provider_id == 4
        assert updated.ai_provider_route_fallback_enabled is True


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_setting_route(session: Session, *, fallback_enabled: bool = False) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add_all((_provider(1, "mimo-v2.5"), _unhealthy_provider()))
    session.add(TenantAiSetting(
        tenant_id=1,
        default_provider_id=1,
        ai_provider_route_fallback_enabled=fallback_enabled,
    ))
    route = TenantAiProviderRouteSet(
        tenant_id=1,
        purpose="group_realize_general",
        revision=2,
        status="active",
        content_hash="a" * 64,
    )
    session.add(route)
    session.flush()
    session.add_all((
        TenantAiProviderRouteItem(
            route_set_id=route.id,
            priority=1,
            provider_id=2,
            model_name="deepseek-chat",
        ),
        TenantAiProviderRouteItem(
            route_set_id=route.id,
            priority=2,
            provider_id=1,
            model_name="mimo-v2.5",
        ),
    ))
    session.commit()


def _provider(provider_id: int, model: str) -> AiProvider:
    return AiProvider(
        id=provider_id,
        provider_name=f"provider-{provider_id}",
        base_url="mock://provider",
        model_name=model,
        api_key_ciphertext="ciphertext",
        credential_enabled=True,
        is_active=True,
        health_status="健康",
    )


def _unhealthy_provider() -> AiProvider:
    provider = _provider(2, "deepseek-chat")
    provider.health_status = "异常"
    return provider
