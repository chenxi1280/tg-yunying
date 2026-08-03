from __future__ import annotations

from app.database import SessionLocal
from app.models import Action, Task, Tenant
from app.services.task_center.ai_generation_parallel import _generation_job


TENANT_ID = 991338
TASK_ID = "ai-generation-partial-index-postgres"
ACTION_ID = "ai-generation-partial-index-action"


def test_postgres_generation_job_conflict_matches_partial_unique_index() -> None:
    _seed_action()
    try:
        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            first = _generation_job(session, action)
            session.commit()
            first_id = first.id

        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            second = _generation_job(session, action)
            session.commit()
            assert second.id == first_id
    finally:
        _cleanup()


def _seed_action() -> None:
    _cleanup()
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="generation partial index test"))
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="generation partial index",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="fact_first_v3",
        ))
        session.add(Action(
            id=ACTION_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            task_type="group_ai_chat",
            action_type="send_message",
            status="pending",
            obligation_type="quantity_slot",
            obligation_id="generation-partial-index-obligation",
        ))
        session.commit()


def _cleanup() -> None:
    with SessionLocal() as session:
        session.query(Task).filter(Task.id == TASK_ID).delete(
            synchronize_session=False,
        )
        session.query(Tenant).filter(Tenant.id == TENANT_ID).delete(
            synchronize_session=False,
        )
        session.commit()
