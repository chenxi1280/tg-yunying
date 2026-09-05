from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import (
    ChannelDiscussionIdentitySnapshot,
    ChannelMessageDeletionObservation,
    ChannelReactionCapabilitySnapshot,
)
from app.integrations.telegram.telethon_content import fetch_channel_message_deletions
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ChannelMessageSourceRevision,
    FulfillmentRemoteFact,
    ListenerChannelSnapshotItem,
    ListenerSourceState,
    OperationTarget,
    Task,
    TaskPlannerWakeState,
    TaskSourceSubscription,
    Tenant,
    TgAccount,
)
from app.services.channel_target_reference import channel_read_reference
from app.services.task_center import channel_listener_runtime
from app.services.task_center import channel_listener_accounts
from app.services.task_center import channel_source_message_persistence
from app.services.task_center.channel_listener_runtime import (
    channel_snapshot_state,
    drain_channel_listener_runtime,
    request_channel_snapshot_refresh,
)
from app.services.task_center.executors import common as executor_common


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 12, 0)


def test_missing_history_requires_exact_delete_evidence(monkeypatch) -> None:
    source = channel_listener_runtime.ChannelListenerSource(1, 31, "hash", 101, 30, 20)
    calls = []
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_message_deletions",
        lambda *args, **_kwargs: calls.append(args) or [
            ChannelMessageDeletionObservation(message_id=9001, deleted=False),
        ],
    )

    observations = channel_listener_runtime._probe_missing_messages(
        source, snapshots=[], tracked_message_ids=[9001],
        channel_peer="-10031", session_ciphertext="session", credentials=object(),
    )

    assert calls and calls[0][2] == [9001]
    assert observations == [
        ChannelMessageDeletionObservation(message_id=9001, deleted=False),
    ]


def test_exact_lookup_only_marks_none_and_message_empty_deleted() -> None:
    class MessageEmpty:
        pass

    class Client:
        async def get_entity(self, target):
            return target

        async def get_messages(self, entity, ids):
            return [SimpleNamespace(id=ids[0]), MessageEmpty(), None]

    observations = asyncio.run(fetch_channel_message_deletions(
        Client(), "-10031", [9001, 9002, 9003],
    ))

    assert [row.deleted for row in observations] == [False, True, True]


def test_listener_owns_channel_fetch_and_publishes_fresh_snapshot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="channel-like-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
        username="channel_test",
    )

    calls: list[tuple] = []
    _stub_listener_fetch(monkeypatch, calls)

    result = drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    assert result.source_count == 1
    assert result.processed_count == 1
    assert len(calls) == 1
    assert calls[0][0][1] == "@channel_test"
    with Session(engine) as session:
        task = session.get(Task, "channel-like-task")
        channel = session.get(OperationTarget, 31)
        message = session.scalar(select(ChannelMessage))
        state = session.scalar(select(ListenerSourceState))
        subscription = session.scalar(select(TaskSourceSubscription))
        wake = session.scalar(select(TaskPlannerWakeState))
        assert message is not None and message.message_id == 9001
        assert channel.reaction_capability_mode == "all"
        assert channel.available_reactions == ["👍", "❤️", "🔥", "👏"]
        assert state is not None and state.snapshot_status == "ready"
        assert state.snapshot_revision == 1
        assert state.fresh_until_at == NOW + timedelta(seconds=60)
        assert subscription is not None and subscription.listener_source_state_id == state.id
        assert wake is not None and wake.reason_code == "channel_source_snapshot_ready"
        assert wake.not_before_at == NOW
        assert channel_snapshot_state(session, task, channel, now_value=NOW) == (
            "ready",
            NOW + timedelta(seconds=30),
        )


def test_listener_prefers_prior_success_over_first_eligible_account() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="channel-listener-failover",
        task_type="channel_view",
        channel_id=31,
        account_id=101,
    )
    with Session(engine) as session:
        task = session.get(Task, "channel-listener-failover")
        task.account_config = {
            "selection_mode": "manual",
            "account_ids": [101, 102],
        }
        session.add(TgAccount(
            id=102,
            tenant_id=1,
            display_name="listener-backup",
            phone_masked="***listener-backup",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="backup-session",
        ))
        session.add_all([
            ListenerSourceState(
                tenant_id=1, source_type="channel", source_peer_id="31",
                account_id=101, snapshot_status="unavailable",
            ),
            ListenerSourceState(
                tenant_id=1, source_type="channel", source_peer_id="31",
                account_id=102, snapshot_status="ready",
            ),
        ])
        session.flush()

        source = channel_listener_runtime._source_for_task(
            session,
            task,
            session.get(OperationTarget, 31),
        )

        assert source is not None and source.account_id == 102


