from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.database import SessionLocal
from app.models import AccountPacingReservation, Action, Task, Tenant, TgAccount
from app.services.task_center.direct_action_claims import _candidate_rows


NOW = datetime(2026, 8, 18, 10, 0)


def test_postgres_claims_future_action_whose_source_deadline_is_exhausted() -> None:
    with SessionLocal() as session:
        _seed_future_deadline_action(session)
        rows = _candidate_rows(
            session,
            limit=10,
            now=NOW,
            exclude_task_ids=None,
            execution_lane=None,
        )
        index_names = {
            str(index["name"])
            for index in inspect(session.get_bind()).get_indexes(
                "account_pacing_reservations"
            )
        }

        assert [row.action_id for row in rows] == ["pg-deadline-action"]
        assert "ix_account_pacing_reservation_action_state" in index_names


def _seed_future_deadline_action(session) -> None:
    tenant_id = 990_154
    task = Task(
        id="pg-deadline-task",
        tenant_id=tenant_id,
        name="deadline candidate",
        type="channel_view",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    action = Action(
        id="pg-deadline-action",
        tenant_id=tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=tenant_id,
        status="pending",
        scheduled_at=NOW + timedelta(days=3),
        release_not_before_at=NOW + timedelta(days=3),
    )
    session.add(Tenant(id=tenant_id, name="deadline candidate"))
    session.add(TgAccount(
        id=tenant_id,
        tenant_id=tenant_id,
        display_name="deadline candidate",
        phone_masked="***0154",
        status="在线",
    ))
    session.add(task)
    session.add(action)
    session.flush()
    session.add(AccountPacingReservation(
        tenant_id=tenant_id,
        task_id=task.id,
        account_id=tenant_id,
        pacing_slot_key="pg-deadline-slot",
        policy_version="account_soft_pacing_v1",
        due_at=NOW,
        release_not_before_at=action.release_not_before_at,
        effective_claim_at=action.release_not_before_at,
        source_deadline_at=NOW + timedelta(days=1),
        action_id=action.id,
        state="bound",
    ))
    session.flush()
