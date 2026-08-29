from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GenerationJob, Task, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center.ai_generation_contract_errors import (
    GenerationContractErrorTarget,
    terminate_generation_contract_error,
)
from app.services.task_center.ai_generation_recovery import reconcile_generation_jobs
from app.services.task_center.ai_generation_parallel import ParallelGenerationClaim
from app.services.task_center.ai_generation_parallel_settlement import settle_parallel_outcome
from app.services.task_center.ai_generation_worker_types import GenerationOutcome


pytestmark = pytest.mark.no_postgres


def test_reconcile_uses_current_job_action_not_historical_terminal() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-current", "slot-1", state="generating", owner="worker-old")
        old = _action("action-old", "slot-1", "job-old", status="failed")
        current = _action(
            "action-current", "slot-1", job.id, status="executing", owner="worker-old",
        )
        session.add_all([job, old, current])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        assert session.get(Action, current.id).status == "pending"
        assert session.get(Action, old.id).status == "failed"
        refreshed = session.get(GenerationJob, job.id)
        assert refreshed.state == "pending"
        assert refreshed.generation_stage == "generation_recovery"


def test_reconcile_cancels_actionless_job_instead_of_orphan_pending() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-orphan", "slot-orphan", state="generating", owner="worker-old")
        session.add(job)
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed = session.get(GenerationJob, job.id)
        assert refreshed.state == "cancelled"
        assert refreshed.generation_stage == "action_missing"


def test_reconcile_does_not_overwrite_action_reclaimed_by_other_owner() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-raced", "slot-raced", state="generating", owner="worker-old")
        action = _action(
            "action-raced", "slot-raced", job.id,
            status="executing", owner="recovery-new",
        )
        action.payload.update({
            "ai_generation_claim_owner": "worker-old",
            "ai_generation_claim_token": "token-worker-old",
        })
        session.add_all([job, action])
        session.commit()

        with pytest.raises(RuntimeError, match="action_claim_changed"):
            reconcile_generation_jobs(session, limit=10)
        session.rollback()

        assert session.get(GenerationJob, job.id).generation_owner_id == "worker-old"
        assert session.get(Action, action.id).claim_owner == "recovery-new"


def test_unknown_job_without_same_attempt_cache_remains_open() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-unknown", "slot-unknown", state="unknown")
        action = _action(
            "action-unknown", "slot-unknown", job.id, status="pending",
            generation_status="ai_result_persist_unknown",
        )
        action.payload["ai_generation_attempt_id"] = "attempt-1"
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 0
        session.commit()

        assert session.get(GenerationJob, job.id).state == "unknown"
        assert session.get(Action, action.id).status == "pending"


def test_unknown_job_requeues_only_same_attempt_cached_result() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-cached", "slot-cached", state="unknown")
        action = _action(
            "action-cached", "slot-cached", job.id, status="pending",
            generation_status="ai_result_persist_unknown",
        )
        action.payload.update({
            "ai_generation_attempt_id": "attempt-2",
            "ai_generation_request_id": "request-2",
            "content_scope_contract_version": "group_content_scope_v1",
            "content_scope_tenant_id": 1,
            "content_scope_group_id": 7,
            "content_scope_task_id": "task-1",
            "ai_generation_result_cache": {
                "attempt_id": "attempt-2",
                "request_id": "request-2",
                "content": "已生成但待持久化",
                "content_scope_contract_version": "group_content_scope_v1",
                "content_scope_tenant_id": 1,
                "content_scope_group_id": 7,
                "content_scope_task_id": "task-1",
            },
        })
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed = session.get(GenerationJob, job.id)
        assert refreshed.state == "pending"
        assert refreshed.generation_stage == "persist_retry"


def test_unknown_job_rejects_cached_result_from_other_scope() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-wrong-cache", "slot-wrong-cache", state="unknown")
        action = _action(
            "action-wrong-cache", "slot-wrong-cache", job.id, status="pending",
            generation_status="ai_result_persist_unknown",
        )
        action.payload.update({
            "ai_generation_attempt_id": "attempt-3",
            "ai_generation_request_id": "request-3",
            "content_scope_contract_version": "group_content_scope_v1",
            "content_scope_tenant_id": 1,
            "content_scope_group_id": 7,
            "content_scope_task_id": "task-1",
            "ai_generation_result_cache": {
                "attempt_id": "attempt-3",
                "request_id": "request-3",
                "content": "其他群的结果",
                "content_scope_contract_version": "group_content_scope_v1",
                "content_scope_tenant_id": 1,
                "content_scope_group_id": 8,
                "content_scope_task_id": "task-1",
            },
        })
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 0
        session.commit()
        assert session.get(GenerationJob, job.id).state == "unknown"


