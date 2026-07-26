from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
    AuditLog,
    Tenant,
    TgAccount,
)
from app.services._common import _now


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


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


def test_reconcile_queues_orphan_missing_mask_without_reopening_manual_required() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    with _session() as session:
        manual_account = _seed_operational_account(session, 814)
        orphan_account = _seed_operational_account(session, 815)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add(job)
        session.flush()
        session.add(
            AiAccountVoiceProfileGenerationItem(
                job_id=job.id,
                tenant_id=1,
                account_id=manual_account.id,
                status="manual_required",
                source="recovery",
                idempotency_key="manual-required-814",
                expected_profile_version=1,
            )
        )
        session.commit()

        result = jobs.reconcile_missing_voice_profile_generation(
            session,
            tenant_id=1,
            limit=10,
            actor="voice-profile-reconcile",
        )
        session.commit()
        items = list(
            session.scalars(
                select(AiAccountVoiceProfileGenerationItem).order_by(AiAccountVoiceProfileGenerationItem.account_id)
            )
        )

    assert result.created_account_ids == (orphan_account.id,)
    assert result.manual_required_account_ids == (manual_account.id,)
    assert [(item.account_id, item.status) for item in items] == [
        (manual_account.id, "manual_required"),
        (orphan_account.id, "queued"),
    ]


def test_reconcile_skips_open_or_disabled_profiles_before_paging_to_generic_profile() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    with _session() as session:
        open_account = _seed_operational_account(session, 814)
        generic_account = _seed_operational_account(session, 815)
        disabled_account = _seed_operational_account(session, 816)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add(job)
        session.flush()
        session.add(
            AiAccountVoiceProfileGenerationItem(
                job_id=job.id,
                tenant_id=1,
                account_id=open_account.id,
                status="queued",
                source="recovery",
                idempotency_key="open-814",
                expected_profile_version=1,
            )
        )
        session.add_all(
            [
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=generic_account.id,
                    version=1,
                    status="active",
                    quality_status="active",
                    short_prompt_summary="自然随意真实，偶尔接话",
                ),
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=disabled_account.id,
                    version=1,
                    status="disabled",
                    quality_status="active",
                    short_prompt_summary="男性日常社交账号先看公开反馈，再简短接话，不做绝对推荐",
                ),
            ]
        )
        session.commit()

        result = jobs.reconcile_missing_voice_profile_generation(
            session,
            tenant_id=1,
            limit=1,
            actor="voice-profile-reconcile",
        )
        session.commit()
        queued_ids = list(
            session.scalars(
                select(AiAccountVoiceProfileGenerationItem.account_id)
                .where(AiAccountVoiceProfileGenerationItem.status == "queued")
                .order_by(AiAccountVoiceProfileGenerationItem.account_id)
            )
        )

    assert result.created_account_ids == (generic_account.id,)
    assert queued_ids == [open_account.id, generic_account.id]


def test_voice_profile_precheck_summary_separates_daily_coverage_mask_states() -> None:
    reconcile = importlib.import_module("app.services.task_center.account_voice_profile_generation_reconcile")
    with _session() as session:
        usable = _seed_operational_account(session, 814)
        disabled = _seed_operational_account(session, 815)
        queued = _seed_operational_account(session, 816)
        retry_wait = _seed_operational_account(session, 817)
        manual_required = _seed_operational_account(session, 818)
        missing = _seed_operational_account(session, 819)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add_all(
            [
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=usable.id,
                    version=1,
                    status="active",
                    quality_status="active",
                    short_prompt_summary="男性日常社交账号，先看公开反馈再简短接话",
                ),
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=disabled.id,
                    version=1,
                    status="disabled",
                    quality_status="active",
                    short_prompt_summary="男性日常社交账号，先看公开反馈再简短接话",
                ),
                job,
            ]
        )
        session.flush()
        session.add_all(
            [
                AiAccountVoiceProfileGenerationItem(
                    job_id=job.id,
                    tenant_id=1,
                    account_id=queued.id,
                    status="queued",
                    source="recovery",
                    idempotency_key="queued-816",
                    expected_profile_version=1,
                ),
                AiAccountVoiceProfileGenerationItem(
                    job_id=job.id,
                    tenant_id=1,
                    account_id=retry_wait.id,
                    status="retry_wait",
                    source="recovery",
                    idempotency_key="retry-817",
                    expected_profile_version=1,
                ),
                AiAccountVoiceProfileGenerationItem(
                    job_id=job.id,
                    tenant_id=1,
                    account_id=manual_required.id,
                    status="manual_required",
                    source="recovery",
                    idempotency_key="manual-818",
                    expected_profile_version=1,
                ),
            ]
        )
        session.commit()

        summary = reconcile.voice_profile_precheck_summary(
            session,
            tenant_id=1,
            account_ids=[usable.id, disabled.id, queued.id, retry_wait.id, manual_required.id, missing.id],
        )

    assert summary == {
        "target_account_count": 6,
        "usable_account_count": 1,
        "queued_account_count": 1,
        "retry_wait_account_count": 1,
        "manual_required_account_count": 1,
        "disabled_account_count": 1,
        "missing_account_count": 1,
        "samples": {
            "queued": [queued.id],
            "retry_wait": [retry_wait.id],
            "manual_required": [manual_required.id],
            "disabled": [disabled.id],
            "missing": [missing.id],
        },
    }


