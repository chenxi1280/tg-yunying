"""Expired settlement and live dispatch retain PostgreSQL ownership isolation."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Action, Tenant, TgAccount, TgGroup, FulfillmentRemoteFact, OperationTarget
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from tests.postgres_pacing_e4_fixture import factory
from tests.test_due_backlog_containment import NOW, _seed_task_and_action

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def seed(session):
    session.add(Tenant(id=1, name="neutral-expiry"))
    session.flush()
    session.add(TgAccount(id=101, tenant_id=1, display_name="neutral", phone_masked="***", status="active"))
    session.add(TgGroup(id=201, tenant_id=1, title="neutral", tg_peer_id="-100201"))
    session.add(OperationTarget(id=201, tenant_id=1, target_type="group", title="neutral", tg_peer_id="-100201"))
    session.commit()
    _seed_task_and_action(session, task_id="expired-task", action_id="expired",
        scheduled_at=NOW-timedelta(days=1), deadline_at=NOW-timedelta(hours=1))
    _seed_task_and_action(session, task_id="live-task", action_id="live",
        scheduled_at=NOW, deadline_at=NOW+timedelta(hours=1), message_text="ready")


def claim(session):
    return claim_fact_first_candidates(session, owner="neutral", limit=1, now=NOW,
        lease_seconds=30, execution_lane="non_search")


def test_expired_and_live_work_both_commit_in_same_turn(factory):
    with factory() as session:
        seed(session)
        assert claim(session).action_ids == ("live",)
    with factory() as readback:
        assert readback.get(Action,"expired").status == "skipped"
        assert readback.get(Action,"live").status == "claiming"
        fact=readback.scalar(select(FulfillmentRemoteFact).where(FulfillmentRemoteFact.action_id=="expired"))
        assert fact.fact_kind == "safely_not_executed"


def test_locked_expired_action_does_not_block_live_claim_or_get_overwritten(factory):
    with factory() as session:
        seed(session)
    with factory() as owner, factory() as dispatcher:
        locked=owner.scalar(select(Action).where(Action.id=="expired").with_for_update())
        assert claim(dispatcher).action_ids == ("live",)
        assert locked.status == "pending"
        owner.rollback()
    with factory() as dispatcher:
        assert claim(dispatcher).action_ids == ()
    with factory() as readback:
        assert readback.get(Action,"expired").status == "skipped"
