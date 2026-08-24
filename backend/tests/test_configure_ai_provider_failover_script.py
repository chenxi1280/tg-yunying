from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    AiProvider,
    AuditLog,
    Tenant,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from scripts import configure_ai_provider_failover as script


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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

    switched_ids = (2, 5)
    default_preview = script._default_operation(_options("default-preview", switched_ids))
    default_applied = script._default_operation(_options(
        "default-apply",
        switched_ids,
        fingerprint=default_preview["fingerprint"],
    ))
    assert default_applied["applied"] is True

    route_preview = script._route_operation(_options("route-preview", switched_ids))
    route_applied = script._route_operation(_options(
        "route-apply",
        switched_ids,
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
        assert setting.default_provider_id == 2
        assert [item.provider_id for item in items] == [2, 5]


def test_guarded_cutover_switches_default_and_route_atomically(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    _seed(factory)
    with factory() as session:
        providers = list(session.scalars(select(AiProvider)))
        for provider in providers:
            provider.credential_enabled = True
            provider.is_active = True
            provider.health_status = "健康"
        session.commit()

    options = _options("cutover-preview", (2, 5))
    preview = script._cutover_operation(options)
    applied = script._cutover_operation(_options(
        "cutover-apply",
        (2, 5),
        fingerprint=preview["fingerprint"],
    ))

    assert applied["applied"] is True
    with factory() as session:
        setting = session.scalar(select(TenantAiSetting))
        routes = list(session.scalars(select(TenantAiProviderRouteSet).order_by(
            TenantAiProviderRouteSet.revision,
        )))
        active = [route for route in routes if route.status == "active"]
        items = list(session.scalars(select(TenantAiProviderRouteItem).where(
            TenantAiProviderRouteItem.route_set_id == active[0].id,
        ).order_by(TenantAiProviderRouteItem.priority)))
        audit = session.scalar(select(AuditLog).where(
            AuditLog.action == "原子切换默认AI供应商与路由",
        ))

        assert setting.default_provider_id == 2
        assert setting.ai_provider_route_fallback_enabled is True
        assert len(active) == 1
        assert active[0].revision == 2
        assert [item.provider_id for item in items] == [2, 5]
        assert audit is not None


def test_guarded_cutover_rolls_back_when_audit_write_fails(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    _seed(factory)
    with factory() as session:
        for provider in session.scalars(select(AiProvider)):
            provider.credential_enabled = True
            provider.is_active = True
            provider.health_status = "健康"
        session.commit()

    preview = script._cutover_operation(_options("cutover-preview", (2, 5)))
    monkeypatch.setattr(
        script,
        "_write_cutover_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    with pytest.raises(RuntimeError, match="audit failed"):
        script._cutover_operation(_options(
            "cutover-apply",
            (2, 5),
            fingerprint=preview["fingerprint"],
        ))

    with factory() as session:
        setting = session.scalar(select(TenantAiSetting))
        active = list(session.scalars(select(TenantAiProviderRouteSet).where(
            TenantAiProviderRouteSet.status == "active",
        )))
        assert setting.default_provider_id == 5
        assert setting.ai_provider_route_fallback_enabled is False
        assert [route.revision for route in active] == [1]


def test_production_workflow_exposes_only_guarded_cutover_operations() -> None:
    source = (
        PROJECT_ROOT / ".github/workflows/production-ai-provider-failover.yml"
    ).read_text()

    assert "          - cutover-preview" in source
    assert "          - cutover-apply" in source
    assert "|cutover-preview|cutover-apply|readback)$" in source


def test_guarded_cutover_requires_two_healthy_route_candidates(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    _seed(factory)

    preview = script._cutover_operation(_options("cutover-preview", (5, 2)))
    with pytest.raises(RuntimeError, match="provider_route_not_ready"):
        script._cutover_operation(_options(
            "cutover-apply",
            (5, 2),
            fingerprint=preview["fingerprint"],
        ))

    with factory() as session:
        setting = session.scalar(select(TenantAiSetting))
        active = list(session.scalars(select(TenantAiProviderRouteSet).where(
            TenantAiProviderRouteSet.status == "active",
        )))
        assert setting.default_provider_id == 5
        assert [route.revision for route in active] == [1]


def test_guarded_cutover_rejects_stale_fingerprint_without_writes(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    _seed(factory)
    with factory() as session:
        for provider in session.scalars(select(AiProvider)):
            provider.credential_enabled = True
            provider.is_active = True
            provider.health_status = "健康"
        session.commit()

    preview = script._cutover_operation(_options("cutover-preview", (2, 5)))
    with factory() as session:
        active = session.scalar(select(TenantAiProviderRouteSet).where(
            TenantAiProviderRouteSet.status == "active",
        ))
        active.content_hash = "f" * 64
        session.commit()

    with pytest.raises(RuntimeError, match="fingerprint_mismatch"):
        script._cutover_operation(_options(
            "cutover-apply",
            (2, 5),
            fingerprint=preview["fingerprint"],
        ))

    with factory() as session:
        setting = session.scalar(select(TenantAiSetting))
        active = list(session.scalars(select(TenantAiProviderRouteSet).where(
            TenantAiProviderRouteSet.status == "active",
        )))
        assert setting.default_provider_id == 5
        assert [route.revision for route in active] == [1]


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