def test_manual_retry_creates_linked_generation_item_idempotently() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    with _session() as session:
        account = _seed_operational_account(session)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add(job)
        session.flush()
        previous = AiAccountVoiceProfileGenerationItem(
            job_id=job.id,
            tenant_id=1,
            account_id=account.id,
            status="manual_required",
            source="recovery",
            idempotency_key="manual-required-source",
            expected_profile_version=1,
            attempt_count=4,
        )
        session.add(previous)
        session.commit()

        created = jobs.retry_voice_profile_generation_item(
            session,
            tenant_id=1,
            item_id=previous.id,
            expected_status="manual_required",
            expected_profile_version=1,
            idempotency_key="operator-retry-814",
            reason="已确认供应商配置恢复",
            actor="operator",
        )
        session.commit()
        repeated = jobs.retry_voice_profile_generation_item(
            session,
            tenant_id=1,
            item_id=previous.id,
            expected_status="manual_required",
            expected_profile_version=1,
            idempotency_key="operator-retry-814",
            reason="已确认供应商配置恢复",
            actor="operator",
        )
        rows = list(
            session.scalars(
                select(AiAccountVoiceProfileGenerationItem).order_by(AiAccountVoiceProfileGenerationItem.created_at)
            )
        )
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "账号面具已人工重试"))

    assert created.id == repeated.id
    assert created.previous_item_id == previous.id
    assert created.status == "queued"
    assert created.attempt_count == 0
    assert [(row.id, row.status) for row in rows] == [
        (previous.id, "manual_required"),
        (created.id, "queued"),
    ]
    assert audit is not None and f"previous_item_id={previous.id}" in audit.detail


def test_retry_wait_is_requeued_without_resetting_attempt_count() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    with _session() as session:
        account = _seed_operational_account(session)
        job = AiAccountVoiceProfileGenerationJob(tenant_id=1, source="recovery", requested_by="tester")
        session.add(job)
        session.flush()
        item = AiAccountVoiceProfileGenerationItem(
            job_id=job.id,
            tenant_id=1,
            account_id=account.id,
            status="retry_wait",
            source="recovery",
            idempotency_key="retry-wait-source",
            expected_profile_version=1,
            attempt_count=2,
            next_retry_at=_now() + timedelta(minutes=5),
        )
        session.add(item)
        session.commit()

        retried = jobs.retry_voice_profile_generation_item(
            session,
            tenant_id=1,
            item_id=item.id,
            expected_status="retry_wait",
            expected_profile_version=1,
            idempotency_key="operator-retry-wait-814",
            reason="立即重试",
            actor="operator",
        )
        session.commit()
        session.refresh(retried)

    assert retried.id == item.id
    assert retried.status == "queued"
    assert retried.attempt_count == 2
    assert retried.operator_idempotency_key == "operator-retry-wait-814"


def test_manual_generation_job_creation_is_idempotent() -> None:
    management = importlib.import_module("app.services.task_center.account_voice_profile_generation_management")
    with _session() as session:
        account = _seed_operational_account(session)
        created = management.create_voice_profile_generation_job(
            session,
            tenant_id=1,
            mode="selected",
            account_ids=[account.id],
            rebuild_existing=False,
            reason="运营要求恢复账号面具",
            idempotency_key="manual-job-814",
            actor="operator",
        )
        session.commit()
        repeated = management.create_voice_profile_generation_job(
            session,
            tenant_id=1,
            mode="invalid-repeat-payload",
            account_ids=[],
            rebuild_existing=False,
            reason="该字段不应覆盖首个请求",
            idempotency_key="manual-job-814",
            actor="operator",
        )
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))

    assert created.job.id == repeated.job.id
    assert created.queue.created_account_ids == (account.id,)
    assert item is not None and item.job_id == created.job.id
    assert item.source == "manual_single"


def test_enqueue_missing_mask_creates_one_durable_item_and_dedupes() -> None:
    jobs = importlib.import_module("app.services.task_center.account_voice_profile_generation_jobs")
    with _session() as session:
        account = _seed_operational_account(session)
        first = jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="login_auto",
            actor="tester",
            reason="登录成功后初始化账号面具",
        )
        second = jobs.enqueue_voice_profile_generation(
            session,
            tenant_id=1,
            account_ids=[account.id],
            source="task_precheck",
            actor="planner",
            reason="AI 活跃群预检发现缺少账号面具",
        )
        session.commit()
        item_count = session.scalar(select(func.count(AiAccountVoiceProfileGenerationItem.id)))
        item = session.scalar(select(AiAccountVoiceProfileGenerationItem))

    assert first.created_account_ids == (account.id,)
    assert second.existing_account_ids == (account.id,)
    assert item_count == 1
    assert item is not None
    assert item.status == "queued"
    assert item.source == "login_auto"
