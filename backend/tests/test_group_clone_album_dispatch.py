"""Exercise the second media identity through the real planner and dispatcher."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.telegram import SendResult
from app.models.group_clone import (
    CloneAlbumManifest, CloneMessagePart, TelegramGatewayMutationIdentity,
)
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.group_clone import build_plan

import test_group_clone_api as clone_api
from test_group_clone_lifecycle import _action, _add_event, _obligation, _running_task


pytestmark = pytest.mark.no_postgres
client_and_session = clone_api.client_and_session


def _album_action(client, session):
    task = _running_task(client, session)
    for order, source_id in enumerate((501, 502), start=1):
        _add_event(session, task, order=order, event_type="message_new",
            source_message_id=source_id, grouped_id="album-test", media_type="photo")
    assert build_plan(session, task) == 0
    manifest = session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
    ))
    manifest.quiet_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    manifest.max_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert build_plan(session, task) == 1
    return _action(session, task, 1)


def test_album_binds_both_original_identities_before_gateway(client_and_session, monkeypatch):
    client, session = client_and_session
    action = _album_action(client, session)
    media = action.payload["media_items"]
    identities = [session.get(TelegramGatewayMutationIdentity,
        item["gateway_mutation_identity_id"]) for item in media]
    original_random_ids = [item.random_id for item in identities]
    calls = []

    def send_media(*_args, **kwargs):
        assert [item.state for item in identities] == ["attempt_bound", "attempt_bound"]
        assert [item["random_id"] for item in kwargs["media_items"]] == original_random_ids
        calls.append(kwargs)
        return SendResult(True, "8101", remote_message_ids=("8101", "8102"),
            remote_mutation_started=True)

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_raw_mtproto_media",
        send_media)
    assert dispatch_action(session, action, project_task_stats=False)
    assert len(calls) == 1
    obligation = _obligation(session, action)
    assert obligation.state == "succeeded"
    parts = session.scalars(select(CloneMessagePart).where(
        CloneMessagePart.obligation_id == obligation.id,
    )).all()
    assert {part.target_message_id for part in parts} == {8101, 8102}


def test_album_rejects_wrong_second_identity_without_gateway(client_and_session, monkeypatch):
    client, session = client_and_session
    action = _album_action(client, session)
    second = action.payload["media_items"][1]
    identity = session.get(TelegramGatewayMutationIdentity, second["gateway_mutation_identity_id"])
    identity.random_id += 1
    session.flush()

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("invalid second identity must not call Telegram")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_raw_mtproto_media",
        unexpected_call)
    dispatch_action(session, action, project_task_stats=False)
    assert action.status != "success"
    assert "group_clone_media_identity_not_callable" in str(action.result)