def test_listener_prefers_recent_same_target_remote_success(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(channel_listener_accounts, "_now", lambda: NOW)
    _seed_listener_task(
        engine,
        task_id="channel-listener-current-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
    )
    with Session(engine) as session:
        task = session.get(Task, "channel-listener-current-task")
        task.account_config = {
            "selection_mode": "manual",
            "account_ids": [101, 102],
        }
        _add_same_target_view_success(session)
        session.flush()

        source = channel_listener_runtime._source_for_task(
            session,
            task,
            session.get(OperationTarget, 31),
        )

        assert source is not None and source.account_id == 102


def test_listener_does_not_borrow_same_target_account_outside_task_scope(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(channel_listener_accounts, "_now", lambda: NOW)
    _seed_listener_task(
        engine,
        task_id="channel-listener-scoped-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
    )
    with Session(engine) as session:
        task = session.get(Task, "channel-listener-scoped-task")
        _add_same_target_view_success(session)
        session.flush()

        source = channel_listener_runtime._source_for_task(
            session,
            task,
            session.get(OperationTarget, 31),
        )

        assert source is not None and source.account_id == 101


def test_listener_rotates_oldest_due_failure_after_all_candidates_fail(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: NOW)
    _seed_listener_task(
        engine,
        task_id="channel-listener-failure-rotation",
        task_type="channel_view",
        channel_id=31,
        account_id=101,
    )
    with Session(engine) as session:
        task = session.get(Task, "channel-listener-failure-rotation")
        task.account_config = {
            "selection_mode": "manual",
            "account_ids": [101, 102, 103],
        }
        session.add_all([
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"listener-{account_id}",
                phone_masked=f"***{account_id}",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext=f"session-{account_id}",
            )
            for account_id in (102, 103)
        ])
        session.add_all([
            ListenerSourceState(
                tenant_id=1, source_type="channel", source_peer_id="31",
                account_id=101, snapshot_status="unavailable",
                next_probe_at=NOW + timedelta(minutes=1), updated_at=NOW - timedelta(minutes=3),
            ),
            ListenerSourceState(
                tenant_id=1, source_type="channel", source_peer_id="31",
                account_id=102, snapshot_status="unavailable",
                next_probe_at=NOW - timedelta(seconds=1), updated_at=NOW - timedelta(minutes=2),
            ),
            ListenerSourceState(
                tenant_id=1, source_type="channel", source_peer_id="31",
                account_id=103, snapshot_status="unavailable",
                next_probe_at=NOW - timedelta(seconds=1), updated_at=NOW - timedelta(minutes=1),
            ),
        ])
        session.flush()

        source = channel_listener_runtime._source_for_task(
            session,
            task,
            session.get(OperationTarget, 31),
        )

        assert source is not None and source.account_id == 102


def test_listener_keeps_disjoint_task_reader_accounts_isolated() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="isolated-like-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
    )
    with Session(engine) as session:
        session.add_all([
            TgAccount(
                id=102,
                tenant_id=1,
                display_name="isolated-view-reader",
                phone_masked="***isolated-view-reader",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="isolated-view-session",
            ),
            Task(
                id="isolated-view-task",
                tenant_id=1,
                name="isolated-view-task",
                type="channel_view",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [102]},
                type_config={"target_channel_id": 31, "message_scope": "dynamic_new"},
            ),
        ])
        session.flush()

        sources = channel_listener_runtime._channel_sources(
            session,
            tenant_id=1,
            limit=10,
        )

        assert [(source.account_id, source.task_ids) for source in sources] == [
            (101, ["isolated-like-task"]),
            (102, ["isolated-view-task"]),
        ]
        state_ids = set(session.scalars(select(TaskSourceSubscription.listener_source_state_id)))
        assert len(state_ids) == 2


