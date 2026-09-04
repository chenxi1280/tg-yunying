from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPacingReservation, FulfillmentObligationProjection, Task, TaskDayLedger, TaskGroupDailyMessageSlot, TgAccount
from app.services.task_center import ai_generation_runtime_config, generation_wait
from app.services.task_center.generation_deadlines import batch_latest_safe_send_at, latest_safe_send_at
from app.timezone import BEIJING_TZ
from tests.ai_generation_phase_test_support import seed_reserved_normal_batch

pytestmark = pytest.mark.no_postgres
START = datetime(2026, 9, 4, 12)


@pytest.fixture
def seeded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        actions, _ = seed_reserved_normal_batch(session, START)
        session.flush()
        yield session, actions
    engine.dispose()


def _pacing(session, action, *, deadline):
    row = AccountPacingReservation(
        tenant_id=action.tenant_id, task_id=action.task_id, account_id=action.account_id,
        pacing_slot_key=f"qa:{action.id}", policy_version="qa", due_at=START,
        release_not_before_at=START, effective_claim_at=START, source_deadline_at=deadline,
        action_id=action.id,
    )
    session.add(row)
    session.flush()
    return row


def _projection(session, action, *, deadline):
    action.obligation_type = "coverage"
    action.obligation_id = f"qa:{action.id}"
    row = FulfillmentObligationProjection(
        tenant_id=action.tenant_id, task_id=action.task_id, obligation_type=action.obligation_type,
        obligation_id=action.obligation_id, deadline_at=deadline, work_lane="group_ai_chat",
        task_lifecycle_epoch=action.task_lifecycle_epoch,
    )
    session.add(row)
    session.flush()
    return row


def _quantity_day(session, action):
    ledger = TaskDayLedger(
        id="qa-day", tenant_id=action.tenant_id, task_id=action.task_id,
        timezone_snapshot="Asia/Shanghai", timezone_revision=1, obligation_local_date=START.date(),
        period_start_at=START.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc),
        deadline_at=(START + timedelta(hours=4)).replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc),
        day_phase="full_day", planning_anchor_at=START,
    )
    slot = TaskGroupDailyMessageSlot(
        id="qa-slot", tenant_id=action.tenant_id, task_id=action.task_id,
        task_day_ledger_id=ledger.id, target_operation_target_id=7, slot_kind="extra_volume", slot_ordinal=1,
    )
    session.add_all([ledger, slot])
    action.primary_quantity_slot_id = slot.id
    session.flush()
    return ledger, slot


def test_source_and_freshness_clip_day_deadline(seeded):
    session, actions = seeded
    action = actions[0]
    _quantity_day(session, action)
    _pacing(session, action, deadline=START + timedelta(minutes=20))
    _projection(session, action, deadline=START + timedelta(minutes=10))
    action.payload = {**action.payload, "freshness_deadline_at": (START + timedelta(seconds=45)).isoformat()}
    assert latest_safe_send_at(session, action) == START + timedelta(seconds=45)
    assert ai_generation_runtime_config._latest_safe_send_at(session, action) == START + timedelta(seconds=45)
    assert generation_wait.latest_safe_send_at(session, action) == START + timedelta(seconds=45)


def test_pacing_does_not_hide_earlier_projection(seeded):
    session, actions = seeded
    _pacing(session, actions[0], deadline=START + timedelta(hours=1))
    _projection(session, actions[0], deadline=START + timedelta(seconds=30))
    assert latest_safe_send_at(session, actions[0]) == START + timedelta(seconds=30)


def test_all_payload_constraints_are_intersected_with_offsets(seeded):
    session, actions = seeded
    action = actions[0]
    action.payload = {
        **action.payload, "obligation_deadline_at": "2026-09-04T13:00:00+08:00",
        "deadline_at": "2026-09-04T04:30:00Z", "freshness_deadline_at": "2026-09-04T12:02:00+08:00",
    }
    assert latest_safe_send_at(session, action) == START + timedelta(minutes=2)


def test_batch_uses_earliest_action_not_first(seeded):
    session, actions = seeded
    for index, action in enumerate(actions):
        action.payload = {**action.payload, "deadline_at": (START + timedelta(minutes=30-index)).isoformat()}
    assert len(actions) > 1
    assert batch_latest_safe_send_at(session, actions) == START + timedelta(minutes=31-len(actions))


def test_quantity_day_sqlite_utc_readback_is_not_beijing_wall(seeded):
    session, actions = seeded
    _quantity_day(session, actions[0])
    session.expire_all()
    assert latest_safe_send_at(session, actions[0]) == START + timedelta(hours=4)


