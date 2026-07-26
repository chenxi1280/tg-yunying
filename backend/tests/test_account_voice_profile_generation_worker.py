from __future__ import annotations

import importlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationAttempt,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
    AuditLog,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now


pytestmark = pytest.mark.no_postgres


def _seed_operational_account(session: Session, account_id: int = 814) -> TgAccount:
    if session.get(Tenant, 1) is None:
        session.add(Tenant(id=1, name="默认运营空间"))
    if session.get(AccountPool, 1) is None:
        session.add(AccountPool(id=1, tenant_id=1, name="普通账号池", pool_purpose="normal", is_default=True))
    account = TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        account_identity="normal",
        display_name="奶盖加满",
        phone_masked=f"138****{account_id:04d}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=f"session-{account_id}",
    )
    session.add(account)
    session.commit()
    return account


def test_voice_profile_worker_generates_an_active_lightweight_profile() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        account_id = account.id
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account_id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def generate_one(session: Session, item) -> int:  # noqa: ANN001
        session.add(
            AiAccountVoiceProfile(
                tenant_id=item.tenant_id,
                account_id=item.account_id,
                version=item.expected_profile_version,
                mask_name="谨慎数码男生",
                audience_archetype="男性日常社交用户",
                identity_frame="成年男性日常社交观察者",
                preference_tags=["数码", "反馈"],
                interaction_habits=["先看反馈", "再简短接话", "不催促"],
                forbidden_expressions=["绝对靠谱", "闭眼冲", "包你满意"],
                short_prompt_summary="男性日常社交账号先看公开反馈，再简短接话，不做绝对推荐",
                status="active",
                quality_status="active",
                source="voice_profile_generation",
            )
        )
        session.flush()
        return item.expected_profile_version

    processed = worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=generate_one,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempt = session.scalar(select(AiAccountVoiceProfileGenerationAttempt))
        profile = session.scalar(select(AiAccountVoiceProfile))
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "账号面具生成成功"))

    assert processed == 1
    assert item is not None and item.status == "succeeded"
    assert attempt is not None and attempt.outcome == "succeeded"
    assert profile is not None and profile.account_id == account_id
    assert audit is not None and f"account_id={account_id}" in audit.detail


def test_voice_profile_worker_recovers_persist_unknown_from_committed_profile() -> None:
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add(job)
        session.flush()
        item = AiAccountVoiceProfileGenerationItem(
            job_id=job.id,
            tenant_id=1,
            account_id=account.id,
            status="persist_unknown",
            source="recovery",
            idempotency_key="persist-unknown-814",
            expected_profile_version=1,
            attempt_count=1,
            next_retry_at=_now() - timedelta(seconds=1),
            lease_owner="crashed-worker",
        )
        session.add(item)
        session.flush()
        session.add(
            AiAccountVoiceProfileGenerationAttempt(
                tenant_id=1,
                job_id=job.id,
                item_id=item.id,
                attempt_no=1,
                stage="generate",
                outcome="running",
            )
        )
        session.add(
            AiAccountVoiceProfile(
                tenant_id=1,
                account_id=account.id,
                version=1,
                status="active",
                quality_status="active",
                short_prompt_summary="男性日常社交账号先看公开反馈，再简短接话，不做绝对推荐",
            )
        )
        session.commit()

    def should_not_generate(_session: Session, _item) -> int:  # noqa: ANN001
        pytest.fail("already committed profile must not trigger another AI call")

    processed = worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=should_not_generate,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempt = session.scalar(select(AiAccountVoiceProfileGenerationAttempt))

    assert processed == 1
    assert item is not None and item.status == "succeeded"
    assert item.result_profile_version == 1
    assert attempt is not None and attempt.outcome == "succeeded"


def test_voice_profile_worker_reconciles_orphan_before_generating() -> None:
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        account_id = account.id

    def generate_one(session: Session, item) -> int:  # noqa: ANN001
        session.add(
            AiAccountVoiceProfile(
                tenant_id=item.tenant_id,
                account_id=item.account_id,
                version=item.expected_profile_version,
                short_prompt_summary="男性日常社交账号先看公开反馈，再简短接话，不做绝对推荐",
                status="active",
                quality_status="active",
            )
        )
        session.flush()
        return item.expected_profile_version

    processed = worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=generate_one,
        worker_id="test-worker",
        reconcile_interval_seconds=0,
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))

    assert processed == 1
    assert item is not None and item.account_id == account_id
    assert item.source == "daily_reconcile"
    assert item.status == "succeeded"


