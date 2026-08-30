from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors

from app.models import Action
from scripts.recover_ai_group_rescue_admissions import (
    AdmissionProbeFloodWait,
    _inventory,
    _observe_source,
    _probe_checkpoint,
)
from scripts.recover_ai_group_rescue_binding import _participant_rights


pytestmark = pytest.mark.no_postgres


def test_recovery_inventory_is_ordered_and_status_sensitive() -> None:
    sources = (
        _source("source-a", "closed_unknown", 11),
        _source("source-b", "skipped", 12),
    )

    first = _inventory(sources)
    second = _inventory(sources)

    assert first == second
    assert first["source_count"] == 2
    assert first["status_counts"] == {"closed_unknown": 1, "skipped": 1}
    assert len(first["source_set_fingerprint"]) == 64


@pytest.mark.parametrize(
    ("remote_result", "expected"),
    [
        (SimpleNamespace(participant=SimpleNamespace()), "member"),
        (errors.UserNotParticipantError(request=None), "absent"),
        (ConnectionError("disconnected"), "inconclusive"),
    ],
)
def test_remote_observation_keeps_three_way_outcome(remote_result, expected: str) -> None:
    source = _source("source-a", "closed_unknown", 11)
    client = _ObservationClient(remote_result)

    observed = asyncio.run(_observe_source(client, SimpleNamespace(id=7), source))

    assert observed.outcome == expected
    assert observed.target_account_id == 11
    assert len(observed.evidence_fingerprint) == 64


def test_creator_has_required_rescue_rights() -> None:
    participant = type("ChannelParticipantCreator", (), {})()

    assert _participant_rights(participant) == (True, True, True, True)


def test_admin_rights_are_read_without_assumption() -> None:
    participant = type("ChannelParticipantAdmin", (), {})()
    participant.admin_rights = SimpleNamespace(
        invite_users=True,
        ban_users=False,
        delete_messages=True,
    )

    assert _participant_rights(participant) == (True, True, False, True)


def test_flood_wait_checkpoint_is_explicit_and_non_mutating() -> None:
    args = SimpleNamespace(
        expected_source_count=125,
        expected_source_set_fingerprint="f" * 64,
    )
    error = AdmissionProbeFloodWait(processed_count=17, retry_after_seconds=43)

    checkpoint = _probe_checkpoint(args, error)

    assert checkpoint == {
        "mode": "probe_checkpoint",
        "state": "stopped_flood_wait",
        "source_count": 125,
        "source_set_fingerprint": "f" * 64,
        "processed_count": 17,
        "retry_after_seconds": 43,
        "database_write_performed": False,
    }


class _ObservationClient:
    def __init__(self, remote_result) -> None:
        self.remote_result = remote_result

    async def get_entity(self, _reference: str):
        return SimpleNamespace(id=11)

    async def __call__(self, _request):
        if isinstance(self.remote_result, Exception):
            raise self.remote_result
        return self.remote_result


def _source(action_id: str, status: str, target_account_id: int) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="invite_group_account",
        account_id=101,
        status=status,
        task_lifecycle_epoch=7,
        payload={
            "group_id": 11,
            "operation_target_id": 21,
            "group_peer_id": "-10011",
            "target_account_id": target_account_id,
            "target_account_ref": f"@target_{target_account_id}",
        },
    )