def test_listener_persists_exact_full_source_text_and_edit_identity(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="channel-exact-source-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
        username="channel_exact",
    )
    exact = "  " + ("长正文" * 220) + "\n"
    edited_at = NOW + timedelta(minutes=1)
    snapshot = SimpleNamespace(
        message_id=9001,
        content_preview=exact[:500].strip(),
        content_text=exact,
        message_url="",
        published_at=NOW,
        edited_at=edited_at,
        source_type="caption",
        content_complete=True,
        comment_available=True,
    )
    calls: list[tuple] = []
    _stub_listener_fetch(monkeypatch, calls)
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *_args, **_kwargs: [snapshot],
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        source = session.scalar(select(ChannelMessageSourceRevision))
        assert source.source_text_snapshot == exact
        assert source.source_content_hash == hashlib.sha256(
            exact.encode("utf-8"),
        ).hexdigest()
        assert source.source_length == len(exact) == source.captured_length
        assert source.truncation_state == "complete"
        assert source.telegram_edit_date == edited_at
        assert source.source_type == "caption"


def test_fresh_empty_snapshot_hides_messages_from_previous_revision(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_dynamic_channel_task(engine)
    current_time = [NOW]
    snapshots = [[_snapshot(9001)], []]
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: current_time[0])
    monkeypatch.setattr(executor_common, "_now", lambda: current_time[0])
    monkeypatch.setattr(
        channel_listener_runtime,
        "task_account_runtime_transport",
        lambda _session, account, _task_type: SimpleNamespace(
            session_ciphertext=account.session_ciphertext,
            credentials=object(),
        ),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *_args, **_kwargs: snapshots.pop(0),
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)
    current_time[0] = NOW + timedelta(seconds=30)
    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        task = session.get(Task, "empty-snapshot-task")
        channel, messages = executor_common.channel_scope(
            session,
            task,
            task.type_config,
        )
        state = session.scalar(select(ListenerSourceState))
        item_count = session.scalar(select(func.count(ListenerChannelSnapshotItem.id)))
        assert state.snapshot_revision == 2
        assert session.scalar(select(func.count(ChannelMessage.id))) == 1
        assert item_count == 0
        assert channel is None
        assert messages == []


def test_listener_dispatches_source_edit_revision_operation(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_dynamic_channel_task(engine)
    current_time = [NOW]
    first = _snapshot(9001)
    edited = SimpleNamespace(**{**vars(first), "content_preview": "edited message"})
    snapshots = [[first], [edited]]
    operations = []
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: current_time[0])
    monkeypatch.setattr(
        channel_listener_runtime,
        "task_account_runtime_transport",
        lambda _session, account, _task_type: SimpleNamespace(
            session_ciphertext=account.session_ciphertext,
            credentials=object(),
        ),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway, "fetch_channel_messages",
        lambda *_args, **_kwargs: snapshots.pop(0),
    )
    monkeypatch.setattr(
        channel_source_message_persistence,
        "reconcile_channel_comment_source_edit",
        lambda _session, _message, source, **_kwargs: operations.append(source.id),
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)
    current_time[0] += timedelta(seconds=30)
    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        message = session.scalar(select(ChannelMessage))
        assert operations == [message.current_source_revision_id]


def test_discussion_identity_creates_successor_without_mutating_source(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="discussion-successor-task",
        task_type="channel_comment",
        channel_id=31,
        account_id=101,
    )
    current_time = [NOW]
    discussion_results = [
        RuntimeError("discussion unavailable"),
        ChannelDiscussionIdentitySnapshot(
            channel_peer_id="-10031",
            discussion_peer_id="-10032",
            thread_root_by_source_message_id={9001: 8101},
            discussion_title="discussion",
        ),
    ]
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: current_time[0])
    monkeypatch.setattr(
        channel_listener_runtime,
        "task_account_runtime_transport",
        lambda _session, account, _task_type: SimpleNamespace(
            session_ciphertext=account.session_ciphertext,
            credentials=object(),
        ),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway, "fetch_channel_messages",
        lambda *_args, **_kwargs: [_snapshot(9001)],
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway, "fetch_channel_discussion_identity",
        lambda *_args, **_kwargs: _next_discussion_result(discussion_results),
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)
    current_time[0] += timedelta(seconds=30)
    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        message = session.scalar(select(ChannelMessage))
        revisions = list(session.scalars(
            select(ChannelMessageSourceRevision).order_by(
                ChannelMessageSourceRevision.source_revision,
            )
        ))
        assert len(revisions) == 2
        assert message.current_source_revision_id == revisions[1].id
        assert revisions[0].discussion_group_binding_id is None
        assert revisions[0].discussion_thread_binding_id is None
        assert revisions[1].discussion_group_binding_id is not None
        assert revisions[1].discussion_thread_binding_id is not None
        assert revisions[1].source_operation == "observed"


