from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.telegram import GroupMessageSnapshot, SendResult
from app.models import Action, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneAlbumManifest,
    CloneSourceEvent,
    CloneSourceStreamState,
    CloneTopicMap,
)
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.group_clone import build_plan
from app.services.task_center.service import _recover_stale_executing_actions

from test_group_clone_api import _auth_headers, client_and_session

pytestmark = pytest.mark.no_postgres


def test_edit_delete_pin_and_topic_use_typed_mutation_chain(client_and_session, monkeypatch):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(),
        headers=_auth_headers(),
    )
    assert response.status_code == 201
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.fetch_raw_group_admin_rights",
        lambda *args, **kwargs: {
            "manage_topics": True,
            "delete_messages": True,
            "pin_messages": True,
        },
    )

    _add_event(session, task, order=1, event_type="message_new", content="v1")
    assert build_plan(session, task) == 1
    send = _action(session, task, 1)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, send, project_task_stats=False)

    _add_event(session, task, order=2, event_type="message_edit", content="v2")
    assert build_plan(session, task) == 1
    edit = _action(session, task, 2)
    assert edit.payload["mutation_kind"] == "editMessage"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.edit_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, edit, project_task_stats=False)
    assert _obligation(session, edit).state == "succeeded"

    _add_event(
        session, task, order=3, event_type="message_pin",
        poll_snapshot={"pinned": True},
    )
    assert build_plan(session, task) == 1
    pin = _action(session, task, 3)
    assert pin.payload["mutation_kind"] == "pinMessage"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.pin_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, pin, project_task_stats=False)

    _add_event(session, task, order=4, event_type="message_delete")
    assert build_plan(session, task) == 1
    delete = _action(session, task, 4)
    assert delete.payload["mutation_kind"] == "deleteMessages"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.delete_raw_mtproto_messages",
        lambda *args, **kwargs: SendResult(True, remote_mutation_started=True),
    )
    assert dispatch_action(session, delete, project_task_stats=False)

    _add_event(
        session, task, order=5, event_type="message_new", source_message_id=901,
        source_top_message_id=900, content="message in existing source topic",
    )
    monkeypatch.setattr(
        "app.services.task_center.group_clone_lifecycle_materializer.gateway.fetch_raw_forum_topic",
        lambda *args, **kwargs: {
            "topic_id": 900,
            "title": "Topic A",
            "icon_color": 7322096,
            "icon_emoji_id": "",
            "closed": False,
            "hidden": False,
        },
    )
    assert build_plan(session, task) == 1
    topic_action = _action(session, task, 5)
    assert topic_action.payload["mutation_kind"] == "createForumTopic"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.create_raw_mtproto_forum_topic",
        lambda *args, **kwargs: SendResult(True, "9900", remote_mutation_started=True),
    )
    assert dispatch_action(session, topic_action, project_task_stats=False)
    topic = session.scalar(select(CloneTopicMap).where(CloneTopicMap.task_id == task.id))
    assert topic.state == "ready"
    assert topic.target_top_message_id == 9900
    assert _obligation(session, topic_action).state == "observed"
    assert build_plan(session, task) == 1
    topic_send = session.scalar(select(Action).where(
        Action.obligation_id == topic_action.obligation_id,
        Action.action_type == "group_clone_send",
    ))
    assert topic_send.payload["target_top_message_id"] == 9900
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "9901", remote_mutation_started=True),
    )
    assert dispatch_action(session, topic_send, project_task_stats=False)

    _add_event(
        session, task, order=6, event_type="message_new", source_message_id=902,
        content="child", reply_to_message_id=400,
    )
    monkeypatch.setattr(
        "app.services.task_center.group_clone_reply.gateway.fetch_group_message",
        lambda *args, **kwargs: GroupMessageSnapshot(
            remote_message_id="400",
            sender_name="parent",
            sender_peer_id="source-user-parent",
            sender_peer_type="user",
            content="parent body",
        ),
    )
    assert build_plan(session, task) == 1
    quoted = _action(session, task, 6)
    assert quoted.payload["reply_to_message_id"] is None
    assert quoted.payload["content"].startswith("> parent body\n\nchild")
    assert _obligation(session, quoted).degradation_reason == "orphan_reply_quote_fallback"


def test_edit_before_gateway_supersedes_original_send(client_and_session):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    _add_event(session, task, order=1, event_type="message_new", content="v1")
    assert build_plan(session, task) == 1
    original = _action(session, task, 1)
    _add_event(session, task, order=2, event_type="message_edit", content="v2")
    assert build_plan(session, task) == 1
    replacement = _action(session, task, 2)
    assert original.status == "cancelled"
    assert _obligation(session, original).state == "superseded"
    assert replacement.action_type == "group_clone_send"
    assert replacement.payload["content"] == "v2"


