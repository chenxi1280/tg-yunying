from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.models import ChannelSourceDecision, ChannelTaskIntake
from app.services.task_center.channel_source_intake import unified_source_intake
from app.services.task_center.channel_source_policy import source_filter_reason, source_opportunity_state
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def test_initial_five_frozen_new_posts_accumulate_without_latest_n_truncation():
    session, task, _, _ = seed_source_session()
    with session:
        old = [message(session, i, at=NOW-timedelta(minutes=20-i)) for i in range(1, 11)]
        config = {**task.type_config, "message_scope": "dynamic_new", "message_count": 1}
        first = unified_source_intake(session, task, old[-1:], config=config, observation_complete=True)
        assert {m.id for m in first} == set(range(6, 11))
        session.commit()
        new = [message(session, i, at=NOW+timedelta(minutes=i)) for i in range(11, 23)]
        second = unified_source_intake(session, task, new[-1:], config=config, observation_complete=True)
        assert {m.id for m in second} == set(range(6, 23))
        assert len(list(session.scalars(select(ChannelTaskIntake)))) == 1
        assert len(list(session.scalars(select(ChannelSourceDecision).where(
            ChannelSourceDecision.decision == "source_archived_skipped")))) == 5


def test_album_counts_once_against_historical_limit_and_filtered_sources_excluded():
    session, task, _, _ = seed_source_session()
    with session:
        rows = [message(session, i, album="album", metadata={"photo": True}) for i in range(1, 10)]
        message(session, 10, at=NOW+timedelta(seconds=1), metadata={"poll": True})
        config = {**task.type_config, "initial_historical_post_limit": 1}
        result = unified_source_intake(session, task, rows, config=config, observation_complete=True)
        assert len(result) == 9
        assert task.stats["source_intake"]["counts"]["source_filtered_non_content"] == 1


@pytest.mark.parametrize("metadata,text,reason", [
    ({"service_action": True}, "", "service_action"),
    ({"poll": True}, "hello", "poll_or_quiz"),
    ({"forwarded": True, "forward_peer_id": "other"}, "#ad promotion", "external_ad_forward"),
    ({}, "#ad discussed in original text", ""),
])
def test_content_filter_is_shared_by_comments_and_likes(metadata, text, reason):
    source = NS(source_metadata={"observed": True, **metadata}, content_preview=text)
    assert source_filter_reason(source, task_type="channel_comment") == reason
    assert source_filter_reason(source, task_type="channel_like") == reason
    assert source_filter_reason(source, task_type="channel_view") == ""


def test_source_modes_never_treat_unknown_observation_as_no_posts():
    for mode in ("continuous_event_driven", "finite_existing_sources", "promised_daily_sources"):
        assert source_opportunity_state(mode, complete=False, has_sources=False, day_closed=True) == "source_ingestion_unproven"
    assert source_opportunity_state("continuous_event_driven", complete=True, has_sources=False, day_closed=True) == "neutral_no_opportunity"
    assert source_opportunity_state("promised_daily_sources", complete=True, has_sources=False, day_closed=True) == "missed_promised_source"
    assert source_opportunity_state("finite_existing_sources", complete=True, has_sources=False) == "missed_no_source"


def test_initial_range_is_not_frozen_from_incomplete_or_stale_observation():
    session, task, _, _ = seed_source_session()
    with session:
        row = message(session, 1)
        assert unified_source_intake(session, task, [row], config=task.type_config, observation_complete=False) == []
        assert list(session.scalars(select(ChannelTaskIntake))) == []
        assert task.stats["source_intake"]["state"] == "source_ingestion_unproven"


def test_finite_existing_sources_not_truncated_by_default_backlog_limit():
    session, task, _, _ = seed_source_session()
    with session:
        # 10 historical messages before NOW
        old = [message(session, i, at=NOW - timedelta(minutes=20 - i)) for i in range(1, 11)]
        config = {
            **task.type_config,
            "source_expectation_mode": "finite_existing_sources",
            "message_scope": "all_available",
        }
        result = unified_source_intake(
            session, task, old, config=config, observation_complete=True
        )
        # All 10 messages must be accepted without default 5 limit truncation!
        assert len(result) == 10
        intake = session.scalar(select(ChannelTaskIntake))
        assert len(intake.initial_source_keys) == 10
