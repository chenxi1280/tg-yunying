"""Rejected media must never cross the real planner/dispatcher send boundary."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.integrations.telegram import SendResult
from app.models import Action, RuleSetVersion
from app.models.group_clone import (
    CloneAlbumManifest, CloneDeliveryObligation, CloneMessagePart,
    CloneSourceEvent, TelegramGatewayMutationIdentity,
)
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.group_clone import build_plan

import test_group_clone_api as clone_api
from test_group_clone_lifecycle import _add_event, _running_task

pytestmark = pytest.mark.no_postgres
client_and_session = clone_api.client_and_session
RULE_VERSION_ID = 32
REJECTED_CONTENT = "audit_rejected"
SOURCE_ID_BASE = 500
REMOTE_ID_BASE = 8100
ALBUM_ORDERS = (1, 2)


@pytest.mark.parametrize("media_type", ["text", "photo"])
@pytest.mark.parametrize("rule_kind", ["input", "output"])
def test_rejected_message_does_not_materialize(
    client_and_session, media_type, *, rule_kind, monkeypatch,
):
    client, session = client_and_session
    task = _running_task(client, session)
    _set_rejection_rule(session, rule_kind)
    _add_event(session, task, order=1, event_type="message_new",
               media_type=media_type, content=REJECTED_CONTENT)
    build_plan(session, task)
    _assert_no_dispatch(session, task, monkeypatch)
    assert _first_obligation(session, task).state == "filtered"
    assert build_plan(session, task) == 0
    _assert_no_send_records(session, task)


@pytest.mark.parametrize("case", ["protected", "input", "output", "entities"])
@pytest.mark.parametrize("rejected_order", ALBUM_ORDERS)
def test_album_items_are_admitted_before_action(
    client_and_session, case, *, rejected_order, monkeypatch,
):
    client, session = client_and_session
    task = _running_task(client, session)
    _prepare_album(session, task, case=case, rejected_order=rejected_order)
    assert build_plan(session, task) == 0
    manifest = session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
    ))
    if manifest is not None:
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        manifest.quiet_deadline_at = expired
        manifest.max_deadline_at = expired
    assert build_plan(session, task) == 0
    _assert_no_dispatch(session, task, monkeypatch)
    expected = "filtered" if case in {"input", "output"} else "waiting_manual_review"
    assert _first_obligation(session, task).state == expected
    assert build_plan(session, task) == 0
    _assert_no_send_records(session, task)


@pytest.mark.parametrize("rule_kind", ["input", "output"])
def test_empty_caption_does_not_bypass_explicit_rules(client_and_session, rule_kind):
    client, session = client_and_session
    task = _running_task(client, session)
    version = session.get(RuleSetVersion, RULE_VERSION_ID)
    if rule_kind == "input":
        version.filters = {"only_text": True}
    else:
        version.output_checks = {"min_length": 1}
    _add_event(session, task, order=1, event_type="message_new", media_type="photo")
    assert build_plan(session, task) == 0
    assert _first_obligation(session, task).state == "filtered"
    _assert_no_send_records(session, task)


def _set_rejection_rule(session, rule_kind):
    version = session.get(RuleSetVersion, RULE_VERSION_ID)
    if rule_kind == "input":
        version.filters = {"keyword_blacklist": [REJECTED_CONTENT]}
    else:
        version.output_checks = {"forbidden_keywords": [REJECTED_CONTENT]}
    return version


def _prepare_album(session, task, *, case, rejected_order):
    if case in {"input", "output", "entities"}:
        version = _set_rejection_rule(session, case)
        if case == "entities":
            version.transforms = {"keyword_replacements": {REJECTED_CONTENT: "changed"}}
    for order in ALBUM_ORDERS:
        _add_event(session, task, order=order, event_type="message_new",
            source_message_id=SOURCE_ID_BASE + order, grouped_id="audit-album", media_type="photo",
            content=REJECTED_CONTENT if order == rejected_order else "allowed")
    rejected = session.scalar(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id, CloneSourceEvent.stream_order_no == rejected_order,
    ))
    if case == "protected":
        rejected.protected_content = True
    if case == "entities":
        rejected.entities = [{"type": "bold", "offset": 0, "length": len(REJECTED_CONTENT)}]


def _assert_no_dispatch(session, task, monkeypatch):
    calls = []

    def send_media(*args, **kwargs):
        calls.append(True)
        remote_ids = tuple(str(REMOTE_ID_BASE + i) for i in range(len(kwargs["media_items"])))
        return SendResult(True, remote_ids[0], remote_message_ids=remote_ids,
                          remote_mutation_started=True)

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_raw_mtproto_media", send_media)
    actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()
    for action in actions:
        dispatch_action(session, action, project_task_stats=False)
    assert not calls, "Rejected media reached Telegram boundary"
    assert not actions


def _first_obligation(session, task):
    return session.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id, CloneDeliveryObligation.stream_order_no == 1,
    ))


def _assert_no_send_records(session, task):
    for model in (Action, TelegramGatewayMutationIdentity, CloneMessagePart):
        assert session.scalar(select(model).where(model.task_id == task.id)) is None
