from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from app.database import SessionLocal
from app.models import Action, GenerationJob, Task, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center import ai_generation_recovery


TENANT_ID = 990_301
TASK_ID = "ai-reconcile-concurrency-task"
ACTION_ID = "ai-reconcile-concurrency-action"
JOB_ID = "ai-reconcile-concurrency-job"


def test_expired_generation_job_has_one_reconcile_cas_winner(monkeypatch) -> None:
    _seed_expired_generation()
    barrier = Barrier(2, timeout=10)
    original = ai_generation_recovery._expired_job_ids

    def synchronized_candidates(session, limit, *, task_type=None):
        candidates = original(session, limit, task_type=task_type)
        barrier.wait()
        return candidates

    monkeypatch.setattr(
        ai_generation_recovery,
        "_expired_job_ids",
        synchronized_candidates,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _index: _run_reconcile(), range(2)))

    assert results == [0, 1]
    with SessionLocal() as session:
        job = session.get(GenerationJob, JOB_ID)
        action = session.get(Action, ACTION_ID)
        assert job.state == "pending"
        assert job.generation_owner_id == ""
        assert job.job_version == 3
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "pending"


def _run_reconcile() -> int:
    with SessionLocal() as session:
        result = ai_generation_recovery.reconcile_generation_jobs(session, limit=1)
        session.commit()
        return result


def _seed_expired_generation() -> None:
    now_value = _now()
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="AI reconcile concurrency"))
        session.flush()
        session.add_all([
            TgAccount(
                id=TENANT_ID,
                tenant_id=TENANT_ID,
                display_name="reconcile-account",
                phone_masked="990301",
                status="在线",
            ),
            TgGroup(
                id=TENANT_ID,
                tenant_id=TENANT_ID,
                tg_peer_id="-100990301",
                title="reconcile-group",
            ),
            Task(
                id=TASK_ID,
                tenant_id=TENANT_ID,
                name="AI reconcile concurrency",
                type="group_ai_chat",
                status="running",
                task_lifecycle_epoch=1,
                fulfillment_contract_version="fact_first_v3",
            ),
        ])
        session.flush()
        session.add_all([
            GenerationJob(
                id=JOB_ID,
                tenant_id=TENANT_ID,
                task_id=TASK_ID,
                task_lifecycle_epoch=1,
                obligation_type="quantity_slot",
                obligation_id="reconcile-concurrency-slot",
                generation_sequence=1,
                context_snapshot_version=1,
                state="generating",
                generation_owner_id="expired-worker",
                generation_lease_epoch=4,
                lease_expires_at=now_value - timedelta(minutes=1),
                job_version=1,
            ),
            Action(
                id=ACTION_ID,
                tenant_id=TENANT_ID,
                task_id=TASK_ID,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=TENANT_ID,
                status="executing",
                obligation_type="quantity_slot",
                obligation_id="reconcile-concurrency-slot",
                task_lifecycle_epoch=1,
                claim_owner="expired-worker",
                claim_token="expired-token",
                lease_owner="expired-worker",
                lease_expires_at=now_value - timedelta(minutes=1),
                payload={
                    "group_id": TENANT_ID,
                    "message_text": "",
                    "generation_job_id": JOB_ID,
                    "ai_generation_status": "generating",
                    "ai_generation_claim_owner": "expired-worker",
                    "ai_generation_claim_token": "expired-token",
                },
                result={},
            ),
        ])
        session.commit()
