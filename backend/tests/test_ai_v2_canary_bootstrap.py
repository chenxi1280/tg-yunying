from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AdultSubjectAttestation,
    AiAccountVoiceProfile,
    AiContentPolicyVersion,
    AiProvider,
    AuditLog,
    Task,
    TaskAiContentPolicyBinding,
    Tenant,
    TenantAiProviderRouteSet,
    TgAccount,
)
from app.services.task_center import ai_v2_canary_bootstrap as service
from app.services.task_center.ai_v2_canary_bootstrap import (
    AiV2BootstrapConflict,
    apply_bootstrap,
    parse_choices,
    preview_bootstrap,
    readback_bootstrap,
)


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_incomplete_preview_lists_user_choices_without_writes() -> None:
    with Session(_engine()) as session:
        _seed(session)
        preview = preview_bootstrap(session, 1, parse_choices({}))

        assert "task_id" in preview["missing_user_choices"]
        assert "allowed_routes" in preview["missing_user_choices"]
        assert (
            "route_items_for_every_required_purpose"
            not in preview["missing_user_choices"]
        )
        assert preview["fingerprint"]
        assert session.scalar(select(func.count(AuditLog.id))) == 0


def test_guarded_bootstrap_applies_policy_routes_and_one_task_atomically() -> None:
    with Session(_engine()) as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)

        assert preview["missing_user_choices"] == []
        assert preview["blockers"] == []
        result = apply_bootstrap(
            session,
            1,
            choices,
            expected_fingerprint=preview["fingerprint"],
        )
        session.commit()

        readback = readback_bootstrap(session, 1, "task-canary")
        assert result["applied"] is True
        assert readback["task"]["route_v2_enabled"] is True
        assert readback["task"]["config_revision"] == 4
        assert readback["policy"]["manifest_id"] == "ai_group_v2_canary_policy_v1"
        assert readback["binding"]["allowed_routes"] == ["general"]
        assert len(readback["routes"]) == 3
        assert readback["production_fixed"] is False
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_reviewer_identity_overlap_blocks_apply_without_writes() -> None:
    with Session(_engine()) as session:
        _seed(session)
        payload = _choices()
        payload["route_items"]["group_semantic_review"][0].update(
            provider_id=1,
            model_name="generator-model",
        )
        choices = parse_choices(payload)
        preview = preview_bootstrap(session, 1, choices)

        assert "semantic_reviewer_identity_overlap" in preview["blockers"]
        with pytest.raises(AiV2BootstrapConflict, match="not_ready"):
            apply_bootstrap(
                session,
                1,
                choices,
                expected_fingerprint=preview["fingerprint"],
            )
        assert session.scalar(select(func.count(AiContentPolicyVersion.id))) == 0


def test_stale_fingerprint_rejects_route_drift_without_writes() -> None:
    with Session(_engine()) as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)
        provider = session.get(AiProvider, 1)
        provider.health_status = "异常"
        session.flush()

        with pytest.raises(AiV2BootstrapConflict, match="fingerprint_mismatch"):
            apply_bootstrap(
                session,
                1,
                choices,
                expected_fingerprint=preview["fingerprint"],
            )
        assert session.scalar(select(func.count(TenantAiProviderRouteSet.id))) == 0


def test_open_unknown_action_blocks_bootstrap() -> None:
    with Session(_engine()) as session:
        _seed(session)
        session.add(
            Action(
                id="unknown-action",
                tenant_id=1,
                task_id="task-canary",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=1,
                status="unknown_after_send",
            )
        )
        session.flush()
        preview = preview_bootstrap(session, 1, parse_choices(_choices()))

        assert "task_open_work_present" in preview["blockers"]
        assert preview["task"]["open_work"]["unknown_count"] == 1


def test_audit_failure_rolls_back_every_bootstrap_write(monkeypatch) -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)
        monkeypatch.setattr(
            service,
            "_write_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("audit failed")
            ),
        )
        with pytest.raises(RuntimeError, match="audit failed"):
            apply_bootstrap(
                session,
                1,
                choices,
                expected_fingerprint=preview["fingerprint"],
            )
        session.rollback()

    with Session(engine) as session:
        task = session.get(Task, "task-canary")
        assert task.config_revision == 3
        assert not task.type_config.get("ai_content_route_v2_enabled")
        assert session.scalar(select(func.count(AiContentPolicyVersion.id))) == 0
        assert session.scalar(select(func.count(TaskAiContentPolicyBinding.id))) == 0


