from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    ConsistencyQuarantine,
    OperationTarget,
    SearchClickFulfillmentObligation,
    Task,
    Tenant,
)
from app.search_keywords import normalized_keyword_hash
from app.security import encrypt_secret
from app.schemas.task_center import SearchClickTaskConfigUpdate, SearchClickTaskCreate
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center import pacing
from app.services.task_center.fulfillment_takeover import (
    FULFILLMENT_CONTRACT_VERSION,
    UNIFIED_TASK_GATE_LIMIT,
)
from app.services.task_center.search_click_target_progress import search_click_target_progress
from app.services.task_center.stats import next_run_after_task
from app.services.task_center.service import (
    create_search_click_task,
    update_search_click_config,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="default"))
        db.add(AccountPool(
            id=1,
            tenant_id=1,
            name="normal",
            pool_purpose="normal",
            is_enabled=True,
        ))
        db.commit()
        yield db


def _task(target_id: int) -> Task:
    return Task(
        tenant_id=1,
        name="pure click",
        type="search_click",
        status="running",
        timezone="Asia/Shanghai",
        type_config={
            "search_execution_mode": "click_only",
            "target_operation_target_id": target_id,
            "target_input": "https://t.me/example_group",
            "target_title": "目标群",
            "target_link": "https://t.me/example_group",
            "daily_click_target_count": 3,
            "keyword_hashes": [normalized_keyword_hash("关键词")],
            "keyword_text_ciphertexts": [encrypt_secret("关键词")],
            "search_bots": [{"username": "jisou"}],
        },
        stats={},
    )


@pytest.mark.no_postgres
def test_search_click_create_schema_is_click_only() -> None:
    payload = SearchClickTaskCreate(
        target_title="目标群",
        target_link="https://t.me/example_group",
        keywords=["关键词"],
        daily_click_target_count=5,
        account_group_id=1,
        scheduled_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert payload.search_execution_mode == "click_only"
    dumped = payload.model_dump()
    assert "daily_target_count" not in dumped
    assert "join_target_group_after_click" not in dumped
    assert "max_actions_per_day" not in dumped


@pytest.mark.no_postgres
def test_pure_search_next_run_aligns_to_next_claim_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_value = datetime(2026, 7, 30, 3, 59, 42)
    monkeypatch.setattr(pacing, "_now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    task = _task(1)
    task.pacing_config = {
        "quiet_hours": {"start": "03:00", "end": "07:38"},
        "operation_profile": {"hourly_activity_curve": [0] * 24},
    }

    assert next_run_after_task(task) == datetime(2026, 7, 30, 4)


@pytest.mark.no_postgres
def test_search_click_create_schema_rejects_membership_fields() -> None:
    with pytest.raises(ValidationError):
        SearchClickTaskCreate.model_validate({
            "target_title": "目标群",
            "target_link": "https://t.me/example_group",
            "keywords": ["关键词"],
            "daily_click_target_count": 5,
            "account_group_id": 1,
            "scheduled_end": "2026-08-01T00:00:00Z",
            "daily_target_count": 2,
        })


@pytest.mark.no_postgres
def test_search_click_creation_does_not_read_runtime_capacity(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.task_center.service._validate_strict_search_join_daily_capacity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime capacity must not run during create")
        ),
    )
    payload = SearchClickTaskCreate(
        target_title="目标群",
        target_link="https://t.me/example_group",
        keywords=["关键词"],
        daily_click_target_count=500,
        account_group_id=1,
        scheduled_end=datetime(2030, 8, 1, tzinfo=timezone.utc),
    )

    task = create_search_click_task(
        session,
        1,
        payload,
        actor="tester",
    )

    assert task.status == "draft"
    assert task.type == "search_click"
    assert task.type_config["daily_click_target_count"] == 500
    assert task.pacing_config["max_actions_per_day"] == UNIFIED_TASK_GATE_LIMIT
    assert task.pacing_config["max_actions_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    assert task.stats["fulfillment_contract_version"] == FULFILLMENT_CONTRACT_VERSION


@pytest.mark.no_postgres
def test_start_materializes_stable_click_ordinals(session: Session) -> None:
    target = OperationTarget(
        tenant_id=1,
        target_type="group",
        tg_peer_id="example_group",
        username="example_group",
        title="目标群",
    )
    session.add(target)
    session.flush()
    task = _task(target.id)
    session.add(task)
    session.flush()

    first = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 29, 3, tzinfo=timezone.utc),
    )
    second = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 29, 4, tzinfo=timezone.utc),
    )
    rows = list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .where(SearchClickFulfillmentObligation.task_day_ledger_id == first.id)
        .order_by(SearchClickFulfillmentObligation.click_obligation_ordinal)
    ))

    assert first.id == second.id
    assert [row.click_obligation_ordinal for row in rows] == [1, 2, 3]
    assert all(row.status == "open" for row in rows)


