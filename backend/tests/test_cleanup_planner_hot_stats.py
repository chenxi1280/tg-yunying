from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskRuntimeActiveBlocker, TaskRuntimeSummary, Tenant
from scripts.cleanup_planner_hot_stats import _apply_task, _preview


pytestmark = pytest.mark.no_postgres


def test_cleanup_moves_legacy_hot_stats_to_bounded_projections() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="task",
            tenant_id=1,
            name="task",
            type="group_ai_chat",
            stats=_legacy_stats(),
        )
        session.add(task)
        session.add(TaskRuntimeActiveBlocker(
            tenant_id=1,
            task_id=task.id,
            lifecycle_epoch=1,
            blocker_domain="another_domain",
            scope_key_hash="b" * 64,
            blocker_code="existing",
        ))
        session.flush()

        before = _preview([task])
        _apply_task(session, task)
        session.flush()
        blockers = list(session.scalars(select(TaskRuntimeActiveBlocker).where(
            TaskRuntimeActiveBlocker.task_id == task.id,
        )))
        summary = session.scalar(select(TaskRuntimeSummary).where(
            TaskRuntimeSummary.task_id == task.id,
        ))

        assert before["rows"][0]["blocker_count"] == 2
        assert task.stats == {
            "membership_summary_version": 2,
            "membership_summary_v2": {
                "blocked_account_count": None,
                "candidate_account_count": 40,
                "estimated_membership_actions": None,
                "failed_account_count": None,
                "joined_account_count": None,
                "need_join_account_count": None,
                "unknown_after_send_count": None,
            },
            "preserved": True,
        }
        assert len(blockers) == 3
        assert summary.summary["runtime_blocker_summary_v2"]["active_count"] == 3
        assert len(summary.summary["runtime_blocker_summary_v2"]["samples"]) == 3


def test_cleanup_preview_fingerprint_detects_task_stats_drift() -> None:
    task = Task(id="task", tenant_id=1, name="task", type="channel_view", stats={"a": 1})
    first = _preview([task])["fingerprint"]
    task.stats = {"a": 2}

    assert _preview([task])["fingerprint"] != first


def _legacy_stats() -> dict:
    return {
        "membership_summary": {"candidate_account_count": 40},
        "conversation_quality_active_blockers": {"group:1": "context_stale"},
        "conversation_quality_active_blocker": "provider_unavailable",
        "preserved": True,
    }
