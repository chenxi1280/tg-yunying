from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Task
from app.models.group_clone import (
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
)
from app.models.telegram_authorities import TelegramGroupMutationAuthority
from app.models.telegram_updates import TelegramAuthorizationUpdateSubscription
from app.services.task_center.group_mutation_authority import release_exclusive_authority

from test_group_clone_api import _auth_headers, client_and_session
from test_group_clone_lifecycle import _create_payload

pytestmark = pytest.mark.no_postgres


def test_created_clone_can_be_started_with_runtime_contract(client_and_session):
    client, session = client_and_session
    created = client.post(
        "/api/tasks/group-clone",
        json=_create_payload(),
        headers=_auth_headers(),
    )
    task_id = created.json()["task_id"]

    started = client.post(
        f"/api/tasks/{task_id}/start",
        json={"start_operation_id": "clone-start-runtime-1"},
        headers=_auth_headers(),
    )

    assert started.status_code == 200
    task = session.get(Task, task_id)
    assert task.status == "pending"
    assert task.stats["clone_start_state"] == "starting"
    assert session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task_id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    )) is not None
    assert session.scalar(select(CloneTargetRouteSnapshot).where(
        CloneTargetRouteSnapshot.task_id == task_id,
        CloneTargetRouteSnapshot.epoch == task.task_lifecycle_epoch,
    )) is not None


def test_pause_resume_preserves_clone_epoch_and_stream(client_and_session):
    client, session = client_and_session
    task = _running_clone(client, session)
    original_epoch = task.task_lifecycle_epoch
    original_stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))

    paused = client.post(
        f"/api/tasks/{task.id}/pause", headers=_auth_headers(),
    )
    resumed = client.post(
        f"/api/tasks/{task.id}/resume", headers=_auth_headers(),
    )

    assert paused.status_code == 200
    assert resumed.status_code == 200
    session.refresh(task)
    assert task.status == "running"
    assert task.task_lifecycle_epoch == original_epoch
    assert task.stats["clone_start_state"] == "running"
    assert session.get(CloneSourceStreamState, original_stream.id).state == "live"


def test_stop_releases_clone_runtime_and_advances_epoch(client_and_session):
    client, session = client_and_session
    task = _running_clone(client, session)
    original_epoch = task.task_lifecycle_epoch

    stopped = client.post(
        f"/api/tasks/{task.id}/stop",
        json={"reason": "operator stop"},
        headers=_auth_headers(),
    )

    assert stopped.status_code == 200
    session.refresh(task)
    assert task.status == "stopped"
    assert task.task_lifecycle_epoch == original_epoch + 1
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == original_epoch,
    ))
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == original_epoch,
    ))
    authority = session.scalar(select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.target_peer_id == "-100222",
    ))
    assert stream.state == "stopped"
    assert subscription.state == "stopped"
    assert authority.mode == "vacant"


def test_reset_restarts_clone_from_new_epoch(client_and_session):
    client, session = client_and_session
    task = _running_clone(client, session)
    original_epoch = task.task_lifecycle_epoch

    reset = client.post(
        f"/api/tasks/{task.id}/reset",
        json={"reason": "restart from now"},
        headers=_auth_headers(),
    )

    assert reset.status_code == 200
    session.refresh(task)
    assert task.status == "pending"
    assert task.task_lifecycle_epoch == original_epoch + 1
    assert task.stats["clone_start_state"] == "starting"
    assert session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    )) is not None


def test_failed_start_reuses_current_epoch_runtime_rows(client_and_session):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(),
        headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
    ))
    route = session.scalar(select(CloneTargetRouteSnapshot).where(
        CloneTargetRouteSnapshot.task_id == task.id,
    ))
    _mark_start_failed(session, task, stream=stream, subscription=subscription)

    restarted = client.post(
        f"/api/tasks/{task.id}/start",
        json={"start_operation_id": "clone-restart-after-start-failure"},
        headers=_auth_headers(),
    )

    assert restarted.status_code == 200
    session.refresh(task)
    assert task.task_lifecycle_epoch == 1
    assert task.status == "pending"
    assert session.scalars(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    )).all() == [stream]
    assert session.scalars(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
    )).all() == [subscription]
    assert session.scalars(select(CloneTargetRouteSnapshot).where(
        CloneTargetRouteSnapshot.task_id == task.id,
    )).all() == [route]
    assert len(session.scalars(select(CloneTargetExecutionSnapshot).where(
        CloneTargetExecutionSnapshot.route_snapshot_id == route.id,
    )).all()) == 1
    assert stream.state == "initializing"
    assert subscription.state == "initializing"


def _mark_start_failed(session, task, *, stream, subscription) -> None:
    stream.state = "blocked"
    subscription.state = "stopped"
    task.status = "failed"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "start_failed"}
    release_exclusive_authority(
        session,
        task.tenant_id,
        target_peer_type="channel",
        target_peer_id="-100222",
        writer_kind="group_clone",
        writer_id=task.id,
    )
    session.commit()


def _running_clone(client, session):
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(),
        headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    session.commit()
    return task
