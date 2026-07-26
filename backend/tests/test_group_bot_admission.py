from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant
from app.models.enums import now as model_now
from app.services.task_center.group_bot_admission import (
    READY_STATE,
    apply_confirmation_event,
    attribute_prompt_to_account,
    close_observation_if_due,
    create_policy,
    ensure_admission_after_join,
    evaluate_send_gate,
    ingest_trusted_bot_prompt,
    is_group_bot_control_prompt,
    mark_channel_follow_completed,
    parse_channel_refs,
    record_probe_observation,
    record_observation_batch,
    reconcile_unresolved_with_not_required,
)

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_parse_channel_refs_accepts_only_exact_tme_links():
    refs = parse_channel_refs("请先关注 @school_news 和 https://t.me/school_notice 后发言 @helperbot")
    assert refs == ["school_notice"]


def test_confirmation_callback_is_a_control_prompt_without_channel_url():
    assert is_group_bot_control_prompt("", [{"text": "我已加入", "action_type": "callback"}]) is True


def test_attribute_prompt_unique_waiting_and_unattributed():
    account_id, reason = attribute_prompt_to_account(
        text="新人请先关注频道",
        waiting_account_ids=[11],
        account_usernames={},
        account_display_names={},
    )
    assert account_id == 11
    assert reason == "unique_waiting"
    account_id, reason = attribute_prompt_to_account(
        text="新人请先关注频道",
        waiting_account_ids=[11, 12],
        account_usernames={},
        account_display_names={},
    )
    assert account_id is None
    assert reason == "unattributed"


def test_joined_account_waits_until_observation_and_policy():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
            observation_window_seconds=120,
        )
        assert admission.state == "awaiting_group_bot_rule"
        gate = evaluate_send_gate(session, tenant_id=1, group_id=7, account_id=11, enforce=True)
        assert gate.allowed is False
        assert gate.code == "group_bot_admission_wait"

        admission.observation_closes_at = model_now() - timedelta(seconds=1)
        record_observation_batch(
            session,
            admission=admission,
            observed_end_cursor="101",
            listener_account_id=12,
            read_count=2,
        )
        close_observation_if_due(session, admission=admission)
        assert admission.state == "group_bot_policy_unresolved"

        create_policy(
            session,
            tenant_id=1,
            group_id=7,
            completion_policy="not_required",
            reason="no bot observed",
            evidence_ref="obs:1",
            created_by="operator",
        )
        assert reconcile_unresolved_with_not_required(session, tenant_id=1, group_id=7) == 1
        gate = evaluate_send_gate(session, tenant_id=1, group_id=7, account_id=11, enforce=True)
        assert gate.allowed is True
        assert gate.state == READY_STATE


def test_probe_ok_cannot_promote_ready():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = ensure_admission_after_join(
            session, tenant_id=1, group_id=7, account_id=11, membership_action_id="join-1"
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请先关注 https://t.me/school_news 后发言",
            bot_peer_id="900",
            is_admin_bot=True,
        )
        mark_channel_follow_completed(session, admission=admission, channel_ref="school_news")
        assert admission.state == "awaiting_group_bot_confirmation"
        record_probe_observation(admission, {"ok": True, "can_send": True})
        assert admission.state == "awaiting_group_bot_confirmation"
        apply_confirmation_event(
            session,
            admission=admission,
            message_id="bot-2",
            text="验证通过，可以发言",
            bot_peer_id="900",
        )
        assert admission.state == READY_STATE


def test_follow_sufficient_policy_ready_after_channels():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        admission = ensure_admission_after_join(
            session, tenant_id=1, group_id=7, account_id=11, membership_action_id="join-1"
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 https://t.me/school_news",
            bot_peer_id="900",
            is_admin_bot=True,
        )
        create_policy(
            session,
            tenant_id=1,
            group_id=7,
            completion_policy="follow_sufficient",
            trusted_bot_peer_id="900",
            reason="bot never confirms",
            evidence_ref="msg:bot-1",
            created_by="operator",
        )
        mark_channel_follow_completed(session, admission=admission, channel_ref="school_news")
        assert admission.state == READY_STATE