@pytest.mark.parametrize("field,value,reason", (
    ("tenant_id", 999, "pacing"), ("task_id", "other-task", "pacing"),
    ("account_id", 999, "pacing_account"),
))
def test_wrong_pacing_owner_does_not_supply_deadline(seeded, *, field, value, reason):
    session, actions = seeded
    row = _pacing(session, actions[0], deadline=START)
    setattr(row, field, value)
    with pytest.raises(ValueError, match=f"generation_deadline_scope_mismatch:{reason}"):
        latest_safe_send_at(session, actions[0])


def test_wrong_projection_epoch_does_not_supply_deadline(seeded):
    session, actions = seeded
    row = _projection(session, actions[0], deadline=START)
    row.task_lifecycle_epoch += 1
    with pytest.raises(ValueError, match="projection_epoch"):
        latest_safe_send_at(session, actions[0])


def test_missing_and_malformed_deadlines_are_distinct(seeded):
    session, actions = seeded
    assert latest_safe_send_at(session, actions[0]) is None
    actions[0].payload = {**actions[0].payload, "freshness_deadline_at": "not-a-date"}
    with pytest.raises(ValueError, match="generation_deadline_invalid:freshness_deadline_at"):
        latest_safe_send_at(session, actions[0])


@pytest.mark.parametrize("frozen,current", ((90, 30), (30, 90), (30, None)))
def test_retry_deadline_cannot_extend(seeded, monkeypatch, *, frozen, current):
    session, actions = seeded
    action = actions[0]
    if current is not None:
        action.payload = {**action.payload, "freshness_deadline_at": (START + timedelta(seconds=current)).isoformat()}
    job = SimpleNamespace(latest_safe_send_at=START + timedelta(seconds=frozen), candidate_hash="", evaluator_evidence={})
    task = SimpleNamespace(failure_policy={})
    spec = generation_wait.GenerationWaitSpec(
        stage="qa", error_code="qa", error_detail="QA", shortfall_kind="generation", evaluator_evidence={},
        next_retry_at=START + timedelta(seconds=40),
    )
    settled = []
    monkeypatch.setattr(generation_wait, "_now", lambda: START)
    monkeypatch.setattr(generation_wait, "_settle_generation_shortfall", lambda *args, **kwargs: settled.append(kwargs))
    result = generation_wait.defer_generation_wait(session, task, action, job, spec)
    assert result == "shortfall"
    assert job.latest_safe_send_at == START + timedelta(seconds=30)
    assert len(settled) == 1


def test_real_generation_request_carries_batch_freshness_deadline(seeded):
    from app.services.task_center.ai_generation_dispatch import _generation_request
    from app.services.task_center.payloads import SendMessagePayload

    session, actions = seeded
    for index, action in enumerate(actions):
        action.payload = {**action.payload, "freshness_deadline_at": (START + timedelta(seconds=90-index)).isoformat()}
    request = _generation_request(
        session.get(Task, actions[0].task_id),
        [(action, SendMessagePayload.model_validate(action.payload)) for action in actions],
        session.get(TgAccount, actions[0].account_id),
        session=session, credentials=object(), peer_id="-1007", attempt_id="qa-deadline",
    )
    assert request.config["_ai_generation_latest_safe_send_at"] == (
        START + timedelta(seconds=91-len(actions))
    ).isoformat()


@pytest.mark.parametrize("offset", (timezone.utc, timezone(timedelta(hours=9)), timezone(timedelta(hours=-5))))
def test_provider_budget_compares_actual_instants(monkeypatch, offset):
    from app.services.task_center.ai_generation_pipeline import _require_provider_attempt_budget
    from app.services.task_center import ai_generation_pipeline
    from app.services.task_center.ai_generator import AI_CONTENT_REQUEST_TIMEOUT_SECONDS, AiGenerationUnavailable

    monkeypatch.setattr(ai_generation_pipeline, "_now", lambda: START)
    for delta in (-1, 1):
        deadline = (START + timedelta(seconds=AI_CONTENT_REQUEST_TIMEOUT_SECONDS + delta)).replace(tzinfo=BEIJING_TZ)
        request = SimpleNamespace(config={"_ai_generation_latest_safe_send_at": deadline.astimezone(offset).isoformat()})
        if delta < 0:
            with pytest.raises(AiGenerationUnavailable, match="deadline_budget_exhausted"):
                _require_provider_attempt_budget(request)
        else:
            _require_provider_attempt_budget(request)


def test_batch_deadline_queries_do_not_grow_per_action(seeded):
    session, actions = seeded
    _, slot = _quantity_day(session, actions[0])
    for action in actions:
        action.primary_quantity_slot_id = slot.id
        _pacing(session, action, deadline=START + timedelta(minutes=10))
        _projection(session, action, deadline=START + timedelta(minutes=5))
    session.flush()
    statements = []

    def record_statement(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", record_statement)
    try:
        assert batch_latest_safe_send_at(session, actions) == START + timedelta(minutes=5)
        assert len(statements) == 3
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", record_statement)