def test_reaction_probe_failure_keeps_shared_message_snapshot_ready(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="shared-like-task",
        task_type="channel_like",
        channel_id=45,
        account_id=205,
    )
    _add_shared_view_task(engine)
    _stub_listener_fetch(monkeypatch, [])
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_reaction_capability",
        _raise_reaction_probe_error,
    )

    result = drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        state = session.scalar(select(ListenerSourceState))
        like_task = session.get(Task, "shared-like-task")
        assert result.processed_count == 1 and result.error_count == 0
        assert state.snapshot_status == "ready"
        assert session.scalar(select(func.count(ChannelMessage.id))) == 1
        assert like_task.stats["reaction_capability_probe"]["error_code"] == "RuntimeError"


def test_listener_marks_subscription_unavailable_without_collect_account() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="missing-account-task",
        task_type="channel_like",
        channel_id=51,
        account_id=301,
    )
    with Session(engine) as session:
        session.get(TgAccount, 301).status = AccountStatus.NEED_RELOGIN.value
        session.commit()

    result = drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    assert result.source_count == 0
    with Session(engine) as session:
        task = session.get(Task, "missing-account-task")
        channel = session.get(OperationTarget, 51)
        subscription = session.scalar(select(TaskSourceSubscription))
        assert subscription is not None and subscription.state == "unavailable"
        assert channel_snapshot_state(session, task, channel, now_value=NOW) == (
            "unavailable",
            None,
        )
        assert task.last_error == "channel_source_snapshot_unavailable"


def test_channel_read_reference_uses_numeric_peer_without_username() -> None:
    channel = SimpleNamespace(username="", tg_peer_id="-10051")

    assert channel_read_reference(channel) == "-10051"


def test_channel_read_reference_keeps_numeric_peer_for_private_invite() -> None:
    channel = SimpleNamespace(username="https://t.me/+private", tg_peer_id="-10052")

    assert channel_read_reference(channel) == "-10052"


def test_reset_refresh_requires_and_schedules_next_snapshot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_dynamic_channel_task(engine)
    _stub_listener_fetch(monkeypatch, [])
    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        task = session.get(Task, "empty-snapshot-task")
        channel = session.get(OperationTarget, 41)
        request_channel_snapshot_refresh(session, task)
        session.commit()
        subscription = session.scalar(select(TaskSourceSubscription))
        state = session.scalar(select(ListenerSourceState))
        assert subscription.required_snapshot_revision == 2
        assert subscription.state == "pending"
        assert state.next_probe_at == NOW
        assert channel_snapshot_state(session, task, channel, now_value=NOW) == (
            "pending",
            NOW,
        )


def _seed_dynamic_channel_task(engine) -> None:
    _seed_listener_task(
        engine,
        task_id="empty-snapshot-task",
        task_type="channel_view",
        channel_id=41,
        account_id=201,
    )


def _add_shared_view_task(engine) -> None:
    with Session(engine) as session:
        session.add(Task(
            id="shared-view-task",
            tenant_id=1,
            name="shared-view-task",
            type="channel_view",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [205]},
            type_config={
                "target_channel_id": 45,
                "message_scope": "dynamic_new",
                "listener_interval_seconds": 30,
            },
        ))
        session.commit()


