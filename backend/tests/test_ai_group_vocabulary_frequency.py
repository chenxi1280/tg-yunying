from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, FulfillmentRemoteFact
from app.services.task_center.ai_group_vocabulary_frequency import (
    vocabulary_frequency_baseline,
    vocabulary_frequency_violation,
)
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Action.__table__,
            FulfillmentRemoteFact.__table__,
        ],
    )
    session = Session(engine)
    return session


def _action(index: int, payload: dict) -> Action:
    return Action(
        id=f"history-{index}",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        status="pending",
        payload={"allocation_plan_id": "plan", "surface_scope_key": "scope", **payload},
    )


def _payload() -> SendMessagePayload:
    return SendMessagePayload(
        group_id=1,
        ai_generation_status="pending",
        allocation_plan_id="plan",
        surface_scope_key="scope",
    )


def _fact(action: Action, index: int, observed_at: datetime) -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        fact_id=f"fact-{index}",
        tenant_id=1,
        task_type="group_ai_chat",
        task_id="task-1",
        obligation_type="ai_send",
        obligation_id=f"obligation-{index}",
        action_id=action.id,
        attempt_id=f"attempt-{index}",
        mutation_kind="send_message",
        remote_mutation_key_hash=f"mutation-{index}",
        gateway_request_hash=f"gateway-{index}",
        fact_kind="remote_message_observed",
        fact_identity_hash=f"identity-{index}",
        outcome={"remote_message_id": str(index)},
        observed_at=observed_at,
    )


def test_normalized_term_frequency_blocks_sixth_reservation() -> None:
    session = _session()
    try:
        for index in range(5):
            session.add(_action(index, {"vocabulary_used_term_ids": ["同一词项"]}))
        current = _action(9, {})
        session.add(current)
        session.flush()
        payload = _payload()

        violation = vocabulary_frequency_violation(
            session, current, payload, data={"vocabulary_used_term_ids": ["同一词项"]}
        )

        assert violation == "normalized_term:同一词项"
    finally:
        session.close()


def test_surface_phrase_frequency_blocks_third_occurrence_in_twenty() -> None:
    session = _session()
    try:
        for index in range(2):
            session.add(
                _action(index, {"surface_phrase_fingerprints": ["fingerprint"]})
            )
        current = _action(9, {})
        session.add(current)
        session.flush()
        payload = _payload()

        violation = vocabulary_frequency_violation(
            session,
            current,
            payload,
            data={"surface_phrase_fingerprints": ["fingerprint"]},
        )

        assert violation == "surface_2gram:fingerprint"
    finally:
        session.close()


def test_vocabulary_id_frequency_blocks_sixth_reservation() -> None:
    session = _session()
    try:
        for index in range(5):
            session.add(_action(index, {"vocabulary_used_ids": ["unit-1"]}))
        current = _action(9, {})
        session.add(current)
        session.flush()
        payload = _payload()

        violation = vocabulary_frequency_violation(
            session, current, payload, data={"vocabulary_used_ids": ["unit-1"]}
        )

        assert violation == "vocabulary_id:unit-1"
    finally:
        session.close()


def test_surface_scope_filter_is_applied_before_history_limit() -> None:
    session = _session()
    try:
        anchor = datetime(2026, 8, 31, tzinfo=timezone.utc)
        for index in range(5):
            historical = _action(
                index,
                {
                    "surface_scope_key": "scope",
                    "vocabulary_used_ids": ["unit-1"],
                },
            )
            historical.created_at = anchor - timedelta(days=1, seconds=index)
            session.add(historical)
        for index in range(500):
            unrelated = _action(
                1000 + index,
                {"surface_scope_key": "another-scope"},
            )
            unrelated.created_at = anchor + timedelta(seconds=index)
            session.add(unrelated)
        current = _action(9999, {})
        current.created_at = anchor + timedelta(days=1)
        session.add(current)
        session.flush()

        violation = vocabulary_frequency_violation(
            session,
            current,
            _payload(),
            data={"vocabulary_used_ids": ["unit-1"]},
        )

        assert violation == "vocabulary_id:unit-1"
    finally:
        session.close()


def test_confirmed_history_is_ordered_by_remote_observed_time() -> None:
    session = _session()
    try:
        anchor = datetime(2026, 8, 31, tzinfo=timezone.utc)
        remote_latest = _action(1, {"vocabulary_used_ids": ["remote-latest"]})
        remote_latest.status = "success"
        remote_latest.created_at = anchor
        created_latest = _action(2, {"vocabulary_used_ids": ["created-latest"]})
        created_latest.status = "success"
        created_latest.created_at = anchor + timedelta(days=2)
        current = _action(9, {})
        current.created_at = anchor + timedelta(days=3)
        session.add_all([remote_latest, created_latest, current])
        session.add(_fact(remote_latest, 1, anchor + timedelta(days=2)))
        session.add(_fact(created_latest, 2, anchor + timedelta(days=1)))
        session.flush()

        rows = vocabulary_frequency_baseline(session, current, _payload())

        assert rows[0]["vocabulary_used_ids"] == ["remote-latest"]
        assert rows[1]["vocabulary_used_ids"] == ["created-latest"]
    finally:
        session.close()
