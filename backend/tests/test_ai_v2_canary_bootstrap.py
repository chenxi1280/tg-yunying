from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    AdultSubjectAttestation,
    AiAccountVoiceProfile,
    AiContentPolicyVersion,
    AiProvider,
    AuditLog,
    GenerationJob,
    Task,
    TaskAiContentPolicyBinding,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TenantAiProviderRouteSet,
    TgAccount,
)
from app.services.task_center import ai_v2_canary_bootstrap as service
from app.services.task_center import ai_v2_canary_bootstrap_read as read_service
from app.services.task_center import service as task_service
from app.services.task_center.ai_v2_canary_bootstrap import (
    AiV2BootstrapConflict,
    apply_bootstrap,
    parse_choices,
    preview_bootstrap,
    readback_bootstrap,
)
from app.services.task_center.service import pause_task


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
    with Session(_engine(), autoflush=False) as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)

        assert preview["missing_user_choices"] == []
        assert preview["blockers"] == []
        assert preview["task"]["legacy_ai_provider_id"] == 1
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
        assert readback["policy"]["manifest_id"] == "ai_group_v2_canary_policy_v2"
        assert readback["binding"]["allowed_routes"] == ["general"]
        assert "ai_provider_id" not in session.get(Task, "task-canary").type_config
        assert len(readback["routes"]) == 3
        policy = session.scalar(select(AiContentPolicyVersion))
        lexicon = policy.gate_config["negative_lexicon"]
        assert lexicon["version"] == "generic_filler_v1"
        assert {item["phrase"] for item in lexicon["entries"]} >= {
            "签到",
            "打卡",
            "大家心情好",
        }
        assert all(item["scope"] == "output" for item in lexicon["entries"])
        assert readback["production_fixed"] is False
        assert session.scalar(select(func.count(AuditLog.id))) == 1
        audit = session.scalar(select(AuditLog))
        assert json.loads(audit.detail)["removed_legacy_ai_provider_id"] == 1


def test_postgres_bootstrap_uses_tenant_transaction_lock() -> None:
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _RecordingSession:
        statement = None
        params = None

        @staticmethod
        def get_bind():
            return _Bind()

        def execute(self, statement, params):  # noqa: ANN001
            self.statement = statement
            self.params = params

    session = _RecordingSession()
    service._lock_bootstrap_scope(session, 7)

    assert "pg_advisory_xact_lock" in str(session.statement)
    assert isinstance(session.params["lock_key"], int)


def test_locked_bootstrap_snapshot_does_not_lock_all_voice_accounts(
    monkeypatch,
) -> None:
    with Session(_engine()) as session:
        _seed(session)
        task = session.get(Task, "task-canary")
        voice = {
            "account_count": 1,
            "ready_count": 1,
            "missing_count": 0,
            "missing_ids_hash": "voice-hash",
        }
        monkeypatch.setattr(
            read_service,
            "_voice_snapshot",
            lambda _session, _task: voice,
        )

        snapshot = read_service.task_snapshot(session, task, lock=True)

        assert snapshot["voice"] == voice


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


def test_pause_cancels_safe_group_ai_work_before_epoch_change() -> None:
    with Session(_engine(), autoflush=False) as session:
        _seed(session)
        task = session.get(Task, "task-canary")
        task.status = "running"
        ledger = TaskDayLedger(
            id="pause-ledger",
            tenant_id=1,
            task_id=task.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=datetime(2026, 8, 25).date(),
            period_start_at=datetime(2026, 8, 25),
            deadline_at=datetime(2026, 8, 26),
            day_phase="active",
            planning_anchor_at=datetime(2026, 8, 25),
        )
        quantity = TaskGroupDailyMessageSlot(
            id="pause-quantity",
            tenant_id=1,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="quantity",
            slot_ordinal=1,
            pacing_plan_hash="a" * 64,
            pacing_slot_ordinal=0,
            pacing_plan_total=10,
            pacing_due_at=datetime(2026, 8, 25, 1),
            release_not_before_at=datetime(2026, 8, 25, 2),
            task_lifecycle_epoch=2,
            pacing_period_key=ledger.id,
            pacing_source_key_hash="b" * 64,
        )
        session.add_all([ledger, quantity])
        pending_action = Action(
            id="pending-action",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=1,
            status="pending",
            task_lifecycle_epoch=2,
            primary_quantity_slot_id=quantity.id,
        )
        reservation = AccountPacingReservation(
            tenant_id=1,
            task_id=task.id,
            account_id=1,
            pacing_slot_key="ai:pause-quantity",
            policy_version="account_soft_pacing_v1",
            due_at=datetime(2026, 8, 25, 1),
            release_not_before_at=datetime(2026, 8, 25, 2),
            effective_claim_at=datetime(2026, 8, 25, 2),
            action_id=pending_action.id,
            state="bound",
        )
        session.add_all([pending_action, reservation])
        session.add(
            GenerationJob(
                id="pending-job",
                tenant_id=1,
                task_id=task.id,
                task_lifecycle_epoch=2,
                obligation_type="group_ai_chat",
                obligation_id="pending-obligation",
                generation_sequence=1,
                context_snapshot_version=1,
                state="pending",
            )
        )
        session.commit()

        paused = pause_task(session, 1, task.id, "operator")
        preview = preview_bootstrap(session, 1, parse_choices(_choices()))

        assert paused.status == "paused"
        assert paused.task_lifecycle_epoch == 3
        assert session.get(Action, "pending-action") is None
        assert reservation.action_id is None
        assert reservation.state == "reserved"
        assert session.get(GenerationJob, "pending-job").state == "cancelled"
        assert quantity.task_lifecycle_epoch is None
        assert quantity.release_not_before_at is None
        assert quantity.pacing_due_at == datetime(2026, 8, 25, 1)
        audit_row = session.scalar(select(AuditLog).where(
            AuditLog.action == "暂停任务中心任务",
        ))
        assert "released_pacing_owners=1" in audit_row.detail
        assert preview["task"]["open_work"]["total"] == 0


def test_pause_preserves_unknown_group_ai_work_for_reconciliation() -> None:
    with Session(_engine()) as session:
        _seed(session)
        task = session.get(Task, "task-canary")
        task.status = "running"
        session.add(
            Action(
                id="unknown-action-on-pause",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=1,
                status="unknown_after_send",
                task_lifecycle_epoch=2,
            )
        )
        session.commit()

        pause_task(session, 1, task.id, "operator")
        preview = preview_bootstrap(session, 1, parse_choices(_choices()))

        assert session.get(Action, "unknown-action-on-pause") is not None
        assert "task_open_work_present" in preview["blockers"]


def test_resume_preserves_existing_pacing_anchor(monkeypatch) -> None:
    original_anchor = "2026-08-25T02:16:54.287388"
    resumed_at = datetime(2026, 8, 25, 14, 22, 26)
    with Session(_engine()) as session:
        _seed(session)
        task = session.get(Task, "task-canary")
        task.status = "paused"
        task.stats = {
            "started_at": original_anchor,
            "pacing_anchor_at": original_anchor,
        }
        monkeypatch.setattr(task_service, "_now", lambda: resumed_at)

        task_service._mark_task_started(session, task)

        assert task.status == "running"
        assert task.stats["pacing_anchor_at"] == original_anchor
        assert task.stats["started_at"] == original_anchor


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
            type_config={"target_group_id": 7, "ai_provider_id": 1},
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
