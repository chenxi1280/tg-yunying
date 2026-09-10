"""Persisted confirmation survives a later unknown projection on PostgreSQL."""
import pytest
from sqlalchemy import select
from app.models import Tenant, TgAccount, OperationTarget, FulfillmentObligationProjection
from app.services.task_center.fulfillment_remote_facts import persist_remote_fact, project_remote_fact
from tests.postgres_pacing_e4_fixture import factory
from tests.test_channel_confirmed_fact import seed

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def test_positive_confirmation_commits_and_late_unknown_preserves_it(factory):
    with factory() as session:
        for row in (Tenant(id=1, name="neutral"),
            TgAccount(id=1, tenant_id=1, display_name="neutral", phone_masked="***", status="active"),
            OperationTarget(id=10, tenant_id=1, target_type="channel", title="neutral", tg_peer_id="-1009001")):
            session.add(row)
            session.flush()
        action, attempt, _, unknown = seed(session)
        fact = persist_remote_fact(session, action)
        assert fact.fact_kind == "reaction_observed"
        assert fact.attempt_id == attempt.id
        project_remote_fact(session, fact)
        project_remote_fact(session, unknown)
        session.commit()
    with factory() as readback:
        assert readback.scalar(select(FulfillmentObligationProjection)).state == "confirmed"
