from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountExternalUseHold,
    Action,
    ExecutionAttempt,
    ExternalAccountUsePolicyRevision,
    Task,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
    UnownedOutboundActivityObservation,
)
from app.services.task_center.engagement_unowned_activity import (
    observe_managed_outbound,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session) -> tuple[Task, TgAccountAuthorization]:
    session.add(Tenant(id=1, name="external use"))
    account = TgAccount(
        id=11, tenant_id=1, display_name="managed", username="ManagedUser",
        phone_masked="11", status="在线",
    )
    authorization = TgAccountAuthorization(
        id=21, tenant_id=1, account_id=11, is_current=True,
        telegram_user_id_digest=hashlib.sha256(b"88").hexdigest(),
    )
    group = TgGroup(
        id=7, tenant_id=1, tg_peer_id="-1007", title="group",
        auth_status="已授权运营",
    )
    task = Task(
        id="group-task", tenant_id=1, name="group", type="group_ai_chat",
        status="running",
    )
    session.add_all([
        account, authorization, group, task,
        AccountBehaviorBudgetPolicyRevision(
            tenant_id=1, account_class="normal",
            action_budgets={"total": 20, "authored_message": 10},
        ),
        ExternalAccountUsePolicyRevision(
            tenant_id=1,
            hold_seconds_by_class={"authored_message": 600},
            collision_classes_by_class={
                "authored_message": ["authored_message", "reaction"],
            },
        ),
    ])
    session.flush()
    account.current_authorization_id = authorization.id
    return task, authorization


def test_owned_outbound_is_not_recorded_as_external_use() -> None:
    with _session() as session:
        task, _authorization = _seed(session)
        action = Action(
            id="owned-action", tenant_id=1, task_id=task.id,
            task_type=task.type, action_type="send_message", account_id=11,
            payload={"group_id": 7},
        )
        session.add(action)
        session.flush()
        session.add(ExecutionAttempt(
            tenant_id=1, action_id=action.id, account_id=11,
            status="success", remote_message_id="501",
        ))
        session.flush()

        managed = observe_managed_outbound(
            session, tenant_id=1, canonical_peer_id="-1007",
            payload={"source_message_id": 501, "sender_peer_id": "88"},
            action_class="authored_message", source_event_id="event-501",
        )

        assert managed is True
        assert session.scalar(select(func.count(
            UnownedOutboundActivityObservation.id
        ))) == 0
        assert session.scalar(select(func.count(AccountExternalUseHold.id))) == 0


def test_unowned_outbound_is_idempotent_and_charges_shared_budget() -> None:
    with _session() as session:
        _task, _authorization = _seed(session)
        payload = {"source_message_id": 502, "sender_peer_id": "88"}

        for _ in range(2):
            assert observe_managed_outbound(
                session, tenant_id=1, canonical_peer_id="-1007",
                payload=payload, action_class="authored_message",
                source_event_id="event-502",
            )

        observation = session.scalar(select(UnownedOutboundActivityObservation))
        hold = session.scalar(select(AccountExternalUseHold))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert observation is not None and observation.canonical_peer_id == "-1007"
        assert hold is not None and hold.policy_revision_id
        assert hold.collision_action_classes == ["authored_message", "reaction"]
        assert ledger is not None
        assert ledger.counters["authored_message"]["unowned"] == 1
        assert session.scalar(select(func.count(
            UnownedOutboundActivityObservation.id
        ))) == 1


def test_username_fallback_is_case_insensitive() -> None:
    with _session() as session:
        _task, authorization = _seed(session)
        authorization.telegram_user_id_digest = ""

        assert observe_managed_outbound(
            session, tenant_id=1, canonical_peer_id="-1007",
            payload={"source_message_id": 503, "sender_username": "@manageduser"},
            action_class="authored_message", source_event_id="event-503",
        )
        assert session.scalar(select(UnownedOutboundActivityObservation)) is not None
