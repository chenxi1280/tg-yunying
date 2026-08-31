from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Action, ExecutionAttempt, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneSequencerHeadCase,
    CloneSourceEvent,
    CloneSourceStreamState,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now
from app.services.task_center.group_clone_binding import CloneSenderBindingManager

from test_group_clone_api import (
    _auth_headers,
    _cutover_clone_payload,
    client_and_session,
)

pytestmark = pytest.mark.no_postgres


def test_clone_config_patch_rejects_missing_rule_version(client_and_session):
    client, session = client_and_session
    created = client.post(
        "/api/tasks/group-clone", json=_cutover_clone_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, created.json()["task_id"])
    payload = _cutover_clone_payload()
    patch_payload = {key: payload[key] for key in (
        "sender_pool", "pacing", "content", "lifecycle",
    )}
    patch_payload["retention"] = {
        "source_event_days": 30, "media_cache_ttl_seconds": 86400,
    }
    patch_payload["content"] = {**patch_payload["content"], "rule_set_version": 999}

    response = client.patch(
        f"/api/tasks/{task.id}/group-clone", json=patch_payload, headers=_auth_headers(),
    )

    assert response.status_code == 400
    session.refresh(task)
    assert task.config_revision == 1
    assert task.type_config["content"]["rule_set_version"] == 1


def test_clone_operational_reads_and_decisions_use_current_epoch(client_and_session):
    client, session = client_and_session
    task = _seed_epoch_records(session)

    pages = _read_current_epoch_pages(client, task.id)
    old_case = _old_case_decision(client, task.id)
    old_review = _old_review_decision(client, task.id)

    assert [item["id"] for item in pages[0]["items"]] == ["event-current"]
    assert [item["id"] for item in pages[1]["items"]] == ["obligation-current"]
    assert [item["id"] for item in pages[2]["items"]] == ["case-current"]
    assert [item["review_id"] for item in pages[3]["items"]] == ["obligation-current"]
    assert old_case.status_code == 404
    assert old_review.status_code == 404


def test_manual_review_history_and_unsettled_action_chain_remain_visible(
    client_and_session,
):
    client, session = client_and_session
    task = _seed_epoch_records(session)
    action = Action(
        id="action-current", tenant_id=1, task_id=task.id,
        task_type="group_clone", action_type="group_clone_send",
        status="failed", obligation_id="obligation-current",
        task_lifecycle_epoch=2,
    )
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(
        id="attempt-current", tenant_id=1, action_id=action.id,
        task_lifecycle_epoch=2, status="failed",
    )
    session.add(attempt)
    session.commit()

    decision = _current_review_decision(client, task.id)
    history = client.get(
        f"/api/tasks/{task.id}/clone-manual-reviews?include_resolved=true",
        headers=_auth_headers(),
    ).json()["items"]
    obligation = client.get(
        f"/api/tasks/{task.id}/clone-obligations",
        headers=_auth_headers(),
    ).json()["items"][0]

    assert decision.status_code == 200
    assert history[0]["last_decision"] == "drop"
    assert history[0]["decision_reason"] == "review complete"
    assert obligation["action_id"] == action.id
    assert obligation["attempt_id"] == attempt.id


def test_sender_rebind_api_is_idempotent(client_and_session):
    client, session = client_and_session
    created = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_cutover_clone_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, created.json()["task_id"])
    binding, error = CloneSenderBindingManager.get_or_assign_sender_binding(
        session, task, source_sender_peer_type="user",
        source_sender_peer_id="sender-a", source_sender_name="Sender A",
    )
    assert not error
    replacement = next(
        item for item in task.type_config["sender_pool"]["account_ids"]
        if item != binding.assigned_account_id
    )
    payload = {
        "expected_binding_version": binding.binding_version,
        "replacement_account_id": replacement,
        "reason": "operator rebind",
        "client_request_id": "binding-change-request-1",
    }

    first = client.post(
        f"/api/tasks/{task.id}/clone-bindings/{binding.id}/change",
        json=payload, headers=_auth_headers(),
    )
    replay = client.post(
        f"/api/tasks/{task.id}/clone-bindings/{binding.id}/change",
        json=payload, headers=_auth_headers(),
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_runtime_health_requires_delivery_obligations_to_close(client_and_session):
    client, session = client_and_session
    created = client.post(
        "/api/tasks/group-clone/create-and-start",
        json=_cutover_clone_payload(), headers=_auth_headers(),
    )
    task = session.get(Task, created.json()["task_id"])
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
    ))
    state = session.get(
        TelegramAuthorizationUpdateState, stream.authorization_update_state_id,
    )
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
    ))
    task.status = "running"
    stream.state = "live"
    state.state = "live"
    state.owner_id = "collector-test"
    state.lease_expires_at = _now() + timedelta(minutes=5)
    subscription.state = "active"
    event = _event(task.id, task.task_lifecycle_epoch, "health")
    obligation = _obligation(
        task.id, event.id, task.task_lifecycle_epoch, "health",
    )
    obligation.state = "action_bound"
    session.add_all([event, obligation])
    session.commit()

    blocked = client.get(
        f"/api/tasks/{task.id}/clone-runtime-summary", headers=_auth_headers(),
    ).json()
    obligation.state = "succeeded"
    session.commit()
    healthy = client.get(
        f"/api/tasks/{task.id}/clone-runtime-summary", headers=_auth_headers(),
    ).json()

    assert blocked["business_health"] == "blocked"
    assert blocked["blocked_count"] == 1
    assert healthy["business_health"] == "healthy", healthy