def _seed_listener_task(
    engine,
    *,
    task_id: str,
    task_type: str,
    channel_id: int,
    account_id: int,
    username: str = "",
) -> None:
    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="tenant"),
            OperationTarget(
                id=channel_id,
                tenant_id=1,
                target_type="channel",
                tg_peer_id=f"-100{channel_id}",
                title="channel",
                username=username,
            ),
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name="listener",
                phone_masked="***listener",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="encrypted-session",
            ),
            Task(
                id=task_id,
                tenant_id=1,
                name=task_id,
                type=task_type,
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [account_id]},
                type_config={
                    "target_channel_id": channel_id,
                    "message_scope": "dynamic_new",
                    "listener_interval_seconds": 30,
                    "message_active_days": 7,
                },
            ),
        ])
        session.commit()


def _snapshot(message_id: int):
    return SimpleNamespace(
        message_id=message_id,
        content_preview="new message",
        message_url="",
        comment_available=True,
        published_at=NOW,
    )


def _remote_view_fact(action: Action) -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        fact_id="same-target-view-fact",
        tenant_id=action.tenant_id,
        task_type=action.task_type,
        task_id=action.task_id,
        obligation_type="view",
        obligation_id="same-target-view-obligation",
        action_id=action.id,
        attempt_id="same-target-view-attempt",
        mutation_kind="view_message",
        remote_mutation_key_hash="same-target-view-mutation",
        gateway_request_hash="same-target-view-request",
        fact_kind="view_observed",
        fact_identity_hash="same-target-view-identity",
        observed_at=NOW,
    )


def _add_same_target_view_success(session: Session) -> None:
    session.add_all([
        TgAccount(
            id=102,
            tenant_id=1,
            display_name="recent-target-reader",
            phone_masked="***recent-target-reader",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="recent-target-session",
        ),
        Task(
            id="same-target-view-task",
            tenant_id=1,
            name="same-target-view-task",
            type="channel_view",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [102]},
            type_config={"target_channel_id": 31},
        ),
    ])
    action = Action(
        id="same-target-view-action",
        tenant_id=1,
        task_id="same-target-view-task",
        task_type="channel_view",
        action_type="view_message",
        account_id=102,
        status="success",
        scheduled_at=NOW,
        executed_at=NOW,
        payload={"channel_target_id": 31},
    )
    session.add_all([action, _remote_view_fact(action)])


def _raise_reaction_probe_error(*_args, **_kwargs):
    raise RuntimeError("probe failed")


def _next_discussion_result(results: list):
    result = results.pop(0)
    if isinstance(result, Exception):
        raise result
    return result


def _stub_listener_fetch(monkeypatch, calls: list[tuple]) -> None:
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: NOW)
    monkeypatch.setattr(
        channel_listener_runtime,
        "task_account_runtime_transport",
        lambda _session, account, _task_type: SimpleNamespace(
            session_ciphertext=account.session_ciphertext,
            credentials=object(),
        ),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [_snapshot(9001)],
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_reaction_capability",
        lambda *_args, **_kwargs: ChannelReactionCapabilitySnapshot(
            mode="all",
            available_reactions=("👍", "❤️", "🔥", "👏"),
        ),
    )


def test_channel_view_uses_snapshot_revision_messages_when_stale(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="view-stale-task",
        task_type="channel_view",
        channel_id=31,
        account_id=401,
    )
    current_time = [NOW]
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: current_time[0])
    monkeypatch.setattr(executor_common, "_now", lambda: current_time[0])
    monkeypatch.setattr(
        channel_listener_runtime,
        "task_account_runtime_transport",
        lambda _session, account, _task_type: SimpleNamespace(
            session_ciphertext=account.session_ciphertext,
            credentials=object(),
        ),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *_args, **_kwargs: [_snapshot(9001)],
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    # Fast-forward time past fresh_until_at (60s) so snapshot becomes stale
    current_time[0] = NOW + timedelta(seconds=120)

    with Session(engine) as session:
        session.query(ListenerChannelSnapshotItem).delete()
        session.flush()
        task = session.get(Task, "view-stale-task")
        channel, messages = executor_common.channel_scope(
            session,
            task,
            task.type_config,
        )
        assert channel is not None
        assert len(messages) == 1
        assert messages[0].message_id == 9001
        assert task.last_error == "channel_source_snapshot_stale"

        messages[0].published_at = NOW - timedelta(days=8)
        session.flush()
        channel, messages = executor_common.channel_scope(
            session, task, task.type_config,
        )
        assert channel is None
        assert messages == []
