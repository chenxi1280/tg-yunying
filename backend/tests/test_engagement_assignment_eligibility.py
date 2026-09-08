"""Actual participation entry points must never allocate known invalid accounts."""
from datetime import datetime, timezone

import pytest

from app.models import TaskDayLedger, TgAccount, TgAccountAuthorization, TgAccountOnlineState
from sqlalchemy import select
from app.services.task_center.engagement_participation import (
    ensure_daily_participation_plan, ensure_source_participation_plan, selected_accounts_for_plan,
)
from tests.test_engagement_participation import _seed, _session

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)


def _invalidate(session, account, kind):
    if kind == "frozen":
        account.telegram_frozen = True  # Deliberately retain the misleading online status.
    elif kind == "expired":
        account.status = "Session失效"
    elif kind == "missing":
        account.session_ciphertext = ""
    elif kind == "authorization_invalid":
        authorization = TgAccountAuthorization(tenant_id=1, account_id=account.id,
            is_current=True, status="active", health_status="invalid", session_ciphertext="invalid-current")
        session.add(authorization)
        session.flush()
        account.current_authorization_id = authorization.id
    else:
        session.add(TgAccountOnlineState(tenant_id=1, account_id=account.id,
            online_status="blocked", failure_type="account_health_probe_failed"))
    session.flush()


def _plan(session, task):
    ledger = session.scalar(select(TaskDayLedger).where(TaskDayLedger.task_id == task.id))
    if ledger is None:
        ledger = TaskDayLedger(tenant_id=1, task_id=task.id, timezone_snapshot="Asia/Shanghai",
            timezone_revision=1, obligation_local_date=NOW.date(), period_start_at=NOW,
            deadline_at=NOW, day_phase="full", planning_anchor_at=NOW)
        session.add(ledger)
        session.flush()
    if task.type == "channel_like":
        return ensure_source_participation_plan(session, task, ledger,
            source_identity="channel:101:message:1", required_count=4)
    return ensure_daily_participation_plan(session, task, ledger)


@pytest.mark.parametrize("task_type", ("group_ai_chat", "channel_comment", "channel_like", "channel_view"))
@pytest.mark.parametrize("invalid", ("frozen", "expired", "missing", "authorization_invalid", "unobserved_blocked"))
def test_new_plan_filters_invalid_accounts_before_counting(task_type, invalid):
    with _session() as session:
        task = _seed(session)
        task.type = task_type
        _invalidate(session, session.get(TgAccount, 11), invalid)
        plan = _plan(session, task)
        assert set(plan.policy_eligible_account_ids) == {12, 13, 14}
        assert 11 not in plan.selected_account_ids
        expected_count = 2 if task_type == "channel_view" else 3
        assert plan.required_count == expected_count


@pytest.mark.parametrize("invalid", ("frozen", "expired", "authorization_invalid", "unobserved_blocked"))
def test_existing_selected_is_history_not_permission_for_new_work(invalid):
    with _session() as session:
        task = _seed(session)
        task.type = "channel_comment"
        plan = _plan(session, task)
        before = list(plan.selected_account_ids)
        _invalidate(session, session.get(TgAccount, 11), invalid)
        assert {account.id for account in selected_accounts_for_plan(session, task, plan)} == {12, 13, 14}
        assert plan.selected_account_ids == before
        assert _plan(session, task).id == plan.id


def test_no_valid_accounts_cannot_freeze_zero_person_completion():
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
    from app.models import TaskParticipationUnitPlan, Action, GenerationJob
    with _session() as session:
        task = _seed(session)
        for identity in (11, 12, 13, 14):
            _invalidate(session, session.get(TgAccount, identity), "frozen")
        with pytest.raises(RuntimeResourceBlocked) as caught:
            _plan(session, task)
        assert caught.value.code == "no_eligible_accounts"
        assert task.stats["account_assignment_eligibility"]["eligible_count"] == 0
        assert session.query(TaskParticipationUnitPlan).count() == 0
        assert session.query(Action).count() == session.query(GenerationJob).count() == 0


@pytest.mark.parametrize("action_type", ("send_message", "post_comment", "view_message", "like_message", "ensure_target_membership"))
def test_actual_factory_rejects_invalid_account_without_creating_action(action_type):
    from app.services.task_center.payloads import _create_action, SendMessagePayload
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
    from app.models import Action
    with _session() as session:
        task = _seed(session)
        _invalidate(session, session.get(TgAccount, 11), "frozen")
        with pytest.raises(RuntimeResourceBlocked):
            _create_action(session, task, action_type, 11, NOW, SendMessagePayload(group_id=1, message_text="QA"))
        assert session.query(Action).count() == 0
        healthy = _create_action(session, task, action_type, 12, NOW, SendMessagePayload(group_id=1, message_text="QA"))
        assert healthy.account_id == 12


