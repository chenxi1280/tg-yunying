"""Counterexamples from the channel pacing and visibility review."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telethon.tl.types import MessageEmpty, PeerChannel

from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import Action, SourcePacingAdmission
from app.services.task_center.direct_action_claims import _reserved_source_tails
from app.services.task_center.dispatcher import _post_send_visibility_target_peer
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from app.services.task_center.source_pacing_admission_settlement import (
    settle_source_pacing_admission, unsettled_prior_admission,
)
from tests.test_source_pacing_admission import NOW, _paced_action, session


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("case", [
    ("actual-discussion", "old-discussion", "actual-discussion"),
    ("", "discussion", "discussion"),
    ("", "", "source-channel"),
])
def test_visibility_preserves_actual_peer(case):
    actual, discussion, expected = case
    action = Action(action_type="post_comment", payload={
        "channel_id": "source-channel", "actual_target_peer": actual,
        "discussion_peer_id": discussion,
    })
    assert _post_send_visibility_target_peer(None, action) == expected


class VisibilityClient:
    def __init__(self, messages):
        self.messages = messages
        self.lookups = []
        self.extra_requests = []

    async def is_user_authorized(self):
        return True

    async def get_messages(self, target, ids):
        self.lookups.append((target.id, ids))
        if isinstance(self.messages, Exception):
            raise self.messages
        return self.messages

    async def __call__(self, request):
        self.extra_requests.append(request)
        raise RuntimeError("unrelated discussion lookup must not run")


def visibility_probe(monkeypatch, messages, *, broadcast=False):
    client = VisibilityClient(messages)
    gateway = TelethonTelegramGateway()

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(id=100, broadcast=broadcast)

    async def get_client(*_args):
        return client

    monkeypatch.setattr(gateway, "_get_or_create_client", get_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", resolve)
    result = asyncio.run(gateway._probe_message_visible_async(
        "", "frozen-peer", 863, SimpleNamespace(),
    ))
    return result, client


@pytest.mark.parametrize("messages", [[], [None], [MessageEmpty(id=863, peer_id=PeerChannel(100))]])
def test_empty_visibility_is_scoped_to_requested_peer(monkeypatch, messages):
    result, client = visibility_probe(monkeypatch, messages, broadcast=True)
    assert result.ok is True and result.visible is False
    assert client.lookups == [(100, [863])]
    assert client.extra_requests == []


def test_visibility_probe_error_stays_unknown(monkeypatch):
    result, client = visibility_probe(monkeypatch, RuntimeError("probe unavailable"))
    assert result.ok is False and result.visible is None
    assert result.detail != "message_missing"
    assert client.extra_requests == []


def test_exact_peer_message_is_visible(monkeypatch):
    result, client = visibility_probe(monkeypatch, [SimpleNamespace(id=863)])
    assert result.ok is True and result.visible is True
    assert result.remote_message_id == "863"
    assert client.extra_requests == []


@pytest.mark.parametrize("gap", [30, 180, 864, 86400])
def test_reserved_tail_keeps_frozen_gap(gap):
    current = SimpleNamespace(execute=lambda _query: [("state", NOW, gap)])
    assert _reserved_source_tails(current, {"state"}) == {
        "state": NOW + timedelta(seconds=gap),
    }


def reserved_attempt(session):
    action, attempt = _paced_action(
        session, task_id="review-task", slot_id="review-slot", action_id="review-action",
    )
    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    admission = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == action.id,
    ))
    return action, attempt, admission


@pytest.mark.parametrize("case", [
    ("call_started", "before_call", ""),
    ("remote_unknown", "result_unknown", ""),
    ("remote_unknown", "result_unknown", "863"),
    ("call_started", "success", "863"),
])
def test_unsettled_admission_is_not_released_without_reconcile(session, case):
    state, status, remote_id = case
    action, attempt, admission = reserved_attempt(session)
    admission.state = state
    attempt.status = status
    attempt.remote_message_id = remote_id
    version = admission.version
    assert unsettled_prior_admission(session, action) is admission
    assert admission.state == state and admission.version == version


@pytest.mark.parametrize("action_status,attempt_status", [
    ("unknown_after_send", "result_unknown"),
    ("unknown_after_send", "failed"),
    ("pending", "result_unknown"),
])
def test_unknown_settlement_does_not_require_gateway_timestamp(session, action_status, attempt_status):
    action, attempt, admission = reserved_attempt(session)
    action.status = action_status
    attempt.status = attempt_status
    assert attempt.gateway_call_started_at is None
    settle_source_pacing_admission(action, attempt)
    assert admission.state == "remote_unknown"
