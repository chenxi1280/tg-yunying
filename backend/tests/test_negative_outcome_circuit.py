from __future__ import annotations

from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    NegativeOutcomeCircuitState,
    NegativeOutcomePolicyRevision,
    Tenant,
    TgAccount,
)
from app.services.task_center.negative_outcome_circuit import (
    NegativeOutcomeBlocked,
    assert_negative_outcome_circuit_clear,
    classify_negative_event,
    detect_ai_suspicion_in_text,
    evaluate_circuit_state,
    record_negative_outcome,
)

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess


def test_detect_ai_suspicion():
    assert detect_ai_suspicion_in_text("你怎么跟个机器人一样？") is True
    assert detect_ai_suspicion_in_text("你是AI吧别装了") is True
    assert detect_ai_suspicion_in_text("群里好多人机水军") is True
    assert detect_ai_suspicion_in_text("今天天气不错，大盘涨了") is False


def test_classify_negative_event():
    assert classify_negative_event(content_text="你是机器人吗？") == "ai_suspicion"
    assert classify_negative_event(deleted_by_admin=True) == "admin_moderation"
    assert classify_negative_event(error_code="user_banned_by_admin") == "admin_moderation"
    assert classify_negative_event(error_code="tg_bot_detected_interception") == "bot_intercept"
    assert classify_negative_event(is_deleted=True) == "unknown"


def test_circuit_escalation_and_gating(session):
    tenant = Tenant(id=1, name="Test Tenant")
    session.add(tenant)
    session.flush()

    peer_id = "-1008888"
    account_id = 101

    # Initially normal: clear
    assert_negative_outcome_circuit_clear(session, tenant_id=1, peer_id=peer_id, account_id=account_id)

    # 1st negative event -> proactive_throttled
    c1 = record_negative_outcome(
        session, tenant_id=1, peer_id=peer_id, account_id=account_id, event_type="user_retract", event_id="delete:1"
    )
    assert c1.level == "proactive_throttled"
    # Proactive is blocked, but response is still allowed
    with pytest.raises(NegativeOutcomeBlocked):
        assert_negative_outcome_circuit_clear(session, tenant_id=1, peer_id=peer_id, account_id=account_id, action_kind="proactive")
    assert_negative_outcome_circuit_clear(session, tenant_id=1, peer_id=peer_id, account_id=account_id, action_kind="response")

    # 2nd negative event -> response_restricted
    c2 = record_negative_outcome(
        session, tenant_id=1, peer_id=peer_id, account_id=account_id, event_type="ai_suspicion", event_id="reply:2"
    )
    assert c2.level == "response_restricted"
    with pytest.raises(NegativeOutcomeBlocked):
        assert_negative_outcome_circuit_clear(session, tenant_id=1, peer_id=peer_id, account_id=account_id, action_kind="response")

    # 3rd negative event -> account_peer_quarantined
    c3 = record_negative_outcome(
        session, tenant_id=1, peer_id=peer_id, account_id=account_id, event_type="admin_moderation", event_id="admin:3"
    )
    assert c3.level == "account_peer_quarantined"
    with pytest.raises(NegativeOutcomeBlocked):
        assert_negative_outcome_circuit_clear(session, tenant_id=1, peer_id=peer_id, account_id=account_id, action_kind="response")
