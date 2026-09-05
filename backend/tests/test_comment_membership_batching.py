from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.models import Action, ChannelDiscussionGroupBinding, DiscussionMembershipFact, TgAccount
from app.services.task_center.channel_comment_discussion_admission import (
    _membership_dedupe_key,
    discussion_admission_candidate_ids,
)
from app.services.task_center.channel_comment_discussion_contracts import (
    current_membership_fact,
    current_membership_facts,
)
from app.services.task_center.channel_comment_discussion_guard import (
    _ready_memberships,
    discussion_membership_counts,
)
from channel_comment_planner_test_support import STABLE_PLANNER_NOW, planner_session, seed_comment_task
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres
ACCOUNT_IDS = list(range(101, 201))


@contextmanager
def _selects(session):
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)
    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _seed(session):
    task = seed_comment_task(session, mode="comment")
    _enable_grounding_plan(session, task)
    binding = session.scalar(select(ChannelDiscussionGroupBinding))
    facts = {row.account_id: row for row in session.scalars(select(DiscussionMembershipFact))}
    facts[102].fresh_until_at = STABLE_PLANNER_NOW - timedelta(seconds=1)
    facts[103].membership_status, facts[103].can_send = "banned", False
    session.flush()
    session.refresh(task)
    return task, binding


def test_bulk_membership_read_matches_scalar_scope_with_one_select():
    with planner_session() as session:
        _task, binding = _seed(session)
        scope = dict(tenant_id=1, discussion_peer_id=binding.discussion_peer_id,
                     group_binding_id=binding.id)
        with _selects(session) as scalar_queries:
            scalar = {account_id: fact.id for account_id in ACCOUNT_IDS
                      if (fact := current_membership_fact(session, account_id=account_id, **scope))}
        with _selects(session) as batch_queries:
            batch = current_membership_facts(session, account_ids=ACCOUNT_IDS, **scope)
        assert {key: row.id for key, row in batch.items()} == scalar
        assert len(scalar_queries) == len(ACCOUNT_IDS)
        assert len(batch_queries) == 1
        assert current_membership_facts(session, account_ids=ACCOUNT_IDS,
                                        **{**scope, "tenant_id": 2}) == {}
        assert current_membership_facts(session, account_ids=ACCOUNT_IDS,
                                        **{**scope, "group_binding_id": "older-binding"}) == {}


def test_plan_readiness_and_counts_keep_missing_expired_and_banned_distinct():
    with planner_session() as session:
        task, binding = _seed(session)
        accounts = [SimpleNamespace(id=account_id) for account_id in ACCOUNT_IDS]
        with _selects(session) as queries:
            ready = _ready_memberships(session, task, binding=binding,
                                      accounts=accounts, now_value=STABLE_PLANNER_NOW)
            counts = discussion_membership_counts(session, task, binding,
                                                  account_ids=ACCOUNT_IDS, now_value=STABLE_PLANNER_NOW)
        assert set(ready) == {101}
        assert counts == {"discussion_membership_ready_count": 1,
                          "discussion_admission_required_count": 97,
                          "discussion_forbidden_count": 1,
                          "discussion_membership_unknown_count": 1}
        assert len(queries) == 2


def test_batch_admission_candidates_keep_unknown_action_excluded():
    with planner_session() as session:
        task, binding = _seed(session)
        session.add(TgAccount(id=104, tenant_id=1, display_name="测试账号", phone_masked="test"))
        task.type_config = {**task.type_config, "auto_join_discussion_enabled": True,
                            "discussion_join_account_ids": ACCOUNT_IDS, "discussion_join_budget": 100,
                            "discussion_join_pacing_policy_version": "test-v1",
                            "discussion_join_pacing_policy": {"interval_seconds": 60}}
        unknown = Action(tenant_id=1, task_id=task.id, task_type=task.type, account_id=104,
                         action_type="ensure_discussion_membership", status="unknown_after_send",
                         action_dedupe_key=_membership_dedupe_key(task, binding, 104))
        session.add(unknown)
        session.flush()
        with _selects(session) as queries:
            candidates = discussion_admission_candidate_ids(
                session, task, binding,
                accounts=[SimpleNamespace(id=value) for value in ACCOUNT_IDS], now_value=STABLE_PLANNER_NOW,
            )
        assert candidates == frozenset(range(105, 201))
        assert unknown.status == "unknown_after_send"
        assert len(queries) == 2
