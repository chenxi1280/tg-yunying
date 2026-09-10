from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.integrations.telegram.update_contracts import TelegramDifferenceBatch
from app.models import Action, Task
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
)
from app.services.task_center import telegram_update_collector as collector
from app.services.task_center.telegram_update_channels import apply_channel_batch
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from app.services.task_center.group_clone_runtime_lifecycle import (
    pause_group_clone, resume_group_clone, start_existing_group_clone,
)
from app.services._common import _now

import test_group_clone_update_collector as collector_tests
import test_group_clone_update_ingress as ingress_tests

pytestmark = pytest.mark.no_postgres
collector_runtime = collector_tests.collector_runtime
TASK_ID = "clone-collector-task"
PEER_ID = "-10011"


@pytest.mark.parametrize("status", ["slice", "too_long"])
def test_paused_task_survives_channel_block_and_recovery(collector_runtime, status):
    with collector_runtime() as session:
        task = session.get(Task, TASK_ID)
        task.status = "paused"
        task.stats = {"clone_start_state": "paused"}
        session.commit()
        _apply(session, status, final=False)
        session.commit()
        session.expire_all()
        assert task.status == "paused"
        assert task.stats["clone_start_state"] == "paused"
        _apply(session, "live", final=True)
        session.commit()
        session.expire_all()
        assert task.status == "paused"
        assert task.stats["clone_start_state"] == "paused"
        stream = session.scalar(select(CloneSourceStreamState))
        assert stream.state == ("blocked" if status == "too_long" else "catching_up")
        assert consume_clone_deliveries(session, task) == 0
        assert task.status == "paused"
        assert session.scalar(select(func.count()).select_from(CloneSourceEvent)) == 0
        assert session.scalar(select(func.count()).select_from(Action)) == 0


@pytest.mark.parametrize("status", ["stopped", "draft", "completed", "failed"])
def test_collector_does_not_overwrite_unrelated_lifecycle(collector_runtime, status):
    with collector_runtime() as session:
        task = session.get(Task, TASK_ID)
        task.status = status
        task.last_error = "unrelated_failure"
        task.stats = {"clone_start_state": "operator_state"}
        session.commit()
        _apply(session, "slice", final=False)
        _apply(session, "live", final=True)
        session.commit()
        session.expire_all()
        assert task.status == status
        assert task.last_error == "unrelated_failure"
        assert task.stats["clone_start_state"] == "operator_state"


def test_too_long_cannot_be_cleared_by_newer_final_batch(collector_runtime):
    with collector_runtime() as session:
        _apply(session, "too_long", final=False)
        session.commit()
        _apply(session, "live", final=True)
        session.commit()
        session.expire_all()
        task = session.get(Task, TASK_ID)
        stream = session.scalar(select(CloneSourceStreamState))
        assert task.status == "failed"
        assert task.last_error == "group_clone_channel_difference_too_long"
        assert stream.state == "blocked"
        assert stream.channel_pts == 500
        assert stream.difference_cursor["continuity_lost"]["observed_pts"] == 550
        assert consume_clone_deliveries(session, task) == 0


def test_too_long_snapshot_is_not_ingested_as_complete_updates(
    collector_runtime, monkeypatch,
):
    initial = TelegramDifferenceBatch(
        scope="common", status="empty",
        cursor={"pts": 100, "qts": 0, "date": 1000, "seq": 1},
    )
    snapshot = TelegramDifferenceBatch(
        scope="channel", status="too_long", cursor={"pts": 550},
        updates=(collector_tests._channel_update(),), final=False,
    )
    monkeypatch.setattr(collector.gateway, "fetch_raw_authorization_update_state", lambda **_: initial)
    monkeypatch.setattr(collector.gateway, "fetch_raw_channel_difference", lambda *_, **__: snapshot)
    result = collector.drain_telegram_update_collector(collector_runtime, tenant_id=1)
    assert result.error_count == 0
    with collector_runtime() as session:
        for model in (TelegramAuthorizationUpdateEvent, TelegramAuthorizationUpdateDelivery):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(CloneSourceStreamState)).state == "blocked"


def test_paused_consumer_keeps_durable_delivery_unconsumed(collector_runtime):
    with collector_runtime() as session:
        task = session.get(Task, TASK_ID)
        task.status = "paused"
        task.stats = {"clone_start_state": "paused"}
        stream = session.scalar(select(CloneSourceStreamState))
        stream.state = "catching_up"
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        state.state = "live"
        state.lease_expires_at = _now() + timedelta(minutes=1)
        ingress_tests._write_ingress(
            session, state, ingress_tests._ingress("paused-delivery", message_id=11, pts=501),
        )
        session.commit()
        assert consume_clone_deliveries(session, task) == 0
        assert stream.state == "catching_up"
        assert task.status == "paused"
        assert session.scalar(select(TelegramAuthorizationUpdateDelivery)).delivery_state == "pending"
        assert session.scalar(select(func.count()).select_from(CloneSourceEvent)) == 0