def test_selection_stores_current_identity_without_overwriting_history():
    from app.models import PlanningAdmissionSnapshot
    with _session() as session:
        task = _seed(session)
        _invalidate(session, session.get(TgAccount, 11), "expired")
        plan = _plan(session, task)
        snapshot = session.scalar(select(PlanningAdmissionSnapshot).where(
            PlanningAdmissionSnapshot.participation_plan_id == plan.id))
        assert snapshot.deficit_account_ids == [11]
        assert snapshot.account_paths[0]["reason"] == "session_invalid"
        assert "authorization_generation" in snapshot.account_paths[0]
        session.get(TgAccount, 12).telegram_frozen = True
        selected_accounts_for_plan(session, task, plan)
        assert snapshot.admissible_account_ids == [12, 13, 14]


def test_late_group_result_keeps_cost_but_cannot_become_ready():
    from tests.test_ai_generation_observability import _seed_phase_c_action, _phase_c_request
    from app.services.task_center.ai_generation_persistence import persist_generation_results
    from app.services.task_center.ai_generation_pipeline import SlotGenerationResult
    from app.services.task_center.ai_generator import GeneratedContent
    with _session() as session:
        task, action = _seed_phase_c_action(session)
        session.add(TgAccount(id=11, tenant_id=1, display_name="QA", phone_masked="11",
            status="在线", session_ciphertext="QA"))
        action.account_id = 11
        session.flush()
        task.type_config = {**task.type_config, "engagement_contract_version": "unified_engagement_v1"}
        session.get(TgAccount, action.account_id).telegram_frozen = True
        request = _phase_c_request(task, action)
        persist_generation_results(session, request, [SlotGenerationResult(GeneratedContent(
            "晚到内容", slot_id="generation-observable:turn:1", sequence_index=1))], tokens=7)
        assert action.payload["ai_generation_status"] != "ready"
        assert action.result["evaluator_evidence"]["tokens"] == 7
        assert action.result["evaluator_evidence"]["account_ineligible"] == "account_frozen"


def test_late_comment_result_cannot_become_ready():
    from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope
    from app.models import Task, GenerationJob
    from app.services.task_center.comment_generation_dispatch import prepare_comment_generation_request
    from app.services.task_center.comment_generation_persistence import persist_comment_generation_result
    from app.services.task_center.comment_generation_pipeline import GeneratedCommentResult
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        request = prepare_comment_generation_request(session, action, task)
        task.type_config = {**task.type_config, "engagement_contract_version": "unified_engagement_v1"}
        session.get(TgAccount, action.account_id).telegram_frozen = True
        persist_comment_generation_result(session, request, GeneratedCommentResult("晚到评论", 3))
        session.expire_all()
        assert session.get(GenerationJob, request.payload.generation_job_id).state == "failed"
        assert action.payload["ai_generation_status"] != "ready"
        assert action.result["generated_tokens"] == 3


def test_generation_claim_and_attempt_creation_reject_invalid_before_inserting():
    from app.models import Action, GenerationJob, ExecutionAttempt
    from app.services.task_center.ai_generation_parallel import _claim_one
    from app.services.task_center.dispatcher import _begin_execution_attempt
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
    with _session() as session:
        task = _seed(session)
        account = session.get(TgAccount, 11)
        action = Action(tenant_id=1, task_id=task.id, task_type="group_ai_chat",
            action_type="send_message", account_id=11, status="pending", scheduled_at=NOW)
        session.add(action)
        session.flush()
        account.telegram_frozen = True
        with pytest.raises(RuntimeResourceBlocked):
            _claim_one(session, action, "QA", now=NOW)
        with pytest.raises(RuntimeResourceBlocked):
            _begin_execution_attempt(session, action, account)
        assert session.query(GenerationJob).count() == session.query(ExecutionAttempt).count() == 0


def test_invalid_coverage_prefix_does_not_starve_healthy_batch():
    from app.models import TaskAccountDailyCoverage
    from app.services.task_center.daily_coverage_planning import ready_coverage_plan_batch
    with _session() as session:
        task = _seed(session)
        task.type = "group_ai_chat"
        for identity in (11, 12):
            session.add(TaskAccountDailyCoverage(tenant_id=1, task_id=task.id, group_id=1,
                account_id=identity, coverage_date=NOW.date(), state="ready", target_count=1, targeted_at=NOW))
        _invalidate(session, session.get(TgAccount, 11), "frozen")
        batch = ready_coverage_plan_batch(session, task, now=NOW, limit=1)
        assert [row.account_id for row in batch.rows] == [12]