def test_contract_error_does_not_overwrite_reclaimed_job() -> None:
    engine = _engine()
    factory = lambda: Session(engine)
    with factory() as session:
        _seed_scope(session)
        action = _action(
            "action-fenced", "slot-fenced", "job-fenced",
            status="executing", owner="worker-old",
        )
        job = _job("job-fenced", "slot-fenced", state="generating", owner="worker-new")
        session.add_all([action, job])
        session.commit()

    with pytest.raises(RuntimeError, match="job_claim_lost"):
        terminate_generation_contract_error(
            factory,
            GenerationContractErrorTarget(
                "action-fenced", "worker-old", "token-worker-old", "job-fenced",
            ),
            ValueError("provider secret response"),
        )

    with factory() as session:
        assert session.get(Action, "action-fenced").status == "executing"
        assert session.get(GenerationJob, "job-fenced").generation_owner_id == "worker-new"


def test_contract_error_persists_type_and_hash_without_raw_detail() -> None:
    engine = _engine()
    factory = lambda: Session(engine)
    with factory() as session:
        _seed_scope(session)
        action = _action(
            "action-error", "slot-error", "job-error",
            status="executing", owner="worker-1",
        )
        job = _job("job-error", "slot-error", state="generating", owner="worker-1")
        session.add_all([action, job])
        session.commit()

    terminate_generation_contract_error(
        factory,
        GenerationContractErrorTarget(
            "action-error", "worker-1", "token-worker-1", "job-error",
        ),
        ValueError("provider secret response"),
    )

    with factory() as session:
        action = session.get(Action, "action-error")
        assert action.status == "failed"
        assert action.result["error_type"] == "ValueError"
        assert len(action.result["error_fingerprint"]) == 64
        assert "error_message" not in action.result
        assert "provider secret response" not in str(action.result)
        assert session.get(GenerationJob, "job-error").state == "failed"


def test_parallel_success_rejects_zero_release_without_prepared_action() -> None:
    engine = _engine()
    factory = lambda: Session(engine)
    with factory() as session:
        _seed_scope(session)
        job = _job("job-zero-release", "slot-zero-release", state="generating", owner="worker-old")
        action = _action(
            "action-zero-release", "slot-zero-release", job.id, status="pending",
        )
        session.add_all([job, action])
        session.commit()

    claim = ParallelGenerationClaim(
        "action-zero-release", "job-zero-release",
        "worker-old", "token-worker-old", 1, 0,
    )
    with pytest.raises(RuntimeError, match="parallel_generation_ready_action_invalid"):
        settle_parallel_outcome(factory, claim, GenerationOutcome())

    with factory() as session:
        job = session.get(GenerationJob, "job-zero-release")
        assert job.state == "generating"
        assert job.generation_owner_id == "worker-old"


def test_parallel_success_accepts_idempotent_prepared_action_readback() -> None:
    engine = _engine()
    factory = lambda: Session(engine)
    content = "已持久化的生成结果"
    with factory() as session:
        _seed_scope(session)
        job = _job("job-ready-readback", "slot-ready-readback", state="generating", owner="worker-old")
        action = _action(
            "action-ready-readback", "slot-ready-readback", job.id, status="pending",
        )
        action.payload.update({
            "message_text": content,
            "ai_generation_status": "ready",
            "ai_generation_claim_owner": "",
            "ai_generation_claim_token": "",
        })
        action.candidate_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        session.add_all([job, action])
        session.commit()

    claim = ParallelGenerationClaim(
        "action-ready-readback", "job-ready-readback",
        "worker-old", "token-worker-old", 1, 0,
    )
    assert settle_parallel_outcome(factory, claim, GenerationOutcome()) == 0

    with factory() as session:
        assert session.get(GenerationJob, "job-ready-readback").state == "ready"


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_scope(session: Session) -> None:
    session.add_all([
        Tenant(id=1, name="tenant"),
        Task(
            id="task-1", tenant_id=1, name="AI", type="group_ai_chat",
            status="running", task_lifecycle_epoch=1,
            fulfillment_contract_version="fact_first_v3",
        ),
        TgAccount(
            id=11, tenant_id=1, display_name="account", phone_masked="+86111",
            status="在线",
        ),
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="group"),
    ])
    session.commit()


def _job(job_id: str, obligation_id: str, *, state: str, owner: str = "") -> GenerationJob:
    return GenerationJob(
        id=job_id,
        tenant_id=1,
        task_id="task-1",
        task_lifecycle_epoch=1,
        obligation_type="quantity_slot",
        obligation_id=obligation_id,
        generation_sequence=1,
        context_snapshot_version=1,
        state=state,
        generation_owner_id=owner,
        lease_expires_at=(
            _now() - timedelta(minutes=5) if state == "generating" else None
        ),
        job_version=1,
    )


def _action(
    action_id: str,
    obligation_id: str,
    job_id: str,
    *,
    status: str,
    owner: str = "",
    generation_status: str = "generating",
) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status=status,
        obligation_type="quantity_slot",
        obligation_id=obligation_id,
        task_lifecycle_epoch=1,
        action_version=1,
        claim_owner=owner,
        claim_token=f"token-{owner}" if owner else "",
        lease_owner=owner,
        lease_expires_at=_now() - timedelta(minutes=5) if owner else None,
        payload={
            "group_id": 7,
            "message_text": "",
            "generation_job_id": job_id,
            "ai_generation_status": generation_status,
            "ai_generation_claim_owner": owner,
            "ai_generation_claim_token": f"token-{owner}" if owner else "",
        },
        result={},
    )
