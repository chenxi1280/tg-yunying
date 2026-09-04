from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountFleetActivityFactProjection,
    AccountFleetActivityLedger,
    AccountFleetActivityPolicyRevision,
    AccountPool,
    AccountPoolConcurrencyLease,
    Action,
    ConversationTurnClaim,
    FulfillmentFactProjectionState,
    FulfillmentRemoteFact,
    InteractionOpportunity,
    PostSendVisibilityObservation,
    PostSendVisibilityPolicyRevision,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.engagement_fleet_activity import (
    ensure_fleet_activity_policy,
    project_operation_fact,
    project_visible_authored_action,
    project_fulfillment_fact_activity,
    recover_fleet_activity_projections,
)


pytestmark = pytest.mark.no_postgres
OBSERVED_AT = datetime(2026, 9, 5, 2, tzinfo=timezone.utc)


def test_view_fact_projects_once_and_does_not_claim_authored_activity() -> None:
    with _session() as session:
        task, action = _task_action(
            session, "view-task", "channel_view", action_type="view_message"
        )
        ledger = TaskDayLedger(
            id="task-day", tenant_id=1, task_id=task.id,
            timezone_snapshot="Asia/Shanghai", timezone_revision=1,
            obligation_local_date=date(2026, 9, 5),
            period_start_at=OBSERVED_AT, deadline_at=OBSERVED_AT + timedelta(days=1),
            day_phase="daytime", planning_anchor_at=OBSERVED_AT,
        )
        obligation = ViewFulfillmentObligation(
            id="view-obligation", tenant_id=1, task_day_ledger_id=ledger.id,
            channel_message_id=41, account_id=11, current_action_id=action.id,
        )
        fact = ViewRemoteFact(
            id="view-fact", tenant_id=1, obligation_id=obligation.id,
            obligation_local_date=date(2026, 9, 5), target_peer_id="-1001",
            channel_message_id=41, account_id=11, remote_confirmed_at=OBSERVED_AT,
        )
        session.add_all([ledger, obligation, fact])
        session.flush()

        assert project_operation_fact(session, fact)
        assert not project_operation_fact(session, fact)

        fleet = session.scalar(select(AccountFleetActivityLedger))
        assert fleet.activity_counts == {"passive_operation": 1}
        assert fleet.qualified_activity_classes == ["passive_operation"]
        assert fleet.required_status == {"any_confirmed_business_operation": True}
        assert session.scalar(select(func.count(
            AccountFleetActivityFactProjection.id
        ))) == 1


def test_reaction_fact_is_a_separate_visible_reaction_class() -> None:
    with _session() as session:
        task, action = _task_action(
            session, "like-task", "channel_like", action_type="like_message"
        )
        obligation = ReactionFulfillmentObligation(
            id="reaction-obligation", tenant_id=1, task_id=task.id,
            channel_message_id=41, account_id=11, reaction_contract_version=1,
            current_action_id=action.id,
        )
        fact = ReactionRemoteFact(
            id="reaction-fact", tenant_id=1, obligation_id=obligation.id,
            target_peer_id="-1001", channel_message_id=41, account_id=11,
            reaction_state_revision="reaction-v1",
            reaction_evidence_hash="reaction-evidence",
            remote_confirmed_at=OBSERVED_AT,
        )
        session.add_all([obligation, fact])
        session.flush()

        assert project_operation_fact(session, fact)
        fleet = session.scalar(select(AccountFleetActivityLedger))
        assert fleet.activity_counts == {"visible_reaction": 1}
        assert "authored_content" not in fleet.qualified_activity_classes


def test_visible_proactive_content_is_not_human_linked() -> None:
    with _session() as session:
        _task, action = _task_action(
            session, "group-task", "group_ai_chat", action_type="send_message"
        )
        _visibility(session, action, state="visible_confirmed")

        assert project_visible_authored_action(session, action) == 1

        ledger = session.scalar(select(AccountFleetActivityLedger))
        assert ledger.activity_counts == {"authored_content": 1}


def test_visible_external_human_reply_projects_authored_and_human_linked() -> None:
    with _session() as session:
        task, action = _task_action(
            session, "group-task", "group_ai_chat", action_type="send_message"
        )
        observation = _visibility(session, action, state="visible_confirmed")
        opportunity = InteractionOpportunity(
            id="opportunity", tenant_id=1, task_id=task.id,
            task_lifecycle_epoch=1, context_turn_id="turn", anchor_event_id="event",
            relation_kind="native_reply_external_human",
            natural_not_before_at=OBSERVED_AT,
            freshness_deadline_at=OBSERVED_AT + timedelta(minutes=2),
        )
        claim = ConversationTurnClaim(
            id="claim", tenant_id=1, context_turn_id="turn",
            interaction_opportunity_id=opportunity.id, task_id=task.id,
            task_lifecycle_epoch=1, account_id=11, action_id=action.id,
            state="served", settled_at=OBSERVED_AT,
        )
        session.add_all([opportunity, claim])
        session.flush()

        assert observation.state == "visible_confirmed"
        assert project_visible_authored_action(session, action) == 2
        assert project_visible_authored_action(session, action) == 0

        ledger = session.scalar(select(AccountFleetActivityLedger))
        assert ledger.activity_counts == {
            "authored_content": 1,
            "human_linked_interaction": 1,
        }


