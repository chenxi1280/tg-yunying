from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, FulfillmentObligationProjection, Task, Tenant
from app.services.task_center.fulfillment_obligation_materialization import (
    rebind_projection,
)


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 992_018
TASK_ID = "materialization-cas-task"
OBLIGATION_TYPE = "quantity_slot"
OBLIGATION_ID = "materialization-cas-obligation"
OLD_ACTION_ID = "materialization-cas-old"
WINNER_ACTION_ID = "materialization-cas-winner"
LOSER_ACTION_ID = "materialization-cas-loser"


def test_cas_loser_converges_to_open_winner() -> None:
    _seed_scope()
    try:
        with SessionLocal() as loser_session:
            loser = loser_session.get(Action, LOSER_ACTION_ID)
            _bind_identity(loser)
            projection = _projection(loser_session)
            _commit_winner(WINNER_ACTION_ID)

            result = rebind_projection(loser_session, loser, projection)
            assert result is False
            assert loser.status == "skipped"
            assert loser.result["error_code"] == "duplicate_open_obligation"
            assert loser.result["existing_action_id"] == WINNER_ACTION_ID

        with SessionLocal() as session:
            projection = _projection(session)
            assert projection.active_action_id == WINNER_ACTION_ID
            assert projection.version == 2
    finally:
        _cleanup()


def test_cas_loser_accepts_same_action_winner() -> None:
    _seed_scope()
    try:
        with SessionLocal() as loser_session:
            winner = loser_session.get(Action, WINNER_ACTION_ID)
            projection = _projection(loser_session)
            _commit_winner(WINNER_ACTION_ID)

            assert rebind_projection(loser_session, winner, projection) is True
            assert winner.status == "pending"
            assert winner.materialization_version == 2
    finally:
        _cleanup()


def test_cas_loser_converges_to_closed_projection() -> None:
    _seed_scope()
    try:
        with SessionLocal() as loser_session:
            loser = loser_session.get(Action, LOSER_ACTION_ID)
            _bind_identity(loser)
            projection = _projection(loser_session)
            _close_projection()

            result = rebind_projection(loser_session, loser, projection)
            assert result is False
            assert loser.status == "skipped"
            assert loser.result["error_code"] == "obligation_not_open"
            assert loser.result["obligation_state"] == "fulfilled"
    finally:
        _cleanup()


def test_cas_loser_keeps_unexplained_conflict_visible() -> None:
    _seed_scope()
    try:
        with SessionLocal() as loser_session:
            loser = loser_session.get(Action, LOSER_ACTION_ID)
            _bind_identity(loser)
            projection = _projection(loser_session)
            _advance_projection_without_valid_winner()

            with pytest.raises(
                ValueError,
                match="fulfillment_obligation_materialization_conflict",
            ):
                rebind_projection(loser_session, loser, projection)
    finally:
        _cleanup()


def _commit_winner(action_id: str) -> None:
    with SessionLocal() as session:
        action = session.get(Action, action_id)
        projection = _projection(session)
        assert rebind_projection(session, action, projection) is True
        session.commit()


def _close_projection() -> None:
    with SessionLocal() as session:
        projection = _projection(session)
        projection.state = "fulfilled"
        projection.version = 2
        session.commit()


def _advance_projection_without_valid_winner() -> None:
    with SessionLocal() as session:
        projection = _projection(session)
        projection.version = 2
        session.commit()


def _projection(session) -> FulfillmentObligationProjection:
    return session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == OBLIGATION_TYPE,
        FulfillmentObligationProjection.obligation_id == OBLIGATION_ID,
    ))


def _seed_scope() -> None:
    _cleanup()
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="materialization cas test"))
        session.flush()
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="materialization cas",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="fact_first_v3",
        ))
        session.flush()
        session.add_all([
            _action(OLD_ACTION_ID, status="failed"),
            _action(WINNER_ACTION_ID),
            _action(LOSER_ACTION_ID, bind_identity=False),
        ])
        session.add(FulfillmentObligationProjection(
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            obligation_type=OBLIGATION_TYPE,
            obligation_id=OBLIGATION_ID,
            work_lane="generation",
            active_action_id=OLD_ACTION_ID,
            materialization_version=1,
            version=1,
        ))
        session.commit()


def _action(
    action_id: str,
    *,
    status: str = "pending",
    bind_identity: bool = True,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        task_type="group_ai_chat",
        action_type="send_message",
        status=status,
        obligation_type=OBLIGATION_TYPE if bind_identity else None,
        obligation_id=OBLIGATION_ID if bind_identity else None,
        execution_lane="generation",
    )


def _bind_identity(action: Action) -> None:
    action.obligation_type = OBLIGATION_TYPE
    action.obligation_id = OBLIGATION_ID


def _cleanup() -> None:
    with SessionLocal() as session:
        session.query(Task).filter(Task.id == TASK_ID).delete(
            synchronize_session=False,
        )
        session.query(Tenant).filter(Tenant.id == TENANT_ID).delete(
            synchronize_session=False,
        )
        session.commit()
