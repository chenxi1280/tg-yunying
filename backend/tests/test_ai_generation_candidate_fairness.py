from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GenerationJob, Task, Tenant
from app.services._common import _now
from app.services.task_center.ai_generation_parallel import _candidate_statement


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="candidate fairness"))
        current.add(Task(id="task", tenant_id=1, name="task", type="group_ai_chat",
                         status="running", fulfillment_contract_version="fact_first_v3"))
        current.commit()
        yield current
    engine.dispose()


def _seed_candidate(session, identity, *, job_state=None, **job_fields):
    action = Action(id=identity, tenant_id=1, task_id="task", task_type="group_ai_chat",
                    action_type="send_message", account_id=11, status="pending",
                    scheduled_at=_now() - timedelta(hours=1), task_lifecycle_epoch=1,
                    obligation_type="coverage", obligation_id=identity,
                    payload={"ai_generation_status": "pending", "message_text": ""})
    session.add(action)
    if job_state:
        session.add(GenerationJob(id=f"job-{identity}", tenant_id=1, task_id="task",
                                 task_lifecycle_epoch=1, obligation_type="coverage",
                                 obligation_id=identity, state=job_state,
                                 generation_sequence=1, context_snapshot_version=1, **job_fields))
    session.commit()
    return action


def test_unknown_jobs_cannot_fill_candidate_window(session):
    for index in range(3):
        _seed_candidate(session, f"unknown-{index}", job_state="unknown")
    ready = _seed_candidate(session, "ready")

    assert list(session.scalars(_candidate_statement(1))) == [ready]
    jobs = list(session.query(GenerationJob))
    assert len(jobs) == 3
    assert all(job.state == "unknown" and job.job_version == 1 for job in jobs)


@pytest.mark.parametrize("case", [
    ("unknown", {}, False),
    ("pending", {"generation_not_before_at": timedelta(minutes=1)}, False),
    ("pending", {"next_retry_at": timedelta(minutes=1)}, False),
    ("pending", {"generation_not_before_at": timedelta(seconds=-1)}, True),
    ("generating", {"lease_expires_at": timedelta(minutes=1)}, False),
    ("generating", {}, False),
    ("generating", {"lease_expires_at": timedelta(seconds=-1)}, True),
])
def test_candidate_respects_existing_job_claimability(session, case):
    state, fields, eligible = case
    now_value = _now()
    values = {key: now_value + offset for key, offset in fields.items()}
    action = _seed_candidate(session, "candidate", job_state=state, **values)

    assert list(session.scalars(_candidate_statement(1))) == ([action] if eligible else [])
