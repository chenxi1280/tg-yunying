from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.telegram import GroupMessageSnapshot, SendResult
from app.models import Action, ExecutionAttempt, RuleSetVersion, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneAlbumManifest,
    CloneMessagePart,
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
    child_event = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.stream_order_no == 6,
    ))
    child_event.entities = [{"type": "bold", "offset": 0, "length": 5}]
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
    prefix = quoted.payload["content"].removesuffix("child")
    assert quoted.payload["entities"][0]["offset"] == len(prefix.encode("utf-16-le")) // 2
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


def test_target_control_right_is_rechecked_before_attempt(client_and_session, monkeypatch):
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
    send = _action(session, task, 1)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, "7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, send, project_task_stats=False)
    _add_event(session, task, order=2, event_type="message_delete")
    assert build_plan(session, task) == 1
    delete = _action(session, task, 2)
    gateway_calls: list[bool] = []
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.fetch_raw_group_admin_rights",
        lambda *args, **kwargs: {"delete_messages": False},
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.delete_raw_mtproto_messages",
        lambda *args, **kwargs: gateway_calls.append(True),
    )

    assert dispatch_action(session, delete, project_task_stats=False)
    assert delete.status == "failed"
    assert "group_clone_target_control_right_missing" in delete.result["error_message"]
    assert gateway_calls == []
    assert session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == delete.id,
    )) is None


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
    listed = client.get(
        f"/api/tasks/{task.id}/clone-manual-reviews",
        headers=_auth_headers(),
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["allowed_decisions"] == [
        "drop", "keep_blocked",
    ]
    forbidden_release = client.post(
        url,
        json={
            **payload,
            "decision": "release",
            "client_request_id": "manual-review-release-1",
        },
        headers=_auth_headers(),
    )
    assert forbidden_release.status_code == 409
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


def test_source_event_uses_frozen_config_after_task_patch(client_and_session):
    client, session = client_and_session
    task = _running_task(client, session)
    frozen = dict(task.type_config)
    _add_event(session, task, order=1, event_type="message_new", content="frozen")
    event = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
    ))
    event.config_snapshot = frozen
    current = dict(task.type_config)
    current["lifecycle"] = {
        **dict(current["lifecycle"]),
        "unknown_deadline_seconds": 60,
    }
    task.type_config = current
    task.config_revision += 1
    session.flush()

    assert build_plan(session, task) == 1

    obligation = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
    ))
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = obligation.unknown_deadline_at - now_utc_naive
    assert delta.total_seconds() > 800


def test_source_event_without_config_snapshot_fails_closed(client_and_session):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(session, task, order=1, event_type="message_new", content="missing")
    event = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
    ))
    event.config_snapshot = {}
    session.flush()

    with pytest.raises(RuntimeError, match="group_clone_event_config_snapshot_missing"):
        build_plan(session, task)


def test_edit_reuses_sanitizer_and_blocks_entity_transform(
    client_and_session,
    monkeypatch,
):
    client, session = client_and_session
    task = _running_task(client, session)
    rule = session.scalar(select(RuleSetVersion).where(
        RuleSetVersion.rule_set_id == 31,
        RuleSetVersion.version == 1,
    ))
    rule.output_checks = {
        "forbidden_keywords": ["risk"],
        "failure_strategy": "transform_once_drop",
    }
    rule.transforms = {"keyword_replacements": {"risk": "safe"}}
    _add_event(session, task, order=1, event_type="message_new", content="original")
    assert build_plan(session, task) == 1
    send = _action(session, task, 1)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(
            True, "7101", remote_mutation_started=True,
        ),
    )
    assert dispatch_action(session, send, project_task_stats=False)
    _add_event(session, task, order=2, event_type="message_edit", content="risk")
    edit_event = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.stream_order_no == 2,
    ))
    edit_event.entities = [{"type": "bold", "offset": 0, "length": 4}]

    assert build_plan(session, task) == 0

    obligation = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.stream_order_no == 2,
    ))
    assert obligation.state == "waiting_manual_review"
    assert obligation.error_code == (
        "group_clone_entity_rebuild_required_after_transform"
    )
    assert _action(session, task, 2) is None


