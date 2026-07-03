from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models import BotProtocolSample, SearchJoinLinkedTaskDispatch, SearchJoinRankObservation, Tenant


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.no_postgres
def test_search_join_group_dataflow_tables_exist_in_metadata() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())

    assert "search_join_rank_observations" in tables
    assert "search_join_linked_task_dispatches" in tables
    assert "bot_protocol_samples" in tables


@pytest.mark.no_postgres
def test_search_join_rank_observation_and_linked_dispatch_roundtrip() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        observation = SearchJoinRankObservation(
            tenant_id=1,
            task_id="task-1",
            bot_username="jisou",
            keyword_hash="a" * 64,
            target_group_id=17,
            observed_position=3,
            total_results=20,
            observed_region="CN-SH",
            observation_source="bot_search_result",
            paid_keyword_ad_status="unknown",
            jisou_ecosystem_status="unknown",
            target_relevance_score=80,
            target_content_health="healthy",
        )
        dispatch = SearchJoinLinkedTaskDispatch(
            tenant_id=1,
            search_join_action_id="action-1",
            source_task_id="task-1",
            linked_task_id="ai-task-1",
            account_id=101,
            target_group_id=17,
            link_type="group_ai_chat",
            status="linked_task_ready_pending",
            block_reason="cooldown_waiting",
        )
        sample = BotProtocolSample(
            tenant_id=1,
            bot_username="jisou",
            sample_type="search_results",
            sample_hash="sample-hash",
            schema_version="v1",
            structure_json={"buttons": [{"effect": "join_candidate"}]},
            pii_scrubbed=True,
            is_active=True,
        )
        session.add_all([observation, dispatch, sample])
        session.commit()

        saved_observation = session.query(SearchJoinRankObservation).one()
        saved_dispatch = session.query(SearchJoinLinkedTaskDispatch).one()
        saved_sample = session.query(BotProtocolSample).one()

    assert saved_observation.keyword_hash == "a" * 64
    assert saved_dispatch.status == "linked_task_ready_pending"
    assert saved_sample.bot_username == "jisou"


@pytest.mark.no_postgres
def test_search_join_group_migration_declares_tables() -> None:
    migration = PROJECT_ROOT / "backend/migrations/versions/0075_search_join_group.py"
    source = migration.read_text()

    assert "search_join_rank_observations" in source
    assert "search_join_linked_task_dispatches" in source
    assert "bot_protocol_samples" in source