def test_membership_candidates_filter_current_invalidation():
    from app.services.task_center.channel_membership import _task_membership_candidates
    with _session() as session:
        task = _seed(session)
        _invalidate(session, session.get(TgAccount, 11), "frozen")
        assert {account.id for account in _task_membership_candidates(session, task)} == {12, 13, 14}


def test_valid_current_authorization_does_not_need_legacy_session_copy():
    from app.services.task_center.account_scope import _scope_account_ids
    from app.services.task_center.account_assignment_eligibility import assignment_decisions
    with _session() as session:
        task = _seed(session)
        account = session.get(TgAccount, 11)
        authorization = TgAccountAuthorization(tenant_id=1, account_id=11, is_current=True,
            status="active", health_status="healthy", session_ciphertext="QA-current")
        session.add(authorization)
        session.flush()
        account.current_authorization_id = authorization.id
        account.session_ciphertext = None
        assert assignment_decisions(session, 1, [11]) == {11: ""}
        assert 11 in _scope_account_ids(session, task)
        assert 11 in _plan(session, task).policy_eligible_account_ids


def test_valid_standby_cannot_replace_invalid_current_identity():
    from app.services.task_center.account_assignment_eligibility import assignment_decisions
    with _session() as session:
        _seed(session)
        account = session.get(TgAccount, 11)
        _invalidate(session, account, "authorization_invalid")
        session.add(TgAccountAuthorization(tenant_id=1, account_id=11, role="standby",
            logical_slot="standby", is_current=False, status="active", health_status="healthy",
            session_ciphertext="QA-standby"))
        assert assignment_decisions(session, 1, [11]) == {11: "authorization_invalid"}


def test_late_view_source_does_not_allocate_invalid_historical_member():
    from tests.test_engagement_participation import _messages, _view_allocation
    with _session() as session:
        task = _seed(session)
        first_message = _messages(session, 1)[0]
        original = _view_allocation(session, task, [first_message],
            per_account_source_degree_min=2, per_account_source_degree_max=2)
        invalid_id = original.edge_set[0]["account_id"]
        old_edges = list(original.edge_set)
        _invalidate(session, session.get(TgAccount, invalid_id), "frozen")
        new_message = _messages(session, 1, start=2)[0]
        successor = _view_allocation(session, task, [first_message, new_message])
        assert original.edge_set == old_edges
        assert not any(edge["account_id"] == invalid_id and edge["message_id"] == new_message.id
            for edge in successor.edge_set)


def test_zero_eligible_membership_gate_reports_recovery_wait():
    from app.models import OperationTarget, Action
    from app.services.task_center.channel_membership import gate_channel_membership
    with _session() as session:
        task = _seed(session)
        for identity in (11, 12, 13, 14):
            _invalidate(session, session.get(TgAccount, identity), "expired")
        gate = gate_channel_membership(session, task, session.get(OperationTarget, 101))
        assert not gate.ready and gate.blocker_reason == "no_eligible_accounts"
        assert task.last_error == "no_eligible_accounts"
        assert task.stats["account_assignment_eligibility"]["excluded_count"] == 4
        assert session.query(Action).count() == 0


def test_retry_query_skips_invalid_account_before_limit():
    from app.models import Action
    from app.services.task_center.fulfillment_retry import retry_failed_actions
    with _session() as session:
        task = _seed(session)
        task.failure_policy = {"max_retries": 2}
        for identity in (11, 12):
            session.add(Action(id=f"retry-{identity}", tenant_id=1, task_id=task.id,
                task_type=task.type, action_type="ensure_target_membership", account_id=identity,
                status="failed", scheduled_at=NOW))
        _invalidate(session, session.get(TgAccount, 11), "frozen")
        assert retry_failed_actions(session, task, limit=1, now_value=NOW) == 1
        assert session.get(Action, "retry-11").status == "failed"
        assert session.get(Action, "retry-12").status == "pending"


def test_comment_job_claim_rechecks_account_before_inserting():
    from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope
    from app.models import Task, GenerationJob
    from app.services.task_center.channel_payloads import PostCommentPayload
    from app.services.task_center.comment_generation_job import claim_comment_generation_job
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = {**task.type_config, "engagement_contract_version": "unified_engagement_v1"}
        session.get(TgAccount, action.account_id).telegram_frozen = True
        with pytest.raises(RuntimeResourceBlocked):
            claim_comment_generation_job(session, action, PostCommentPayload.model_validate(action.payload), owner="QA")
        assert session.query(GenerationJob).count() == 0
