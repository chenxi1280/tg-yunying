from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob
from app.services._common import _now
from app.services.task_center.ai_generation_recovery import reconcile_generation_jobs
from tests.test_ai_generation_reconcile_fencing import (
    _action,
    _engine,
    _job,
    _seed_scope,
)


pytestmark = pytest.mark.no_postgres


def test_reconcile_recovers_exact_unowned_legacy_action() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-unowned", "slot-unowned", state="generating", owner="worker-old")
        action = _action(
            "action-unowned", "slot-unowned", job.id,
            status="pending", generation_status="generating",
        )
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "pending"
        assert refreshed_job.state == "pending"
        assert refreshed_job.generation_stage == "generation_recovery"


def test_reconcile_preserves_provider_started_skipped_legacy_action_as_unknown() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-provider-started", "slot-provider-started", state="generating", owner="worker-old")
        action = _action(
            "action-provider-started", "slot-provider-started", job.id,
            status="skipped", owner="worker-old",
        )
        action.claim_owner = ""
        action.claim_token = ""
        action.lease_owner = ""
        action.lease_expires_at = None
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
        session.add_all([job, action])
        session.commit()

        assert reconcile_generation_jobs(session, limit=10) == 1
        session.commit()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        action_count = session.scalar(select(func.count(Action.id)))
        assert action_count == 1
        assert refreshed_action.status == "pending"
        assert refreshed_action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert refreshed_job.state == "unknown"
        assert refreshed_job.generation_stage == "ai_result_persist_unknown"


def test_reconcile_rejects_provider_started_action_with_different_live_owner() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_scope(session)
        job = _job("job-reclaimed", "slot-reclaimed", state="generating", owner="worker-old")
        action = _action(
            "action-reclaimed", "slot-reclaimed", job.id,
            status="skipped", owner="worker-new",
        )
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
        session.add_all([job, action])
        session.commit()

        with pytest.raises(RuntimeError, match="action_claim_changed"):
            reconcile_generation_jobs(session, limit=10)
        session.rollback()

        refreshed_action = session.get(Action, action.id)
        refreshed_job = session.get(GenerationJob, job.id)
        assert refreshed_action.claim_owner == "worker-new"
        assert refreshed_job.generation_owner_id == "worker-old"