def test_voice_profile_worker_persists_malformed_output_for_retry() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def malformed_output(_session: Session, _item) -> int:  # noqa: ANN001
        raise json.JSONDecodeError("Expecting property name", "{broken", 1)

    processed = worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=malformed_output,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempt = session.scalar(select(AiAccountVoiceProfileGenerationAttempt))

    assert processed == 1
    assert item is not None and item.status == "retry_wait"
    assert item.error_code == "voice_profile_output_malformed"
    assert item.next_retry_at is not None
    assert attempt is not None and attempt.outcome == "retry_wait"
    assert attempt.error_code == "voice_profile_output_malformed"
    assert attempt.prompt_feedback_summary.startswith("voice_profile_output_malformed:")


def test_voice_profile_worker_rate_limit_defers_without_a_provider_attempt() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    limits = importlib.import_module("app.services.task_center.account_voice_profile_generation_limits")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def should_not_generate(_session: Session, _item) -> int:  # noqa: ANN001
        pytest.fail("provider rate limit must prevent the AI call")

    def rate_limited(_session: Session, _item):  # noqa: ANN001
        raise limits.VoiceProfileProviderRateLimitedError("7", 7)

    processed = worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=should_not_generate,
        reserve_provider=rate_limited,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempts = session.scalar(select(func.count(AiAccountVoiceProfileGenerationAttempt.id)))

    assert processed == 1
    assert item is not None and item.status == "retry_wait"
    assert item.error_code == "voice_profile_provider_rate_limited"
    assert item.attempt_count == 0
    assert attempts == 0


def test_voice_profile_worker_records_limiter_outage_as_provider_unavailable() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    limits = importlib.import_module("app.services.task_center.account_voice_profile_generation_limits")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def unavailable(_session: Session, _item):  # noqa: ANN001
        raise limits.VoiceProfileProviderLimiterUnavailableError("7", "redis timeout")

    worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=lambda _session, _item: pytest.fail("provider must not be called"),
        reserve_provider=unavailable,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempt = session.scalar(select(AiAccountVoiceProfileGenerationAttempt))

    assert item is not None and item.status == "retry_wait"
    assert item.error_code == "voice_profile_provider_unavailable"
    assert attempt is not None and attempt.provider == "7"
    assert attempt.outcome == "retry_wait"


def test_voice_profile_worker_marks_four_failed_attempts_manual_required() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = _seed_operational_account(session)
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def temporarily_unavailable(_session: Session, _item) -> int:  # noqa: ANN001
        raise RuntimeError("network temporarily unavailable")

    for _ in range(4):
        worker.drain_voice_profile_generation(
            lambda: Session(engine),
            limit=1,
            generate_one=temporarily_unavailable,
            worker_id="test-worker",
        )
        with Session(engine) as session:
            item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
            assert item is not None
            if item.status == "retry_wait":
                item.next_retry_at = _now() - timedelta(seconds=1)
                session.commit()

    with Session(engine) as session:
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))
        attempt_count = session.scalar(select(func.count(AiAccountVoiceProfileGenerationAttempt.id)))
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "账号面具生成需要人工处理"))

    assert item is not None and item.status == "manual_required"
    assert item.attempt_count == 4
    assert attempt_count == 4
    assert audit is not None


def test_voice_profile_worker_releases_blocked_daily_coverage_after_success() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    worker = importlib.import_module("app.services.task_center.account_voice_profile_generation_worker")
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    timestamp = _now()
    with Session(engine) as session:
        account = _seed_operational_account(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="运营群"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account.id, can_send=True))
        session.add(
            Task(
                id="voice-profile-coverage",
                tenant_id=1,
                name="面具恢复覆盖",
                type="group_ai_chat",
                status="running",
                last_error="账号面具待恢复，受影响账号已隔离并等待自动重建",
                stats={"voice_profile_missing_account_count": 1},
            )
        )
        session.add(
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id="voice-profile-coverage",
                group_id=7,
                account_id=account.id,
                coverage_date=timestamp.date(),
                state="blocked",
                blocker_code="voice_profile_missing",
                blocker_stage="voice_profile",
                targeted_at=timestamp,
            )
        )
        jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="recovery",
            actor="tester",
            reason="恢复缺失账号面具",
        )
        session.commit()

    def generate_one(session: Session, item) -> int:  # noqa: ANN001
        session.add(
            AiAccountVoiceProfile(
                tenant_id=item.tenant_id,
                account_id=item.account_id,
                version=item.expected_profile_version,
                short_prompt_summary="男性日常社交账号先看公开反馈，再简短接话，不做绝对推荐",
                status="active",
                quality_status="active",
            )
        )
        session.flush()
        return item.expected_profile_version

    worker.drain_voice_profile_generation(
        lambda: Session(engine),
        limit=1,
        generate_one=generate_one,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        row = session.scalar(select(TaskAccountDailyCoverage))
        task = session.get(Task, "voice-profile-coverage")

    assert row is not None and row.state == "ready"
    assert row.blocker_code == ""
    assert task is not None and task.next_run_at is not None
    assert task.last_error == ""
    assert task.stats["voice_profile_missing_account_count"] == 0
