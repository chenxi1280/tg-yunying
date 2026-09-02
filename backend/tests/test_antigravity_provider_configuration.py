from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AiProvider
from app.schemas import AiProviderCreate, AiProviderUpdate
from app.services.ai_config import (
    ANTIGRAVITY_MODELS as CONFIG_MODELS,
    _validate_ai_provider_boundary,
    _validate_antigravity_bridge_credentials,
    create_ai_provider,
    update_ai_provider,
)
from app.services.antigravity_provider_client import ANTIGRAVITY_MODELS as CLIENT_MODELS
from app.services.task_center.antigravity_schemas import antigravity_schema_for_purpose
from scripts.antigravity_provider_server import ALLOWED_MODELS, AntigravityRuntime, BridgeConfig
from scripts.configure_ai_provider_generation_cutover import (
    ANTIGRAVITY_MODELS as CUTOVER_MODELS,
)
from scripts.configure_antigravity_providers import PROVIDERS


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_model_catalog_is_consistent_across_release_boundaries():
    expected = ("gemini-3.6-flash-medium", "gemini-3.1-pro-low")
    assert CLIENT_MODELS == CONFIG_MODELS == ALLOWED_MODELS == frozenset(expected)
    assert CUTOVER_MODELS == expected
    assert tuple(model for _name, model in PROVIDERS) == expected
    probe_source = (
        PROJECT_ROOT / "deploy/check-antigravity-provider-slot.py"
    ).read_text()
    probe_tree = ast.parse(probe_source)
    probe_models = next(
        ast.literal_eval(node.value)
        for node in probe_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MODELS" for target in node.targets)
    )
    assert probe_models == expected


def test_brief_schema_requires_v2_claim_and_reply_binding():
    schema = antigravity_schema_for_purpose("group_context_route")
    item = schema["properties"]["briefs"]["items"]["oneOf"][0]
    assert {"reply_to_message_id", "claims"}.issubset(item["required"])
    claim = item["properties"]["claims"]["items"]
    assert set(claim["required"]) == {"category", "speech_act", "evidence_ids"}
    assert item["additionalProperties"] is False


def test_brief_schema_rejects_invalid_enums_and_extra_fields():
    schema = antigravity_schema_for_purpose("group_context_route")
    item = schema["properties"]["briefs"]["items"]["oneOf"][0]
    assert item["additionalProperties"] is False
    assert "invented" not in item["properties"]["speech_act"]["enum"]
    categories = {
        variant["properties"]["claims"]["items"]["properties"]["category"]["enum"][0]
        for variant in schema["properties"]["briefs"]["items"]["oneOf"]
    }
    assert "grounded_reaction" in categories


@pytest.mark.parametrize(
    ("mode", "expected_categories", "expected_lengths"),
    [
        ("general", {"grounded_reaction", "fact_question", "agreement"}, {"micro", "short", "medium"}),
        ("adult_visual", {"adult_visual_reaction", "adult_visual_question"}, {"micro", "short"}),
        ("adult_product", {"adult_product_reaction", "adult_product_question"}, {"micro", "short"}),
        ("adult_service_inquiry", {
            "price_question", "region_question", "availability_question",
            "service_question", "duration_question", "identity_question", "booking_question",
        }, {"micro", "short"}),
        ("adult_service_sensory", {"sensory_reaction", "sensory_question"}, {"micro", "short"}),
    ],
)
def test_planner_schema_freezes_each_mode_contract(mode, expected_categories, expected_lengths):
    config = {"_ai_provider_planner_slots": [{
        "slot_id": "slot-1", "reply_to_message_id": "",
        "content_mode": mode, "route_evidence_ids": ["f1", "f2"],
    }]}
    schema = antigravity_schema_for_purpose("group_context_route", config)
    variants = schema["properties"]["briefs"]["items"]["oneOf"]
    categories = {
        item["properties"]["claims"]["items"]["properties"]["category"]["enum"][0]
        for item in variants
    }
    assert categories == expected_categories
    assert {value for item in variants for value in item["properties"]["length_band"]["enum"]} == expected_lengths
    assert all(item["properties"]["reply_to_message_id"]["enum"] == [""] for item in variants)
    assert all(item["properties"]["anchor_ids"]["items"]["enum"] == ["f1", "f2"] for item in variants)


def test_realizer_schema_rejects_extra_fields():
    schema = antigravity_schema_for_purpose("group_realize_general")
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "content", "used_anchor_ids", "speech_act", "voice_profile_version",
    }


def test_realizer_schema_freezes_brief_contract():
    schema = antigravity_schema_for_purpose("group_realize_general", {
        "_ai_provider_realizer_contract": {
            "speech_act": "reaction", "anchor_ids": ["f1"],
            "voice_profile_version": "voice-v3",
        },
    })
    assert schema["properties"]["speech_act"]["enum"] == ["reaction"]
    assert schema["properties"]["used_anchor_ids"]["items"]["enum"] == ["f1"]
    assert schema["properties"]["voice_profile_version"]["enum"] == ["voice-v3"]


def test_health_exposes_operational_facts_without_credentials(tmp_path: Path):
    health = AntigravityRuntime(_runtime_config(tmp_path)).health()
    expected = {"bridge_version", "process_state", "auth_probe_age_seconds", "last_terminal_code"}
    assert expected <= health.keys()
    assert "token" not in json.dumps(health).lower()


