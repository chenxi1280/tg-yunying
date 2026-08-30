from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AiProvider, Tenant
from scripts import configure_antigravity_providers as script


pytestmark = pytest.mark.no_postgres


def test_provider_apply_is_idempotent_and_preserves_healthy_state(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    monkeypatch.setattr(script, "_advisory_lock", lambda _session: None)
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.commit()

    token = "bridge-token-value"
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    preview_options = script.Options("preview", 1, "", "pytest", "incident", "a" * 40)
    preview = script._snapshot(preview_options, token=token, lock=False)
    apply_options = script.Options(
        "apply", 1, preview["fingerprint"], "pytest", "incident", "a" * 40,
    )
    assert script._apply(apply_options, token) is True
    with factory() as session:
        rows = list(session.scalars(select(AiProvider).order_by(AiProvider.id)))
        ciphertexts = [row.api_key_ciphertext for row in rows]
        for row in rows:
            row.health_status = "健康"
            row.last_error = ""
        session.commit()

    second_preview = script._snapshot(preview_options, token=token, lock=False)
    second_options = script.Options(
        "apply", 1, second_preview["fingerprint"], "pytest", "incident", "a" * 40,
    )
    assert script._apply(second_options, token) is False
    with factory() as session:
        rows = list(session.scalars(select(AiProvider).order_by(AiProvider.id)))
        assert [row.api_key_ciphertext for row in rows] == ciphertexts
        assert [row.health_status for row in rows] == ["健康", "健康"]


def test_provider_preview_fingerprint_binds_bridge_token(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    options = script.Options("preview", 1, "", "pytest", "incident", "a" * 40)
    first = script._snapshot(options, token="token-a", lock=False)
    second = script._snapshot(options, token="token-b", lock=False)
    assert first["fingerprint"] != second["fingerprint"]
    assert "token-a" not in str(first)


def test_provider_preview_rejects_duplicate_provider_rows(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    name, model = script.PROVIDERS[0]
    with factory() as session:
        session.add_all((
            AiProvider(
                provider_name=name, model_name=model,
                base_url=script.BASE_URL, api_key_ciphertext="a",
            ),
            AiProvider(
                provider_name=name, model_name=model,
                base_url=script.BASE_URL, api_key_ciphertext="b",
            ),
        ))
        session.commit()
    options = script.Options("preview", 1, "", "pytest", "incident", "a" * 40)
    with pytest.raises(RuntimeError, match="antigravity_provider_duplicate"):
        script._snapshot(options, token="token", lock=False)


def test_provider_preview_rejects_same_identity_under_another_name(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(script, "SessionLocal", factory)
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    desired_name, model = script.PROVIDERS[0]
    with factory() as session:
        session.add_all((
            AiProvider(
                provider_name=desired_name, provider_type=script.PROVIDER_TYPE,
                model_name=model, base_url=script.BASE_URL, api_key_ciphertext="a",
            ),
            AiProvider(
                provider_name="alias-name", provider_type=script.PROVIDER_TYPE,
                model_name=model,
                base_url="http://HOST.DOCKER.INTERNAL:18101/",
                api_key_ciphertext="b",
            ),
        ))
        session.commit()
    options = script.Options("preview", 1, "", "pytest", "incident", "a" * 40)
    with pytest.raises(RuntimeError, match="identity_duplicate"):
        script._snapshot(options, token="token", lock=False)
