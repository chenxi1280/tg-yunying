"""Committed channel pages prove continuity across independent PG sessions."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services._common import _now
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from tests import test_runtime_retention_protection_postgres as postgres_fixtures
import test_group_clone_update_collector as collector_tests
from test_group_clone_update_ingress import _ingress, _write_ingress
from test_telegram_channel_difference_ranges import _range

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = postgres_fixtures.database


def test_committed_pages_cover_head_without_advancing_to_proof_end(database):
    with Session(database, autoflush=False) as collector:
        collector_tests._seed_runtime(collector)
        state = collector.scalar(select(TelegramAuthorizationUpdateState))
        state.state = "live"
        state.lease_expires_at = _now() + timedelta(minutes=1)
        collector.flush()
        _write_ingress(collector, state, replace(
            _ingress("pg-out-of-order", message_id=11, pts=502), cursor_scope="event_only",
        ))
        _range(collector, state, start=500, end=501)
        _range(collector, state, start=501, end=503)
        collector.commit()
    with Session(database, autoflush=False) as consumer:
        task = consumer.get(Task, "clone-collector-task")
        assert consume_clone_deliveries(consumer, task) == 1
        consumer.commit()
    with Session(database) as readback:
        assert readback.scalar(select(CloneSourceStreamState)).channel_pts == 502
        assert readback.scalar(select(CloneSourceEvent)).source_message_id == 11