def test_health_requires_fresh_success_for_both_models(tmp_path: Path):
    binary = tmp_path / "agy"
    binary.write_text("binary")
    config = _runtime_config(tmp_path)
    runtime = AntigravityRuntime(BridgeConfig(**{**config.__dict__, "agy_bin": binary}))
    runtime.cli_version = "agy-test"
    runtime.confirmed_models.update({
        "gemini-3.6-flash-medium", "gemini-3.1-pro-low",
    })
    runtime.last_confirmed_at = __import__("time").time()
    runtime.last_terminal_code = "confirmed"
    assert runtime.health()["status"] == "ready"
    runtime.last_confirmed_at -= runtime.health()["probe_max_age_seconds"] + 1
    assert runtime.health()["status"] == "degraded"
    runtime.last_confirmed_at = __import__("time").time()
    runtime.last_terminal_code = "antigravity_auth_required"
    assert runtime.health()["status"] == "degraded"


def test_terminate_ignores_already_exited_process(tmp_path: Path, monkeypatch):
    runtime = AntigravityRuntime(_runtime_config(tmp_path))

    def already_exited(*_args):
        raise ProcessLookupError

    monkeypatch.setattr("os.killpg", already_exited)
    runtime._terminate(SimpleNamespace(pid=123))


def test_bridge_credential_validation_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(
        "app.services.ai_config.ai_gateway.check",
        lambda _credentials: (False, "antigravity_bridge_unauthorized"),
    )
    with pytest.raises(ValueError, match="bridge_validation_failed"):
        _validate_antigravity_bridge_credentials(
            "antigravity_cli", "http://host.docker.internal:18101",
            "gemini-3.6-flash-medium", "wrong-token",
        )


def test_antigravity_create_and_update_fail_before_write_on_wrong_token(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.ai_config.ai_gateway.check",
        lambda _credentials: (False, "antigravity_bridge_unauthorized"),
    )
    create_payload = AiProviderCreate(
        provider_name="Antigravity invalid", provider_type="antigravity_cli",
        base_url="http://host.docker.internal:18101",
        model_name="gemini-3.6-flash-medium", api_key="wrong-token",
        is_billable=False,
    )
    with factory() as session:
        with pytest.raises(ValueError, match="bridge_validation_failed"):
            create_ai_provider(session, create_payload, "pytest")
        assert session.query(AiProvider).count() == 0
        provider = AiProvider(
            provider_name="existing", provider_type="openai_compatible",
            base_url="https://provider.invalid", model_name="model",
            api_key_ciphertext=Fernet.generate_key().decode(),
        )
        session.add(provider)
        session.commit()
        update_payload = AiProviderUpdate(
            provider_type="antigravity_cli",
            base_url="http://host.docker.internal:18101",
            model_name="gemini-3.1-pro-low", api_key="wrong-token",
        )
        with pytest.raises(ValueError, match="bridge_validation_failed"):
            update_ai_provider(session, provider.id, update_payload, "pytest")
        session.refresh(provider)
        assert provider.provider_type == "openai_compatible"


def test_antigravity_create_rejects_same_identity_under_another_name(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.ai_config.ai_gateway.check", lambda _credentials: (True, "ready"),
    )
    base = {
        "provider_type": "antigravity_cli",
        "base_url": "http://host.docker.internal:18101",
        "model_name": "gemini-3.6-flash-medium",
        "api_key": "bridge-token", "is_billable": False,
    }
    with factory() as session:
        create_ai_provider(session, AiProviderCreate(provider_name="first", **base), "pytest")
        duplicate = {
            **base, "base_url": "http://HOST.DOCKER.INTERNAL:18101/",
        }
        with pytest.raises(ValueError, match="identity_duplicate"):
            create_ai_provider(
                session, AiProviderCreate(provider_name="different-name", **duplicate), "pytest",
            )
        assert session.query(AiProvider).count() == 1


@pytest.mark.parametrize(
    ("url", "model", "header"),
    [
        ("https://example.com:18101", "gemini-3.6-flash-medium", "Authorization"),
        ("http://host.docker.internal:18199", "gemini-3.6-flash-medium", "Authorization"),
        ("http://host.docker.internal:18101", "gemini-3.5-flash-medium", "Authorization"),
        ("http://host.docker.internal:18101", "gemini-3.7-flash", "Authorization"),
        ("http://host.docker.internal:18101", "gemini-3.6-flash-medium", "X-API-Key"),
    ],
)
def test_antigravity_provider_boundary_rejects_ssrf_and_unfrozen_identity(url, model, header):
    with pytest.raises(ValueError):
        _validate_ai_provider_boundary("antigravity_cli", url, model, header)


@pytest.mark.parametrize("model", ["gemini-3.6-flash-medium", "gemini-3.1-pro-low"])
def test_antigravity_provider_boundary_accepts_frozen_internal_slot(model):
    _validate_ai_provider_boundary(
        "antigravity_cli", "http://host.docker.internal:18101",
        model, "Authorization",
    )


def _runtime_config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(
        slot_id="slot-01", token="token", agy_bin=Path("/usr/local/bin/agy"),
        ledger_path=tmp_path / "requests.sqlite3",
        ledger_key=Fernet.generate_key().decode("ascii"), max_timeout_seconds=180,
    )