def test_new_message_blocks_entities_when_outbound_filter_changes_text(
    client_and_session,
):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(
        session, task, order=1, event_type="message_new",
        content="hello  world",
    )
    event = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
    ))
    event.entities = [{"type": "bold", "offset": 7, "length": 5}]

    assert build_plan(session, task) == 0

    obligation = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
    ))
    assert obligation.state == "waiting_manual_review"
    assert obligation.error_code == (
        "group_clone_entity_rebuild_required_after_sanitization"
    )


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

    assert build_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))
    assert action.payload["mutation_kind"] == "sendMultiMedia"
    assert [item["source_message_id"] for item in action.payload["media_items"]] == [501, 502]
    obligations = list(session.scalars(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
    ).order_by(CloneDeliveryObligation.stream_order_no)))
    assert [row.state for row in obligations] == ["action_bound", "superseded"]
    _add_event(session, task, order=3, event_type="message_new", source_message_id=503, content="after album")
    assert build_plan(session, task) == 1


def test_photo_without_caption_dispatches_media_and_closes_typed_chain(
    client_and_session, monkeypatch,
):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(
        session, task, order=1, event_type="message_new",
        media_type="photo", content="",
    )

    assert build_plan(session, task) == 1
    action = _action(session, task, 1)
    assert action.payload["mutation_kind"] == "sendMedia"
    assert action.payload["content"] == ""
    assert len(action.payload["media_items"]) == 1
    called = {}

    def send_media(*args, **kwargs):
        called.update(kwargs)
        return SendResult(
            True, "8101", remote_message_ids=("8101",),
            remote_mutation_started=True,
        )

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_media",
        send_media,
    )
    assert dispatch_action(session, action, project_task_stats=False)

    obligation = _obligation(session, action)
    part = session.scalar(select(CloneMessagePart).where(
        CloneMessagePart.obligation_id == obligation.id,
    ))
    assert called["source_peer_id"] == "-100111"
    assert obligation.state == "succeeded"
    assert part.target_message_id == 8101


def test_album_late_part_after_freeze_is_explicit(client_and_session):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(session, task, order=1, event_type="message_new", grouped_id="album-late", media_type="photo")
    _add_event(session, task, order=2, event_type="message_new", source_message_id=502, grouped_id="album-late", media_type="photo")
    assert build_plan(session, task) == 0
    manifest = session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
    ))
    manifest.quiet_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert build_plan(session, task) == 1
    _add_event(session, task, order=3, event_type="message_new", source_message_id=503, grouped_id="album-late", media_type="photo")

    assert build_plan(session, task) == 0
    late = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.stream_order_no == 3,
    ))
    assert late.state == "waiting_manual_review"
    assert late.error_code == "album_late_part_after_freeze"


def test_unsupported_media_never_degrades_to_file_or_text(client_and_session):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(
        session, task, order=1, event_type="message_new",
        media_type="location", content="location caption",
    )

    assert build_plan(session, task) == 0
    obligation = session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
    ))
    assert obligation.state == "waiting_manual_review"
    assert obligation.error_code == "unsupported_media:location:block"
    assert _action(session, task, 1) is None


def test_media_acquisition_failure_is_safely_not_sent(client_and_session, monkeypatch):
    client, session = client_and_session
    task = _running_task(client, session)
    _add_event(
        session, task, order=1, event_type="message_new",
        media_type="photo", content="caption",
    )
    assert build_plan(session, task) == 1
    action = _action(session, task, 1)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_media",
        lambda *args, **kwargs: SendResult(
            False, failure_type="group_clone_media_download_empty",
            detail="source media unavailable", remote_mutation_started=False,
        ),
    )

    assert dispatch_action(session, action, project_task_stats=False)

    assert action.status == "failed"
    assert _obligation(session, action).state == "failed_terminal"


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


def _running_task(client, session):
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
    return task


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
        config_snapshot=dict(task.type_config or {}),
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
