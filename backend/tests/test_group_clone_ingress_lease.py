"""Exercise PostgreSQL-shaped lease timestamps through the Clone lifecycle."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Task
from app.timezone import BEIJING_TZ, as_beijing_aware
from app.services.task_center import (
    group_clone_precheck, group_clone_read_model, group_clone_source_stream,
    telegram_update_ingress,
)
import test_group_clone_update_ingress as ingress_tests

pytestmark = pytest.mark.no_postgres
clone_ingress_session = ingress_tests.clone_ingress_session
NOW = datetime(2026, 9, 9, 17, 0)
REPRESENTATIONS = (None, BEIJING_TZ, timezone.utc)
ENTRYPOINTS = ("precheck", "read_model", "start", "ingress")


@pytest.mark.parametrize("zone", REPRESENTATIONS)
@pytest.mark.parametrize("seconds", [-1, 0, 60])
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_clone_lease_uses_same_instant(
    clone_ingress_session, monkeypatch, *, zone, seconds, entrypoint,
):
    session, state = clone_ingress_session
    for module in (group_clone_precheck, group_clone_read_model,
                   group_clone_source_stream, telegram_update_ingress):
        monkeypatch.setattr(module, "_now", lambda: NOW)
    expires = NOW + timedelta(seconds=seconds)
    state.lease_expires_at = as_beijing_aware(expires).astimezone(zone) if zone else expires
    task = session.get(Task, "clone-ingress-task")
    expected = seconds > 0
    if entrypoint == "precheck":
        assert group_clone_precheck._update_ingress_ready(state) is expected
        return
    if entrypoint == "read_model":
        assert group_clone_read_model.update_ingress_status(session, task)["owner_lease_healthy"] is expected
        return
    if expected:
        _write_or_start(session, state, task=task, entrypoint=entrypoint)
        return
    error = "group_clone_shared_ingress_not_live" if entrypoint == "start" else "telegram_update_collector_lease_expired"
    with pytest.raises((ValueError, RuntimeError), match=error):
        _write_or_start(session, state, task=task, entrypoint=entrypoint)


def _write_or_start(session, state, *, task, entrypoint):
    if entrypoint == "start":
        group_clone_source_stream._apply_start_boundary(
            session, task, {"channel_pts": 100, "max_message_id": 10},
        )
        return
    ingress_tests._write_ingress(
        session, state, ingress_tests._ingress("lease-audit", message_id=11, pts=101),
    )
