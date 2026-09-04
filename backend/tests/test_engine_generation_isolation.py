from contextlib import nullcontext
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace as NS

import pytest

from app import worker
from app.services.task_center import ai_generation_worker as group_worker
from app.services.task_center import comment_generation_worker as comment_worker
from app.services.task_center.ai_generation_timing import generation_not_before
from app.services.task_center.channel_membership import gate_channel_membership, channel_member_accounts
from app.services.task_center.comment_generation_job import _claim_generation_job, CommentGenerationJobConflict
from app.worker_independent_lanes import independent_comment_lane


pytestmark = pytest.mark.no_postgres


def test_group_drain_never_enters_comment_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(group_worker, "reconcile_generation_jobs", lambda *a, **kw: calls.append(kw["task_type"]))
    monkeypatch.setattr(comment_worker, "drain_comment_generation", lambda *a, **kw: pytest.fail("cross-adapter drain"))
    monkeypatch.setattr(group_worker, "_drain_parallel_generation", lambda *a, **kw: 20)
    factory = lambda: nullcontext(NS(commit=lambda: None))
    assert group_worker.drain_ai_generation(factory, limit=20) == 20
    assert calls == ["group_ai_chat"]


def test_comment_role_does_not_call_group(monkeypatch):
    monkeypatch.setattr(worker, "drain_ai_generation", lambda *a: pytest.fail("cross-adapter drain"))
    monkeypatch.setattr(worker, "drain_comment_generation", lambda factory, limit: limit)
    assert worker.drain_once(3, role="comment-generation") == 3


def test_comment_retry_cannot_reclaim_same_action_within_one_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(comment_worker, "reconcile_generation_jobs", lambda *a, **kw: None)

    def claim(_factory, *, owner, excluded_action_ids):
        remaining = [key for key in ("retry", "healthy") if key not in excluded_action_ids]
        return NS(action_id=remaining[0]) if remaining else None

    monkeypatch.setattr(comment_worker, "_claim_comment_generation", claim)
    monkeypatch.setattr(comment_worker, "_process_comment_generation", lambda factory, row, **kw: calls.append(row.action_id))
    factory = lambda: nullcontext(NS(commit=lambda: None))
    assert comment_worker.drain_comment_generation(factory, limit=20) == 2
    assert calls == ["retry", "healthy"]


def test_blocked_comment_loop_does_not_block_primary_iterations():
    entered, primary_ran = Event(), Event()

    def blocked_comment(stop):
        entered.set()
        assert primary_ran.wait(2)
        stop.wait(2)

    with independent_comment_lane("all", stop_event=None, run=blocked_comment):
        assert entered.wait(2)
        for _ in range(3):
            primary_ran.set()


def test_two_lanes_do_not_join_at_each_iteration():
    progressed = Event()

    def fast_comment(stop):
        progressed.set()
        stop.wait(2)

    with independent_comment_lane("all", stop_event=None, run=fast_comment):
        assert progressed.wait(2)


def test_generation_uses_effective_send_time_minus_ten_seconds():
    scheduled = datetime(2026, 9, 4, 12)
    action = NS(scheduled_at=scheduled, release_not_before_at=scheduled + timedelta(minutes=5),
                effective_claim_at=scheduled + timedelta(minutes=8))
    assert generation_not_before(action) == scheduled + timedelta(minutes=8, seconds=-10)


def test_comment_job_rejects_future_preparation_without_db_write(monkeypatch):
    now = datetime(2026, 9, 4, 12)
    monkeypatch.setattr("app.services.task_center.comment_generation_job._now", lambda: now)
    job = NS(id="future", state="pending", generation_not_before_at=now + timedelta(seconds=1), next_retry_at=None)
    with pytest.raises(CommentGenerationJobConflict, match="generation_not_due"):
        _claim_generation_job(None, job, owner="test")


def test_public_view_skips_membership_queries_but_not_other_tasks():
    target = NS(target_type="channel", username="public", can_send=False)
    task = NS(type="channel_view", stats={})
    accounts = [NS(id=1)]
    assert gate_channel_membership(None, task, target).ready
    assert channel_member_accounts(None, task, target, accounts) == accounts
    assert task.stats["membership_stage"] == "not_required_public_view"


@pytest.mark.parametrize("task_type,username,expected", [
    ("channel_view", "public", True), ("channel_view", "", False),
    ("channel_comment", "public", False), ("channel_like", "public", False),
])
def test_public_view_rule_is_operation_specific(task_type, username, expected):
    from app.services.task_center.channel_access import public_channel_view
    assert public_channel_view(task_type, NS(target_type="channel", username=username)) is expected