def test_operator_can_pause_source_failure_without_clearing_evidence(collector_runtime):
    with collector_runtime() as session:
        _apply(session, "too_long", final=False)
        task = session.get(Task, TASK_ID)
        pause_group_clone(task)
        session.commit()
        _apply(session, "live", final=True)
        session.commit()
        assert task.status == "paused"
        assert task.last_error == "group_clone_channel_difference_too_long"
        assert session.scalar(select(CloneSourceStreamState)).state == "blocked"


@pytest.mark.parametrize("entry", [resume_group_clone, start_existing_group_clone])
def test_lifecycle_cannot_erase_lost_continuity(collector_runtime, entry):
    with collector_runtime() as session:
        _apply(session, "too_long", final=False)
        session.commit()
        task = session.get(Task, TASK_ID)
        with pytest.raises(ValueError, match="source_continuity_lost"):
            entry(session, task)
        assert task.status == "failed"
        assert task.last_error == "group_clone_channel_difference_too_long"


@pytest.mark.parametrize("scope", ["old_epoch", "deleted"])
def test_channel_batch_does_not_change_inactive_scope(collector_runtime, scope):
    with collector_runtime() as session:
        task = session.get(Task, TASK_ID)
        if scope == "old_epoch":
            task.task_lifecycle_epoch += 1
        else:
            task.deleted_at = _now()
        session.commit()
        _apply(session, "too_long", final=False)
        session.commit()
        assert task.status == "running"
        assert task.last_error == ""
        assert session.scalar(select(CloneSourceStreamState)).state == "live"


def test_legacy_too_long_is_preserved_even_when_next_page_is_incomplete(collector_runtime):
    with collector_runtime() as session:
        task = session.get(Task, TASK_ID)
        task.status = "failed"
        task.last_error = "group_clone_channel_difference_too_long"
        session.scalar(select(CloneSourceStreamState)).state = "gap"
        session.commit()
        _apply(session, "slice", final=False)
        _apply(session, "live", final=True)
        session.commit()
        assert task.status == "failed"
        assert task.last_error == "group_clone_channel_difference_too_long"
        assert session.scalar(select(CloneSourceStreamState)).state == "blocked"


def _apply(session, status, *, final):
    state = session.scalar(select(TelegramAuthorizationUpdateState))
    batch = TelegramDifferenceBatch(
        scope="channel", status=status, cursor={"pts": 550}, final=final,
    )
    apply_channel_batch(session, state, batch, peer_id=PEER_ID)


def test_channel_slice_continues_from_persisted_cursor_until_final(
    collector_runtime,
    monkeypatch,
) -> None:
    _mark_collector_gap(collector_runtime)
    common = TelegramDifferenceBatch(
        scope="common",
        status="live",
        cursor={"pts": 100, "qts": 0, "date": 1000, "seq": 1},
        final=True,
    )
    batches = [
        TelegramDifferenceBatch(
            scope="channel", status="slice", cursor={"pts": 550}, final=False,
        ),
        TelegramDifferenceBatch(
            scope="channel", status="live", cursor={"pts": 551}, final=True,
        ),
    ]
    requested_pts: list[int] = []

    def fetch_channel(_peer_id, pts, **_kwargs):
        requested_pts.append(pts)
        return batches.pop(0)

    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_difference",
        lambda *_args, **_kwargs: common,
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_channel_difference",
        fetch_channel,
    )

    first = collector.drain_telegram_update_collector(collector_runtime, tenant_id=1)
    with collector_runtime() as session:
        task = session.get(Task, "clone-collector-task")
        stream = session.scalar(select(CloneSourceStreamState))
        assert first.error_count == 0
        assert task.status == "failed"
        assert stream.state == "gap"
    second = collector.drain_telegram_update_collector(collector_runtime, tenant_id=1)

    with collector_runtime() as session:
        task = session.get(Task, "clone-collector-task")
        stream = session.scalar(select(CloneSourceStreamState))
        assert second.error_count == 0
        assert requested_pts == [500, 550]
        assert task.status == "running"
        assert stream.state == "catching_up"

def _mark_collector_gap(collector_runtime) -> None:
    with collector_runtime() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        state.state = "gap"
        state.common_pts = 100
        state.common_date = 1000
        task = session.get(Task, "clone-collector-task")
        task.status = "failed"
        task.last_error = "group_clone_source_pts_gap"
        stream = session.scalar(select(CloneSourceStreamState))
        stream.state = "gap"
        session.commit()