def test_same_approval_and_fingerprint_is_idempotent() -> None:
    with Session(_engine()) as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)
        apply_bootstrap(
            session,
            1,
            choices,
            expected_fingerprint=preview["fingerprint"],
        )
        session.commit()

        repeated = apply_bootstrap(
            session,
            1,
            choices,
            expected_fingerprint=preview["fingerprint"],
        )

        assert repeated["idempotent"] is True
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_adult_route_requires_exact_next_revision_attestation() -> None:
    with Session(_engine()) as session:
        _seed(session)
        attestation = AdultSubjectAttestation(
            tenant_id=1,
            scope_type="task_group",
            scope_id="7",
            subject_class="adult_service",
            evidence_codes=["adult_service_subject_verified"],
            expires_at=datetime.now() + timedelta(days=1),
            task_config_revision=4,
            policy_version=9,
            status="active",
            evidence_hash="c" * 64,
        )
        session.add(attestation)
        session.flush()
        payload = _choices()
        payload["allowed_routes"] = ["adult_service_sensory"]
        payload["attestation_ids"] = [attestation.id]
        payload["route_items"].pop("group_realize_general")
        payload["route_items"]["group_realize_adult_service_sensory"] = _route_item(
            1,
            "generator-model",
        )

        preview = preview_bootstrap(session, 1, parse_choices(payload))

        assert "adult_attestation_not_current" in preview["blockers"]


def test_workflow_exposes_only_guarded_operations_and_exact_sha_gate() -> None:
    source = (
        PROJECT_ROOT / ".github/workflows/production-ai-v2-canary-bootstrap.yml"
    ).read_text()

    assert "          - preview" in source
    assert "          - apply" in source
    assert "          - readback" in source
    assert '[[ "${EXPECTED_SHA}" =~ ^[0-9a-f]{40}$ ]]' in source
    assert 'choices.get("deployed_sha") != os.environ["EXPECTED_SHA"]' in source
    assert "tgyunying-worker-ai-generation-2" in source
    assert "tgyunying-worker-ai-generation-3" in source
    assert "--expected-fingerprint" in source


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(
        TgAccount(
            id=1,
            tenant_id=1,
            display_name="account",
            phone_masked="***",
            status="在线",
            session_ciphertext="session",
        )
    )
    session.add(
        Task(
            id="task-canary",
            tenant_id=1,
            name="AI canary",
            type="group_ai_chat",
            status="draft",
            config_revision=3,
            task_lifecycle_epoch=2,
            account_config={"selection_mode": "selected", "account_ids": [1]},
            type_config={"target_group_id": 7},
        )
    )
    session.add(
        AiAccountVoiceProfile(
            tenant_id=1,
            account_id=1,
            version=1,
            short_prompt_summary="短句，少标点",
            status="active",
            quality_status="active",
        )
    )
    session.add_all(
        (
            _provider(1, "generator"),
            _provider(2, "reviewer"),
        )
    )
    session.commit()


def _provider(provider_id: int, model: str) -> AiProvider:
    return AiProvider(
        id=provider_id,
        provider_name=f"provider-{provider_id}",
        base_url="https://provider.invalid",
        model_name=f"{model}-model",
        api_key_ciphertext="cipher",
        credential_enabled=True,
        is_active=True,
        health_status="健康",
        input_price_per_1k=0.01,
        output_price_per_1k=0.02,
    )


def _route_item(provider_id: int, model_name: str) -> list[dict]:
    return [
        {
            "priority": 1,
            "provider_id": provider_id,
            "model_name": model_name,
            "timeout_ms": 20_000,
            "rate_policy": {"requests_per_minute": 10},
            "concurrency_policy": {"max_concurrent": 2},
        }
    ]


def _choices() -> dict:
    return {
        "deployed_sha": "a" * 40,
        "task_id": "task-canary",
        "expected_task_revision": 3,
        "allowed_routes": ["general"],
        "route_items": {
            "group_context_route": _route_item(1, "router-model"),
            "group_realize_general": _route_item(1, "generator-model"),
            "group_semantic_review": _route_item(2, "reviewer-model"),
        },
        "max_cost_per_slot": 0.5,
        "daily_ai_budget": 10,
        "sampling_manifest_hash": "b" * 64,
        "requester": "requester",
        "approver": "approver",
        "approval_ref": "quality-canary-1",
    }
