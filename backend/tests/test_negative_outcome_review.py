from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routers.negative_outcomes import router
from app.auth import get_current_user
from app.database import Base, get_session
from app.models import AuditLog, Tenant
from app.services.task_center import negative_outcome_circuit as circuit
from app.services.task_center.negative_outcome_review import review_negative_outcome
from test_group_ai_update_stream import _session, _seed

pytestmark = pytest.mark.no_postgres


def _blocked(session, count=2):
    task, _, _ = _seed(session)
    scope = dict(tenant_id=1, route=task.type, peer_id="-1007", account_id=11)
    for index in range(count):
        state = circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id=str(index))
    session.commit()
    return state, scope


@pytest.mark.parametrize("count", [2, 3, 5])
def test_operator_review_without_new_send_reopens_only_exact_scope(count):
    with _session() as session:
        state, scope = _blocked(session, count)
        other = circuit.record_negative_outcome(session, **{**scope, "route": "channel_comment"},
            event_type="premature_answer", event_id="comment-feedback")
        session.commit()
        review_negative_outcome(session, state.id, tenant_id=1, expected_version=state.version,
            reason="已人工检查并调整", evidence="群管理员确认可继续，样本已核查", actor="operator")
        assert state.level == "normal" and other.level == "proactive_throttled"
        assert len(state.events) == count and all(e["reviewed"] for e in state.events)
        circuit.assert_negative_outcome_circuit_clear(session, **scope, action_kind="proactive")
        version = state.version
        circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="0")
        assert state.version == version
        circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="new-feedback")
        assert state.level == "proactive_throttled"
        assert session.scalar(select(AuditLog).where(AuditLog.action == "negative_outcome_review")) is not None


def test_review_cas_does_not_override_new_feedback_or_other_tenant():
    with _session() as session:
        state, scope = _blocked(session)
        original_version = state.version
        circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="new")
        session.add(Tenant(id=2, name="other"))
        session.commit()
        kwargs = dict(expected_version=original_version, reason="review", evidence="evidence", actor="operator")
        with pytest.raises(ValueError, match="version_conflict"):
            review_negative_outcome(session, state.id, tenant_id=1, **kwargs)
        with pytest.raises(LookupError):
            review_negative_outcome(session, state.id, tenant_id=2, **kwargs)
        assert state.level == "account_peer_quarantined"


def test_review_http_entry_lists_blocks_and_requires_version_reason_and_evidence():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        state, _ = _blocked(session)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(name="operator")
        with TestClient(app) as client:
            rows = client.get("/api/negative-outcomes").json()
            assert rows[0]["id"] == state.id and rows[0]["level"] == "response_restricted"
            url = f"/api/negative-outcomes/{state.id}/review"
            payload = {"expected_version": state.version, "reason": "核查完成", "evidence": "管理员确认"}
            assert client.post(url, json={**payload, "reason": " "}).status_code == 422
            response = client.post(url, json=payload)
            assert response.status_code == 200 and response.json()["level"] == "normal"
            assert client.post(url, json=payload).status_code == 409
            assert client.get("/api/negative-outcomes").json() == []