def test_pending_visibility_cannot_create_fleet_activity() -> None:
    with _session() as session:
        _task, action = _task_action(
            session, "group-task", "group_ai_chat", action_type="send_message"
        )
        _visibility(session, action, state="visibility_pending")

        assert project_visible_authored_action(session, action) == 0
        assert session.scalar(select(func.count(AccountFleetActivityLedger.id))) == 0


def test_missing_frozen_pool_provenance_fails_explicitly() -> None:
    with _session() as session:
        task, action = _task_action(
            session, "group-task", "group_ai_chat", action_type="send_message",
            with_lease=False,
        )
        _visibility(session, action, state="visible_confirmed")

        with pytest.raises(RuntimeError, match="fleet_activity_pool_provenance_missing"):
            project_visible_authored_action(session, action)
        assert task.type_config["engagement_contract_version"] == "unified_engagement_v1"


def test_runtime_projection_failure_does_not_rollback_remote_fact_and_recovers() -> None:
    with _session() as session:
        _task, action = _task_action(
            session, "group-task", "group_ai_chat", action_type="send_message",
            with_lease=False,
        )
        _visibility(session, action, state="visible_confirmed")
        fact = _generic_remote_fact(session, action)

        assert project_fulfillment_fact_activity(session, fact) == 0
        state = _fleet_state(session, fact)
        assert state.state == "failed"
        assert "fleet_activity_pool_provenance_missing" in state.last_error
        assert session.get(FulfillmentRemoteFact, fact.fact_id) is fact

        _lease(session, action)
        state.next_retry_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert recover_fleet_activity_projections(session, limit=10) == 1
        assert state.state == "projected"
        ledger = session.scalar(select(AccountFleetActivityLedger))
        assert ledger.activity_counts == {"authored_content": 1}


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="互动组"))
    session.add(TgAccount(
        id=11, tenant_id=1, pool_id=1, display_name="账号11",
        phone_masked="11", status="在线",
    ))
    session.flush()
    ensure_fleet_activity_policy(session, tenant_id=1, account_pool_id=1)
    session.commit()
    return session


def _task_action(
    session: Session,
    task_id: str,
    task_type: str,
    *,
    action_type: str,
    with_lease: bool = True,
) -> tuple[Task, Action]:
    task = Task(
        id=task_id, tenant_id=1, name=task_id, type=task_type, status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    )
    action = Action(
        id=f"{task_id}-action", tenant_id=1, task_id=task.id,
        task_type=task_type, action_type=action_type, account_id=11,
        task_lifecycle_epoch=1, status="success",
    )
    session.add_all([task, action])
    session.flush()
    if with_lease:
        _lease(session, action)
    return task, action


def _lease(session: Session, action: Action) -> None:
    session.add(AccountPoolConcurrencyLease(
        tenant_id=1, policy_revision_id="runtime-policy", account_pool_id=1,
        task_id=action.task_id, account_id=11, action_id=action.id,
        attempt_id=f"{action.task_id}-attempt", invocation_identity=action.id,
        task_group_share_limit=1, state="released",
    ))
    session.flush()


def _visibility(session: Session, action: Action, *, state: str):
    policy = PostSendVisibilityPolicyRevision(tenant_id=1, revision=1)
    session.add(policy)
    session.flush()
    observation = PostSendVisibilityObservation(
        tenant_id=1, policy_revision_id=policy.id, action_id=action.id,
        attempt_id=f"{action.id}-attempt", remote_message_id="901",
        target_peer="-1001", state=state, checked_at=OBSERVED_AT,
        deadline_at=OBSERVED_AT + timedelta(seconds=15),
    )
    session.add(observation)
    session.flush()
    return observation


def _generic_remote_fact(session: Session, action: Action) -> FulfillmentRemoteFact:
    fact = FulfillmentRemoteFact(
        fact_id="generic-fact", tenant_id=1, task_type=action.task_type,
        task_id=action.task_id, obligation_type="test", obligation_id="test",
        action_id=action.id, attempt_id=f"{action.id}-attempt",
        mutation_kind=action.action_type, remote_mutation_key_hash="mutation",
        gateway_request_hash="request", fact_kind="remote_message_observed",
        fact_identity_hash="identity", observed_at=OBSERVED_AT,
    )
    state = FulfillmentFactProjectionState(
        fact_id=fact.fact_id, projection_kind="fleet_activity",
        next_retry_at=OBSERVED_AT,
    )
    session.add_all([fact, state])
    session.flush()
    return fact


def _fleet_state(session: Session, fact: FulfillmentRemoteFact):
    return session.scalar(select(FulfillmentFactProjectionState).where(
        FulfillmentFactProjectionState.fact_id == fact.fact_id,
        FulfillmentFactProjectionState.projection_kind == "fleet_activity",
    ))
