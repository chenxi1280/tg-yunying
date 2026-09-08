from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountGroupAdmissionFact, Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center import task_group_bot_admission_v2 as admission_service
from app.services.task_center.task_group_bot_admission_recovery import reopen_unproven_task_coverages

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(admission_service, "credentials_for_account", lambda *args: object())
    monkeypatch.setattr(admission_service.gateway, "fetch_group_messages", lambda *args, **kwargs: [])
    with Session(engine) as current:
        current.add_all([
            Tenant(id=1, name="测试"),
            Task(id="task", tenant_id=1, type="group_ai_chat", name="测试", status="running",
                 fulfillment_contract_version="fact_first_v3"),
            TgGroup(id=10, tenant_id=1, tg_peer_id="-10010", title="测试"),
            TgAccount(id=20, tenant_id=1, display_name="测试", phone_masked="20",
                      status="在线", session_ciphertext="test-session"),
        ])
        current.commit()
        yield current
    engine.dispose()


def _evaluate(session):
    return admission_service.evaluate_task_admission(
        session, task_id="task", tenant_id=1, group_id=10, account_id=20,
    )


def _admission(session):
    from app.models import TaskGroupBotAdmission
    result = _evaluate(session)
    return session.get(TaskGroupBotAdmission, result.admission_id)


def _fail_three_times(session, admission):
    for _ in range(admission_service.MAX_OBSERVATION_GAP_RETRIES):
        admission_service._restart_with_gap(session, admission, "TimeoutError")


def _previous_day(admission):
    admission.terminal_evidence = {
        **admission.terminal_evidence,
        "terminal_date": (_now().date() - timedelta(days=1)).isoformat(),
    }


def test_high_observation_version_does_not_consume_retry_budget(session):
    admission = _admission(session)
    admission.observation_version = 50
    admission_service._start_post_follow_observation(admission)
    session.flush()
    result = admission_service._restart_with_gap(session, admission, "TimeoutError")
    assert result.code == "c2_observation_gap"
    assert admission.state == "observing"
    assert admission.consecutive_observation_gaps == 1


def test_only_actual_failed_reads_increment_count(session, monkeypatch):
    admission = _admission(session)

    def fail(*args, **kwargs):
        raise TimeoutError("temporary network failure")

    monkeypatch.setattr(admission_service.gateway, "fetch_group_messages", fail)
    for count in (1, 2, 3):
        admission.no_prompt_pass_at = _now() - timedelta(seconds=1)
        result = _evaluate(session)
        assert admission.consecutive_observation_gaps == count
        expected = "c2_account_abandoned" if count == 3 else "c2_observation_gap"
        assert result.code == expected
        _evaluate(session)
        assert admission.consecutive_observation_gaps == count
    assert admission.terminal_reason == "observation_gap_limit_reached"
    assert admission.terminal_evidence["outcome"] == "abandoned_for_day"
    assert admission.terminal_evidence["detail"] == "TimeoutError"
    assert admission.terminal_evidence["blocker_code"] == "c2_observation_evidence_missing"


def test_successful_empty_read_clears_failure_streak(session):
    admission = _admission(session)
    admission_service._restart_with_gap(session, admission, "TimeoutError")
    admission_service._restart_with_gap(session, admission, "TimeoutError")
    admission.no_prompt_pass_at = _now() - timedelta(seconds=1)
    assert _evaluate(session).allowed
    assert admission.consecutive_observation_gaps == 0


@pytest.mark.parametrize("transition", ["follow", "surface"])
def test_normal_new_observation_resets_failure_streak(session, transition):
    admission = _admission(session)
    admission_service._restart_with_gap(session, admission, "TimeoutError")
    if transition == "follow":
        admission_service._start_post_follow_observation(admission)
    else:
        admission_service._restart_surface(session, admission=admission,
            group=session.get(TgGroup, 10), account=session.get(TgAccount, 20), authorization=None)
    assert admission.consecutive_observation_gaps == 0
    result = admission_service._restart_with_gap(session, admission, "TimeoutError")
    assert result.code == "c2_observation_gap"
    assert admission.consecutive_observation_gaps == 1


def test_persisted_gap_is_restarted_without_version_based_abandonment(session):
    admission = _admission(session)
    admission.observation_version = 50
    admission.observation_gap = True
    session.flush()
    assert _evaluate(session).code == "c2_observation_gap"
    assert admission.observation_gap is False
    assert admission.consecutive_observation_gaps == 1


