from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    Action,
    ChannelCommentPlanContract,
    ChannelMessage,
    CommentFulfillmentObligation,
    DiscussionMembershipFact,
    OperationTarget,
    TgAccount,
)
from app.schemas.task_center import ChannelCommentConfig, TaskSettingsUpdate
from app.services.task_center import engagement_comment_participation
from app.services.task_center.channel_comment_plan_contract import ensure_comment_plan_contract
from app.services.task_center.executors.channel_comment_grounding_planner import (
    build_grounding_comment_plan_slots,
)
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres
ACCOUNT_IDS = (101, 102, 103)


def _context(session, task, *, admissible):
    return SimpleNamespace(
        config=task.type_config,
        channel=session.get(OperationTarget, 31),
        messages=[session.get(ChannelMessage, 41)],
        policy_accounts=list(session.scalars(select(TgAccount).order_by(TgAccount.id))),
        ledger=SimpleNamespace(id="unified-day", obligation_local_date=STABLE_PLANNER_NOW.date()),
        participation_plan=None,
        admission_snapshot=SimpleNamespace(id="admission", admissible_account_ids=admissible),
        rule_version=SimpleNamespace(rule_set_id="test-rule", version=1),
    )


def _seed(session, monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    # The source selector has its own persistence tests; exercise its real ratio here.
    monkeypatch.setattr(engagement_comment_participation, "_source_plan", lambda *_a, **_k: None)
    task = seed_comment_task(session, mode="comment", target_count=3)
    _enable_grounding_plan(session, task)
    task.type_config = {**task.type_config, "account_ratio_min_bps": 10000,
                        "account_ratio_max_bps": 10000}
    session.flush()
    return task


@pytest.mark.parametrize("admissible", [[], [101], list(ACCOUNT_IDS)])
def test_unified_plan_denominator_ignores_transient_admission(monkeypatch, admissible):
    with planner_session() as session:
        task = _seed(session, monkeypatch)
        context = _context(session, task, admissible=admissible)
        plan = ensure_comment_plan_contract(
            session, task, context.messages[0], accounts=context.policy_accounts,
            ledger=context.ledger, admission_snapshot=context.admission_snapshot,
        )
        assert plan.contract.eligible_account_count == len(ACCOUNT_IDS)
        assert plan.contract.required_distinct_account_count == len(ACCOUNT_IDS)
        assert set(plan.account_by_ordinal.values()) == set(ACCOUNT_IDS)


def _build_slots(session, task, context):
    return build_grounding_comment_plan_slots(
        session, task, context, input_allowed=lambda *_args: True,
        target_builder=lambda *_args, quantity, **_kwargs: [None] * quantity,
    )


def test_missing_membership_preserves_obligation_and_other_accounts_progress(monkeypatch):
    with planner_session() as session:
        task = _seed(session, monkeypatch)
        missing = session.scalar(select(DiscussionMembershipFact).where(
            DiscussionMembershipFact.account_id == 103,
        ))
        missing.fresh_until_at = STABLE_PLANNER_NOW - timedelta(days=3650)
        context = _context(session, task, admissible=[101, 102])
        slots = _build_slots(session, task, context)
        obligations = list(session.scalars(select(CommentFulfillmentObligation)))
        assert len(obligations) == len(ACCOUNT_IDS), (task.last_error, task.stats)
        assert {slot.obligation.account_id for slot in slots} == {101, 102}
        pending = next(row for row in obligations if row.account_id == 103)
        assert pending.membership_fact_id is None
        frozen_ids = {row.id for row in obligations}

        missing.fresh_until_at = STABLE_PLANNER_NOW + timedelta(days=1)
        resumed = _build_slots(session, task, context)
        assert {slot.obligation.account_id for slot in resumed} == set(ACCOUNT_IDS)
        assert pending.membership_fact_id == missing.id
        assert {row.id for row in session.scalars(select(CommentFulfillmentObligation))} == frozen_ids
        assert len(list(session.scalars(select(ChannelCommentPlanContract)))) == 1


@pytest.mark.parametrize("schema", [ChannelCommentConfig, TaskSettingsUpdate])
def test_comment_schema_accepts_participation_upper_bound(schema):
    target = {"target_channel_id": 31} if schema is ChannelCommentConfig else {}
    config = schema(**target, target_comments_per_message=1061,
                    business_max_comments_per_message=1061)
    assert config.target_comments_per_message == config.business_max_comments_per_message == 1061


@pytest.mark.parametrize("field", ["target_comments_per_message", "business_max_comments_per_message"])
def test_comment_schema_keeps_positive_quantity_requirement(field):
    with pytest.raises(ValueError):
        ChannelCommentConfig(target_channel_id=31, **{field: 0})


def test_all_memberships_unavailable_freezes_full_debt_without_send_slots(monkeypatch):
    with planner_session() as session:
        task = _seed(session, monkeypatch)
        for fact in session.scalars(select(DiscussionMembershipFact)):
            fact.fresh_until_at = STABLE_PLANNER_NOW - timedelta(days=3650)
        assert _build_slots(session, task, _context(session, task, admissible=[])) == []
        plan = session.scalar(select(ChannelCommentPlanContract))
        rows = list(session.scalars(select(CommentFulfillmentObligation)))
        assert plan.eligible_account_count == plan.required_distinct_account_count == 3
        assert len(rows) == 3
        assert all(row.membership_fact_id is None and row.current_action_id is None for row in rows)


def test_membership_refresh_never_changes_a_bound_unknown_action(monkeypatch):
    with planner_session() as session:
        task = _seed(session, monkeypatch)
        context = _context(session, task, admissible=list(ACCOUNT_IDS))
        slots = _build_slots(session, task, context)
        obligation = slots[0].obligation
        action = Action(tenant_id=task.tenant_id, task_id=task.id,
                        task_type=task.type, action_type="post_comment",
                        account_id=obligation.account_id, status="unknown_after_send",
                        payload={"membership_fact_id": obligation.membership_fact_id})
        session.add(action)
        session.flush()
        obligation.current_action_id = action.id
        old_fact_id = obligation.membership_fact_id
        remaining = _build_slots(session, task, context)
        assert obligation.id not in {slot.obligation.id for slot in remaining}
        assert obligation.current_action_id == action.id
        assert obligation.membership_fact_id == action.payload["membership_fact_id"] == old_fact_id
        assert action.status == "unknown_after_send"
