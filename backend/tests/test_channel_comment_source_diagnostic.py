import hashlib
from datetime import timedelta, timezone
from app.timezone import as_beijing_aware

import pytest

from app.integrations.telegram.contracts import ChannelMessageSnapshot
from app.models import ChannelMessage, ChannelMessageSourceRevision, ListenerSourceState, TaskSourceSubscription
from app.services.task_center.channel_comment_source_diagnostic import (
    LatestSourceDiagnosticDependencies,
    LatestSourceDiagnosticRequest,
    diagnose_latest_channel_source,
)
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    planner_session,
    seed_comment_task,
)
from test_channel_comment_plan_contract import _enable_grounding_plan


pytestmark = pytest.mark.no_postgres


def _listener_state(session, task) -> ListenerSourceState:
    state = ListenerSourceState(
        tenant_id=task.tenant_id, source_type="channel", source_peer_id="31",
        account_id=101, shard_key="channel:31", snapshot_status="ready",
        snapshot_revision=1, observed_at=STABLE_PLANNER_NOW,
        fresh_until_at=STABLE_PLANNER_NOW + timedelta(hours=1),
    )
    session.add(state)
    session.flush()
    session.add(TaskSourceSubscription(
        tenant_id=task.tenant_id, task_id=task.id,
        lifecycle_epoch=task.task_lifecycle_epoch, source_type="channel",
        source_peer_hash="source-hash", listener_source_state_id=state.id,
        state="ready", required_snapshot_revision=1,
    ))
    session.commit()
    return state


def _dependencies(fetcher) -> LatestSourceDiagnosticDependencies:
    return LatestSourceDiagnosticDependencies(
        fetch_messages=fetcher,
        credentials_for_account=lambda *_args: object(),
        observed_at=STABLE_PLANNER_NOW,
    )


@pytest.mark.parametrize("remote_utc", [False, True])
def test_latest_source_diagnostic_reports_in_sync_without_writes(remote_utc) -> None:
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        _listener_state(session, task)
        message = session.get(ChannelMessage, 41)
        source = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        exact_text = f"  {message.content_preview}{'很长的正文' * 150}  "
        source.source_text_snapshot = exact_text
        source.source_content_hash = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
        source.telegram_edit_date = STABLE_PLANNER_NOW
        session.commit()
        before_dirty = set(session.dirty)

        result = diagnose_latest_channel_source(
            session, LatestSourceDiagnosticRequest(tenant_id=1, task_id=task.id),
            _dependencies(lambda *_args, **_kwargs: [ChannelMessageSnapshot(
                message_id=message.message_id,
                content_preview=message.content_preview,
                content_text=exact_text,
                published_at=(as_beijing_aware(message.published_at).astimezone(timezone.utc)
                              if remote_utc else message.published_at),
                edited_at=(as_beijing_aware(STABLE_PLANNER_NOW).astimezone(timezone.utc)
                           if remote_utc else STABLE_PLANNER_NOW),
            )]),
        )

        assert result["state"] == "in_sync"
        assert result["remote"] == result["local"]
        assert set(session.dirty) == before_dirty


def test_latest_source_diagnostic_refuses_active_listener_lease() -> None:
    calls: list[str] = []
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        _enable_grounding_plan(session, task)
        state = _listener_state(session, task)
        state.lease_owner = "canonical-listener"
        state.lease_expires_at = STABLE_PLANNER_NOW + timedelta(minutes=1)
        session.commit()

        result = diagnose_latest_channel_source(
            session, LatestSourceDiagnosticRequest(tenant_id=1, task_id=task.id),
            _dependencies(lambda *_args, **_kwargs: calls.append("telegram")),
        )

        assert result["state"] == "listener_session_in_use"
        assert calls == []


def test_listener_lease_compares_equivalent_instants_with_utc_offset():
    from types import SimpleNamespace
    from app.services.task_center.channel_comment_source_diagnostic import _listener_lease_active
    deadline = as_beijing_aware(STABLE_PLANNER_NOW + timedelta(minutes=1)).astimezone(timezone.utc)
    state = SimpleNamespace(lease_owner='listener', lease_expires_at=deadline)
    assert _listener_lease_active(state, STABLE_PLANNER_NOW)