def _current_review_decision(client, task_id: str):
    return client.post(
        f"/api/tasks/{task_id}/clone-manual-reviews/obligation-current/decision",
        json={
            "expected_review_revision": 1, "decision": "drop",
            "reason": "review complete",
            "client_request_id": "current-review-decision-1",
        },
        headers=_auth_headers(),
    )


def _seed_epoch_records(session) -> Task:
    task = Task(
        id="task-current-epoch-only", tenant_id=1, name="Epoch scoped clone",
        type="group_clone", status="running", task_lifecycle_epoch=2,
    )
    session.add(task)
    for epoch, suffix in ((1, "old"), (2, "current")):
        event = _event(task.id, epoch, suffix)
        obligation = _obligation(task.id, event.id, epoch, suffix)
        session.add_all([event, obligation, _case(task.id, obligation.id, epoch, suffix)])
    session.commit()
    return task


def _event(task_id: str, epoch: int, suffix: str) -> CloneSourceEvent:
    return CloneSourceEvent(
        id=f"event-{suffix}", tenant_id=1, task_id=task_id,
        task_lifecycle_epoch=epoch, source_peer_type="channel",
        source_peer_id="-100111", source_message_id=epoch,
        event_type="message_new", event_identity_hash=f"event-hash-{suffix}",
        apply_order_key=f"order-{suffix}", stream_order_no=1, content=suffix,
        content_fingerprint=f"content-{suffix}", config_snapshot={"epoch": epoch},
    )


def _obligation(task_id: str, event_id: str, epoch: int, suffix: str):
    return CloneDeliveryObligation(
        id=f"obligation-{suffix}", tenant_id=1, task_id=task_id, epoch=epoch,
        source_event_id=event_id, obligation_kind="send", stream_order_no=1,
        sequencer_id=1, planned_at=datetime.now(timezone.utc),
        state="waiting_manual_review", error_code="protected_content",
    )


def _case(task_id: str, obligation_id: str, epoch: int, suffix: str):
    return CloneSequencerHeadCase(
        id=f"case-{suffix}", task_id=task_id, epoch=epoch, sequencer_id=1,
        obligation_id=obligation_id, case_kind="failed_terminal",
        policy_snapshot="fail_stop", state="waiting_decision",
    )


def _read_current_epoch_pages(client, task_id: str) -> list[dict]:
    paths = (
        "clone-source-events", "clone-obligations",
        "clone-sequencer-head-cases", "clone-manual-reviews",
    )
    return [
        client.get(f"/api/tasks/{task_id}/{path}", headers=_auth_headers()).json()
        for path in paths
    ]


def _old_case_decision(client, task_id: str):
    return client.post(
        f"/api/tasks/{task_id}/clone-sequencer-head-cases/case-old/decision",
        json={
            "expected_case_revision": 1, "decision": "accept_visible_gap",
            "reason": "must not mutate old epoch",
            "client_request_id": "old-epoch-case-decision",
        },
        headers=_auth_headers(),
    )


def _old_review_decision(client, task_id: str):
    return client.post(
        f"/api/tasks/{task_id}/clone-manual-reviews/obligation-old/decision",
        json={
            "expected_review_revision": 1, "decision": "drop",
            "reason": "must not mutate old epoch",
            "client_request_id": "old-epoch-review-decision",
        },
        headers=_auth_headers(),
    )