@pytest.mark.no_postgres
def test_progress_reads_immutable_obligations_not_action_count(session: Session) -> None:
    target = OperationTarget(
        tenant_id=1,
        target_type="group",
        tg_peer_id="example_group",
        username="example_group",
        title="目标群",
    )
    session.add(target)
    session.flush()
    task = _task(target.id)
    session.add(task)
    session.flush()
    ledger = ensure_task_day_ledger(
        session,
        task,
        now=datetime(2026, 7, 29, 3, tzinfo=timezone.utc),
    )
    obligations = list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .where(SearchClickFulfillmentObligation.task_day_ledger_id == ledger.id)
        .order_by(SearchClickFulfillmentObligation.click_obligation_ordinal)
    ))
    obligations[0].status = "confirmed"
    obligations[0].target_click_observed = True
    obligations[0].click_evidence_hash = "e" * 64
    obligations[1].status = "assigned"

    progress = search_click_target_progress(
        session,
        task,
        now_value=datetime(2026, 7, 29, 4, tzinfo=timezone.utc),
    )

    assert progress.confirmed_count == 1
    assert progress.held_count == 1
    assert progress.remaining_slot_count == 2
    assert progress.planning_click_deficit == 1
    assert progress.committed_attempt_count == 1


@pytest.mark.no_postgres
def test_running_search_click_update_waits_for_next_ledger(
    session: Session,
) -> None:
    target = OperationTarget(
        tenant_id=1,
        target_type="group",
        tg_peer_id="example_group",
        username="example_group",
        title="目标群",
    )
    session.add(target)
    session.flush()
    task = _task(target.id)
    session.add(task)
    session.flush()
    start = datetime(2026, 7, 29, 3, tzinfo=timezone.utc)
    current = ensure_task_day_ledger(session, task, now=start)
    session.commit()

    update_search_click_config(
        session,
        1,
        task.id,
        SearchClickTaskConfigUpdate(daily_click_target_count=5),
        "运营",
    )

    assert task.type_config["daily_click_target_count"] == 3
    pending = task.stats["pending_search_click_revision"]
    assert pending["type_config"]["daily_click_target_count"] == 5
    assert session.query(SearchClickFulfillmentObligation).filter_by(
        task_day_ledger_id=current.id
    ).count() == 3

    next_ledger = ensure_task_day_ledger(
        session,
        task,
        now=start + timedelta(days=1),
    )
    assert current.lifecycle_status == "closed_missed"
    assert task.type_config["daily_click_target_count"] == 5
    assert session.query(SearchClickFulfillmentObligation).filter_by(
        task_day_ledger_id=next_ledger.id
    ).count() == 5


@pytest.mark.no_postgres
def test_deadline_does_not_close_met_with_active_quarantine(
    session: Session,
) -> None:
    target = OperationTarget(
        tenant_id=1,
        target_type="group",
        tg_peer_id="example_group",
        username="example_group",
        title="目标群",
    )
    session.add(target)
    session.flush()
    task = _task(target.id)
    session.add(task)
    session.flush()
    start = datetime(2026, 7, 29, 3, tzinfo=timezone.utc)
    current = ensure_task_day_ledger(session, task, now=start)
    obligations = list(session.scalars(select(
        SearchClickFulfillmentObligation
    ).where(
        SearchClickFulfillmentObligation.task_day_ledger_id == current.id
    )))
    for obligation in obligations:
        obligation.status = "confirmed"
        obligation.target_click_observed = True
        obligation.click_evidence_hash = obligation.id.replace("-", "")
    session.add(ConsistencyQuarantine(
        tenant_id=1,
        scope_type="search_click_obligation",
        scope_id=obligations[0].id,
        reason_code="remote_fact_owned_elsewhere",
        issue_fingerprint="q" * 64,
    ))

    ensure_task_day_ledger(session, task, now=start + timedelta(days=1))

    assert current.lifecycle_status == "closed_missed"