def test_same_day_is_terminal_but_next_day_direct_gate_restarts(session):
    admission = _admission(session)
    _fail_three_times(session, admission)
    assert _evaluate(session).code == "c2_account_abandoned"
    _previous_day(admission)
    session.flush()
    assert _evaluate(session).code == "c2_observation_restarted_for_task_day"
    assert admission.consecutive_observation_gaps == 0
    assert admission.state == "observing"


def test_planner_reopens_next_day_without_changing_historical_coverage(session):
    admission = _admission(session)
    _fail_three_times(session, admission)
    rows = [TaskAccountDailyCoverage(
        id=f"coverage-{days}", tenant_id=1, task_id="task", group_id=10, account_id=20,
        coverage_date=_now().date() - timedelta(days=days), state="abandoned_for_day",
        blocker_code="c2_observation_evidence_missing",
    ) for days in (0, 1)]
    session.add_all(rows)
    session.flush()
    task, group = session.get(Task, "task"), session.get(TgGroup, 10)
    assert reopen_unproven_task_coverages(session, task, group, limit=20) == 0
    _previous_day(admission)
    session.flush()
    assert reopen_unproven_task_coverages(session, task, group, limit=20) == 1
    assert admission.state == "observing"
    assert admission.consecutive_observation_gaps == 0
    assert rows[0].state == "pending_admission"
    assert rows[1].state == "abandoned_for_day"


@pytest.mark.parametrize("blocker", ["paused", "frozen", "epoch"])
def test_next_day_recovery_preserves_task_and_account_eligibility(session, blocker):
    admission = _admission(session)
    _fail_three_times(session, admission)
    _previous_day(admission)
    if blocker == "paused":
        session.get(Task, "task").status = "paused"
    elif blocker == "frozen":
        session.get(TgAccount, 20).telegram_frozen = True
    else:
        session.get(Task, "task").task_lifecycle_epoch += 1
    session.flush()
    assert _evaluate(session).code == "c2_account_abandoned"
    assert admission.consecutive_observation_gaps == 3


def test_stale_gap_result_cannot_overwrite_new_observation(session):
    from app.models import TaskGroupBotAdmission
    admission = _admission(session)
    session.commit()
    original_version = admission.version
    with Session(session.bind) as successor:
        current = successor.get(TaskGroupBotAdmission, admission.id)
        current.version = original_version + 1
        successor.commit()
    count_before = session.scalar(select(func.count()).select_from(AccountGroupAdmissionFact))
    with pytest.raises(ValueError, match="c2_observation_version_conflict"):
        admission_service._restart_with_gap(session, admission, "TimeoutError")
    session.rollback()
    assert admission.version == original_version + 1
    assert admission.consecutive_observation_gaps == 0
    assert session.scalar(select(func.count()).select_from(AccountGroupAdmissionFact)) == count_before


@pytest.mark.parametrize("coverage_state", ["ready", "unknown"])
def test_observation_abandonment_preserves_other_days_targets_and_called_work(session, coverage_state):
    from app.models import Action, ExecutionAttempt
    from app.services.task_center.dispatcher import _abandon_fact_first_account_for_task
    from app.timezone import beijing_day_bounds

    today, tomorrow = beijing_day_bounds(_now())
    rows = [Action(id=name, tenant_id=1, task_id="task", task_type="group_ai_chat",
        account_id=20, action_type="send_message", status="pending", scheduled_at=when,
        payload={"group_id": group_id}) for name, when, group_id in (
            ("current", today, 10), ("safe", today, 10), ("called", today, 10),
            ("next-day", tomorrow, 10), ("other-target", today, 11),
        )]
    session.add_all(rows)
    session.flush()
    session.add(ExecutionAttempt(action_id="called", tenant_id=1, account_id=20,
        gateway_call_started_at=today, status="unknown_after_send"))
    coverages = [TaskAccountDailyCoverage(id=name, tenant_id=1, task_id="task",
        account_id=20, group_id=group_id, coverage_date=when.date(), state=state)
        for name, when, group_id, state in (
            ("today", today, 10, coverage_state), ("yesterday", today - timedelta(days=1), 10, "ready"),
            ("other", today, 11, "ready"),
        )]
    session.add_all(coverages)
    session.flush()
    _abandon_fact_first_account_for_task(session, rows[0], reason="observation_gap_limit_reached")
    assert rows[1].status == "skipped"
    assert all(row.status == "pending" for row in (rows[2], rows[3], rows[4]))
    expected = "abandoned_for_day" if coverage_state == "ready" else "unknown"
    assert coverages[0].state == expected
    assert coverages[1].state == coverages[2].state == "ready"
    if coverage_state == "ready":
        assert coverages[0].blocker_code == "c2_observation_evidence_missing"