def test_unknown_edit_is_closed_by_exact_desired_state_readback(
    client_and_session,
    monkeypatch,
):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.fetch_raw_group_admin_rights",
        lambda *args, **kwargs: {
            "manage_topics": True,
            "delete_messages": True,
            "pin_messages": True,
        },
    )
    _add_event(session, task, order=1, event_type="message_new", content="v1")
    assert build_plan(session, task) == 1
    send = _action(session, task, 1)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, send, project_task_stats=False)
    _add_event(session, task, order=2, event_type="message_edit", content="v2")
    assert build_plan(session, task) == 1
    edit = _action(session, task, 2)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.edit_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(
            False, failure_type="timeout", detail="RPC result unknown",
            remote_mutation_started=True,
        ),
    )
    assert dispatch_action(session, edit, project_task_stats=False)
    assert edit.status == "unknown_after_send"

    monkeypatch.setattr(
        "app.services.task_center.service.gateway.supports_group_clone_desired_state_probe",
        True,
    )
    monkeypatch.setattr(
        "app.services.task_center.service.gateway.fetch_group_message",
        lambda *args, **kwargs: GroupMessageSnapshot(
            remote_message_id="7001", sender_name="clone", content="v2",
        ),
    )
    assert _recover_stale_executing_actions(session, limit=10) == 1
    session.refresh(edit)
    assert edit.status == "success"
    assert _obligation(session, edit).state == "succeeded"


def test_manual_review_decision_is_revisioned_and_idempotent(client_and_session):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone",
        json=_create_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    _add_event(session, task, order=1, event_type="message_new", content="blocked")
    event = session.scalar(select(CloneSourceEvent).where(CloneSourceEvent.task_id == task.id))
    obligation = CloneDeliveryObligation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        source_event_id=event.id,
        obligation_kind="send",
        stream_order_no=1,
        sequencer_id=1,
        planned_at=event.observed_at,
        state="waiting_manual_review",
        error_code="protected_content",
    )
    session.add(obligation)
    session.commit()
    payload = {
        "expected_review_revision": 1,
        "decision": "drop",
        "reason": "受保护内容不复制",
        "client_request_id": "manual-review-request-1",
    }
    url = f"/api/tasks/{task.id}/clone-manual-reviews/{obligation.id}/decision"
    first = client.post(url, json=payload, headers=_auth_headers())
    replay = client.post(url, json=payload, headers=_auth_headers())
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["obligation_state"] == "filtered"
    conflict = client.post(
        url,
        json={**payload, "decision": "release"},
        headers=_auth_headers(),
    )
    assert conflict.status_code == 409


def test_incomplete_album_does_not_permanently_block_stream(client_and_session):
    client, session = client_and_session
    response = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_create_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, response.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    _add_event(session, task, order=1, event_type="message_new", grouped_id="album-1", media_type="photo")
    _add_event(session, task, order=2, event_type="message_new", source_message_id=502, grouped_id="album-1", media_type="photo")

    assert build_plan(session, task) == 0
    manifest = session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
    ))
    assert manifest.items_total == 2
    manifest.quiet_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    manifest.max_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert build_plan(session, task) == 0
    obligations = list(session.scalars(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
    ).order_by(CloneDeliveryObligation.stream_order_no)))
    assert [row.state for row in obligations] == ["filtered", "filtered"]
    _add_event(session, task, order=3, event_type="message_new", source_message_id=503, content="after album")
    assert build_plan(session, task) == 1


def _create_payload():
    return {
        "name": "Clone lifecycle",
        "source": {
            "internal_group_id": 21,
            "operation_target_id": 11,
            "peer_type": "channel",
            "peer_id": "-100111",
            "listener_account_id": 101,
            "authorization_id": 201,
            "authorization_mode": "admin_authorized",
        },
        "target": {
            "internal_group_id": 22,
            "operation_target_id": 12,
            "peer_type": "channel",
            "peer_id": "-100222",
            "control_account_id": 102,
            "control_authorization_id": 202,
        },
        "sender_pool": {"account_ids": [101, 102, 103]},
        "pacing": {"min_delay_ms": 1, "max_delay_ms": 1, "strict_target_order": True},
        "content": {"rule_set_id": 31, "rule_set_version": 1},
        "lifecycle": {"start_mode": "start_from_now", "failure_order_policy": "fail_stop"},
    }


def _add_event(
    session, task, *, order, event_type, content="", source_message_id=501,
    source_top_message_id=None, poll_snapshot=None, reply_to_message_id=None,
    grouped_id=None, media_type="text",
):
    session.add(CloneSourceEvent(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        source_peer_type="channel",
        source_peer_id="-100111",
        source_message_id=source_message_id,
        event_type=event_type,
        event_identity_hash=f"event-{order}-{event_type}",
        apply_order_key=f"{order:04d}",
        stream_order_no=order,
        sender_peer_type="user",
        sender_peer_id="source-user-501",
        reply_to_message_id=reply_to_message_id,
        source_top_message_id=source_top_message_id,
        grouped_id=grouped_id,
        media_type=media_type,
        content=content,
        entities=[],
        poll_snapshot=poll_snapshot or {},
        content_fingerprint=f"content-{order}",
        config_revision=task.config_revision,
    ))
    session.flush()


def _action(session, task, order):
    obligation = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.stream_order_no == order,
    ))
    return session.scalar(select(Action).where(Action.obligation_id == obligation.id))


def _obligation(session, action):
    return session.get(CloneDeliveryObligation, action.obligation_id)
