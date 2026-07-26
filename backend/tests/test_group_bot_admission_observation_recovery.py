from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Action, GroupAuthStatus, GroupBotAdmission, GroupBotAdmissionObservation, GroupContextMessage, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import now as model_now
from app.services.task_center import dispatcher
from app.services.task_center.group_bot_admission import (
    READY_STATE,
    close_observation_if_due,
    create_policy,
    ensure_admission_after_join,
    evaluate_send_gate,
)
from app.services.task_center.group_bot_observation import (
    record_listener_observations,
    restart_admission_observation,
)
from app.services.task_center.payloads import EnsureChannelMembershipPayload

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _snapshot(remote_message_id: str) -> SimpleNamespace:
    return SimpleNamespace(remote_message_id=remote_message_id)


def _group(session: Session) -> TgGroup:
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
    session.add(group)
    session.flush()
    return group


def test_missing_join_cursor_is_visible_observation_stale() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
        )

        assert admission.state == "observation_stale"
        assert admission.failure_code == "join_start_cursor_missing"


def test_listener_observation_closes_to_policy_unresolved_only_with_valid_window() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        admission.observation_closes_at = model_now() - timedelta(seconds=1)

        recorded = record_listener_observations(
            session,
            group=group,
            listener_account_id=21,
            snapshots=[_snapshot("100"), _snapshot("101")],
        )
        observation = session.scalar(select(GroupBotAdmissionObservation))
        close_observation_if_due(session, admission=admission)

        assert recorded == 1
        assert observation is not None
        assert observation.cursor_gap is False
        assert observation.observed_end_cursor == "101"
        assert admission.state == "group_bot_policy_unresolved"


def test_listener_observation_with_explicit_policy_reaches_ready() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        admission.observation_closes_at = model_now() - timedelta(seconds=1)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="not_required",
            reason="listener window has no trusted bot rule",
            evidence_ref="observation:100-101",
            created_by="operator",
        )

        record_listener_observations(
            session,
            group=group,
            listener_account_id=21,
            snapshots=[_snapshot("100"), _snapshot("101")],
        )
        close_observation_if_due(session, admission=admission)

        assert admission.state == READY_STATE


def test_truncated_listener_window_is_stale_and_never_closes() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        admission.observation_closes_at = model_now() - timedelta(seconds=1)

        record_listener_observations(
            session,
            group=group,
            listener_account_id=21,
            snapshots=[_snapshot("101"), _snapshot("102")],
        )
        close_observation_if_due(session, admission=admission)

        assert admission.state == "observation_stale"
        assert admission.failure_code == "cursor_gap"


def test_collect_group_context_writes_observation_after_listener_poll(monkeypatch) -> None:
    from app.services import group_listeners

    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        listener = TgAccount(
            id=21,
            tenant_id=1,
            display_name="listener",
            phone_masked="+100",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="listener-session",
        )
        session.add_all([listener, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=listener.id, is_listener=True)])
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        admission.observation_closes_at = model_now() - timedelta(seconds=1)
        monkeypatch.setattr(group_listeners, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(group_listeners, "_listener_context_account_error", lambda _account: "")
        monkeypatch.setattr(group_listeners, "insert_context_snapshots", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(
            group_listeners.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_snapshot("100"), _snapshot("101")],
        )

        assert group_listeners.collect_group_context(session, group) == 0
        assert session.scalar(select(GroupBotAdmissionObservation)) is not None
        assert admission.state == "group_bot_policy_unresolved"


def test_listener_fetch_failure_persists_stale_observation_after_worker_rollback(monkeypatch) -> None:
    from app.services import group_listeners

    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        group.listener_enabled = True
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
        listener = TgAccount(
            id=21,
            tenant_id=1,
            display_name="listener",
            phone_masked="+100",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="listener-session",
        )
        session.add_all([listener, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=listener.id, is_listener=True)])
        ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        session.commit()
        monkeypatch.setattr(group_listeners, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            group_listeners.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("listener upstream unavailable")),
        )

        assert group_listeners.process_group_listener(session, group.id) == 0
        admission = session.scalar(select(GroupBotAdmission))
        observation = session.scalar(select(GroupBotAdmissionObservation))
        refreshed_group = session.get(TgGroup, group.id)

        assert admission is not None
        assert admission.state == "observation_stale"
        assert observation is not None
        assert observation.failure_code == "listener_fetch_failed"
        assert refreshed_group is not None
        assert refreshed_group.listener_last_error == "listener upstream unavailable"


def test_restart_observation_uses_persisted_listener_waterline() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = _group(session)
        listener = TgAccount(id=21, tenant_id=1, display_name="listener", phone_masked="+100")
        session.add(listener)
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=group.id,
                listener_account_id=listener.id,
                remote_message_id="300",
                content="latest listener context",
            )
        )
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group.id,
            account_id=11,
            membership_action_id="join-1",
        )

        restart_admission_observation(
            session,
            admission=admission,
            expected_admission_version=1,
            reason="repair legacy missing baseline",
            evidence_ref="incident:2026-07-27",
        )

        assert admission.state == "awaiting_group_bot_rule"
        assert admission.join_start_cursor == "300"
        assert admission.admission_version == 2
        assert admission.failure_code == ""


def test_expired_legacy_wait_without_baseline_becomes_stale_at_send_gate() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = GroupBotAdmission(
            tenant_id=1,
            group_id=7,
            account_id=11,
            state="awaiting_group_bot_rule",
            observation_closes_at=model_now() - timedelta(seconds=1),
        )
        session.add(admission)
        session.flush()

        gate = evaluate_send_gate(session, tenant_id=1, group_id=7, account_id=11)

        assert gate.allowed is False
        assert admission.state == "observation_stale"
        assert admission.failure_code == "join_start_cursor_missing"


def test_membership_join_captures_listener_cursor_before_gateway(monkeypatch) -> None:
    with _session() as session:
        session.add_all(
            [
                Tenant(id=1, name="t"),
                Task(id="task-1", tenant_id=1, name="ai", type="group_ai_chat", status="running"),
                OperationTarget(id=8, tenant_id=1, target_type="group", tg_peer_id="-1007", title="g"),
                TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup"),
                TgAccount(id=11, tenant_id=1, display_name="joining", phone_masked="+101", status=AccountStatus.ACTIVE.value, session_ciphertext="joining-session"),
                TgAccount(id=21, tenant_id=1, display_name="listener", phone_masked="+121", status=AccountStatus.ACTIVE.value, session_ciphertext="listener-session"),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=21, is_listener=True),
            ]
        )
        action = Action(
            id="join-action",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="ensure_target_membership",
            account_id=11,
            status="executing",
            payload={},
        )
        session.add(action)
        session.flush()
        payload = EnsureChannelMembershipPayload(
            channel_id="-1007",
            channel_target_id=8,
            target_type="group",
            target_display="g",
            require_send=True,
        )
        context = dispatcher.MembershipDispatchContext(session, action, session.get(TgAccount, 11), object(), payload, None)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "fetch_group_messages",
            lambda *_args, **_kwargs: [_snapshot("400"), _snapshot("401")],
        )

        assert dispatcher._capture_group_bot_join_baseline(context) is True
        assert action.result["join_start_cursor"] == "401"
