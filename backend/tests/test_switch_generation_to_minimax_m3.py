from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AiProvider, AuditLog, Tenant, TenantAiProviderRouteItem, TenantAiProviderRouteSet
from app.services.task_center.ai_provider_routes import (
    _active_scope_snapshots, resolve_request_route,
)
from scripts import switch_generation_to_minimax_m3 as script


pytestmark = pytest.mark.no_postgres
SHA = "a" * 40


def _seed_provider(session, provider_id):
    session.add(AiProvider(id=provider_id, model_name=script.MODELS[provider_id],
        provider_name=f"provider-{provider_id}", credential_enabled=True, is_active=True,
        provider_type="antigravity_cli" if provider_id in (7, 8) else "openai_compatible",
        base_url=script.SLOT_URL if provider_id in (7, 8) else "http://fixture.invalid",
        api_key_ciphertext="test-cipher", health_status="异常" if provider_id == 4 else "健康"))


def _seed_route(session, purpose):
    route = TenantAiProviderRouteSet(tenant_id=1, purpose=purpose,
        revision=3, status="active", content_hash=f"old-{purpose}")
    session.add(route)
    session.flush()
    ids = [5] if purpose in script.REVIEW_PURPOSES else [8, 7, 4]
    if purpose == "group_realize_adult_product":
        ids.append(5)
    for priority, provider_id in enumerate(ids, 1):
        session.add(TenantAiProviderRouteItem(route_set_id=route.id, priority=priority,
            provider_id=provider_id, model_name=script.MODELS[provider_id],
            timeout_ms=120000, rate_policy={"source": "original-rate"},
            concurrency_policy={"source": "original-concurrency"}))


@pytest.fixture()
def seeded(monkeypatch):
    monkeypatch.setenv("RELEASE_SHA", SHA)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(Tenant(id=1, name="test"))
        for provider_id in script.MODELS:
            _seed_provider(session, provider_id)
        session.flush()
        for purpose in script.PURPOSES:
            _seed_route(session, purpose)
        session.commit()
        options = script.CutoverOptions(deployed_sha=SHA,
            generator_probe_hash=script.row_hash(session.get(AiProvider, 5)),
            reviewer_probe_hash=script.row_hash(session.get(AiProvider, 4)),
            actor="test-operator", approval_ref="test-user-approval")
    yield factory, options
    engine.dispose()


def test_cutover_revises_exact_routes_preserves_old_items_and_frozen_work(seeded):
    factory, options = seeded
    with factory() as session:
        original_items = {row.id: script.row_hash(row)
            for row in session.scalars(select(TenantAiProviderRouteItem))}
        frozen = _active_scope_snapshots(session, 1, "group", content_modes=("general",))
    preview = script.run(options, factory)
    result = script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)

    assert result["applied"] is True and len(result["after"]) == 10
    assert {row["revision"] for row in result["after"]} == {4}
    with factory() as session:
        for item_id, old_hash in original_items.items():
            assert script.row_hash(session.get(TenantAiProviderRouteItem, item_id)) == old_hash
        assert len(list(session.scalars(select(AuditLog)))) == 1
        assert session.get(AiProvider, 4).health_status == "健康"
        assert all(session.get(AiProvider, provider_id).health_status == "异常" for provider_id in (7, 8))
        original = resolve_request_route(session, 1, "两阶段意图规划",
            config={"ai_content_route_v2_enabled": True, "_ai_provider_route_snapshots": frozen})
        assert original.route_set_id == frozen["group_context_route"]["route_set_id"]
        assert original.provider_ids == (8, 7)
        assert len(script.readback_routes(session)) == 10


@pytest.mark.parametrize("change,error", (
    ("sha", "minimax_cutover_release_changed"),
    ("provider", "minimax_cutover_generator_probe_drift"),
    ("route", "minimax_cutover_preview_drift"),
))
def test_exact_preview_drift_has_no_route_or_health_mutations(seeded, monkeypatch, change, error):
    factory, options = seeded
    preview = script.run(options, factory)
    if change == "sha":
        monkeypatch.setenv("RELEASE_SHA", "b" * 40)
    else:
        with factory() as session:
            if change == "provider":
                session.get(AiProvider, 5).provider_name = "concurrent-change"
            else:
                session.scalar(select(TenantAiProviderRouteSet)).content_hash = "concurrent-change"
            session.commit()
    with pytest.raises(RuntimeError, match=error):
        script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        assert len(list(session.scalars(select(TenantAiProviderRouteSet)))) == 10
        assert session.get(AiProvider, 4).health_status == "异常"
        assert list(session.scalars(select(AuditLog))) == []


def test_post_write_validation_error_rolls_back_entire_cutover(seeded, monkeypatch):
    factory, options = seeded
    preview = script.run(options, factory)

    def fail_readback(_session):
        raise RuntimeError("independent_reviewer_validation_failed")

    monkeypatch.setattr(script, "readback_routes", fail_readback)
    with pytest.raises(RuntimeError, match="independent_reviewer_validation_failed"):
        script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        routes = list(session.scalars(select(TenantAiProviderRouteSet)))
        assert len(routes) == 10 and all(route.status == "active" for route in routes)
        assert session.get(AiProvider, 4).health_status == "异常"
        assert session.get(AiProvider, 8).health_status == "健康"
        assert list(session.scalars(select(AuditLog))) == []
