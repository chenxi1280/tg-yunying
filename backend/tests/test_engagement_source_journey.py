from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    CrossAdapterSourceJourneyPlanRevision,
    OperationTarget,
    SourceJourneyDecision,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.engagement_source_journey import (
    JourneyDemand,
    compile_source_journey,
    register_source_journey_demand,
)


pytestmark = pytest.mark.no_postgres
TASK_DAY = date(2026, 9, 5)


def test_joint_journey_preserves_quantities_and_minimizes_overlap() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        decision = compile_source_journey(
            session,
            source,
            task_day=TASK_DAY,
            demands=_three_adapter_demands(),
        )
        session.commit()

        edges = decision.account_ids_by_task_action
        assert len(edges[("comment-task", "authored_comment")]) == 3
        assert len(edges[("like-task", "reaction")]) == 3
        assert len(edges[("view-task", "view")]) == 4
        assert decision.plan.overlap_metrics == {
            "comment_count": 3,
            "reaction_count": 3,
            "view_count": 4,
            "reaction_comment_overlap": 1,
            "minimum_reaction_comment_overlap": 1,
            "triple_overlap": 0,
        }
        assert decision.plan.decision == "feasible"
        assert session.scalar(select(func.count(SourceJourneyDecision.id))) == 10


def test_one_hard_deficit_prevents_partial_cross_adapter_commit() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        decision = compile_source_journey(
            session,
            source,
            task_day=TASK_DAY,
            demands=[
                JourneyDemand("comment-task", "authored_comment", 3, (1, 2)),
                JourneyDemand("like-task", "reaction", 1, (1, 2, 3)),
            ],
        )

        assert not decision.achievable
        assert decision.account_ids_by_task_action == {}
        assert decision.plan.edge_set == []
        assert decision.plan.deficits == [{
            "task_id": "comment-task",
            "action_class": "authored_comment",
            "required_count": 3,
            "available_count": 2,
            "reason": "eligible_account_capacity_insufficient",
        }]
        assert session.scalar(select(func.count(SourceJourneyDecision.id))) == 0


def test_replay_is_idempotent_and_changed_input_creates_successor() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        first_demands = [
            JourneyDemand("comment-task", "authored_comment", 2, (1, 2, 3)),
        ]
        first = compile_source_journey(
            session, source, task_day=TASK_DAY, demands=first_demands,
        )
        replay = compile_source_journey(
            session, source, task_day=TASK_DAY, demands=first_demands,
        )
        successor = compile_source_journey(
            session,
            source,
            task_day=TASK_DAY,
            demands=first_demands + [
                JourneyDemand("like-task", "reaction", 2, (1, 2, 3))
            ],
        )

        assert replay.plan.id == first.plan.id
        assert successor.plan.id != first.plan.id
        assert successor.plan.plan_revision == 2
        assert successor.plan.supersedes_plan_id == first.plan.id
        assert first.plan.state == "superseded"
        assert successor.plan.state == "active"
        assert (
            successor.account_ids_by_task_action[
                ("comment-task", "authored_comment")
            ]
            == first.account_ids_by_task_action[
                ("comment-task", "authored_comment")
            ]
        )
        assert session.scalar(select(func.count(
            CrossAdapterSourceJourneyPlanRevision.id
        ))) == 2


def test_unachievable_late_task_does_not_replace_existing_active_plan() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        first = compile_source_journey(
            session,
            source,
            task_day=TASK_DAY,
            demands=[JourneyDemand(
                "comment-task", "authored_comment", 2, (1, 2, 3),
            )],
        )
        rejected_demands = [
            JourneyDemand("comment-task", "authored_comment", 2, (1, 2, 3)),
            JourneyDemand("like-task", "reaction", 4, (1, 2, 3)),
        ]

        rejected = compile_source_journey(
            session, source, task_day=TASK_DAY, demands=rejected_demands,
        )
        replay = compile_source_journey(
            session, source, task_day=TASK_DAY, demands=rejected_demands,
        )

        active = session.scalar(select(
            CrossAdapterSourceJourneyPlanRevision
        ).where(CrossAdapterSourceJourneyPlanRevision.state == "active"))
        assert not rejected.achievable
        assert rejected.plan.state == "rejected"
        assert replay.plan.id == rejected.plan.id
        assert active.id == first.plan.id
        assert first.plan.state == "active"


def test_multiple_tasks_of_same_adapter_are_jointly_supported() -> None:
    with _session() as session:
        session.add(_task("second-like-task", "channel_like"))
        session.flush()
        source = session.get(ChannelMessageSourceRevision, "source-revision")

        decision = compile_source_journey(
            session,
            source,
            task_day=TASK_DAY,
            demands=[
                JourneyDemand("like-task", "reaction", 2, (1, 2, 3)),
                JourneyDemand("second-like-task", "reaction", 1, (3, 4, 5)),
            ],
        )

        assert len(decision.account_ids_by_task_action[("like-task", "reaction")]) == 2
        assert len(
            decision.account_ids_by_task_action[("second-like-task", "reaction")]
        ) == 1


def test_incremental_registration_preserves_prior_demand_and_uses_preference() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        first = register_source_journey_demand(
            session,
            source,
            task_day=TASK_DAY,
            demand=JourneyDemand(
                "comment-task", "authored_comment", 2, (1, 2, 3, 4), (1, 2),
            ),
        )
        successor = register_source_journey_demand(
            session,
            source,
            task_day=TASK_DAY,
            demand=JourneyDemand(
                "like-task", "reaction", 2, (1, 2, 3, 4), (1, 2),
            ),
        )

        assert successor.plan.plan_revision == 2
        assert successor.account_ids_by_task_action[
            ("comment-task", "authored_comment")
        ] == first.account_ids_by_task_action[
            ("comment-task", "authored_comment")
        ]
        assert set(successor.account_ids_by_task_action[("like-task", "reaction")]) == {
            3, 4,
        }
        assert len(successor.plan.adapter_constraints) == 2


def test_joint_graph_witness_is_hard_even_when_local_overlap_would_choose_differently() -> None:
    with _session() as session:
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        decision = compile_source_journey(
            session, source, task_day=TASK_DAY,
            demands=[
                JourneyDemand("comment-task", "authored_comment", 1, (1,)),
                JourneyDemand(
                    task_id="view-task", action_class="view", required_count=1,
                    candidate_account_ids=(1, 2), hard_account_ids=(1,),
                    joint_constraint_hash="joint-graph-proof",
                ),
            ],
        )
        assert decision.achievable
        assert decision.account_ids_by_task_action[("view-task", "view")] == (1,)


def test_joint_graph_witness_cannot_introduce_an_ineligible_account() -> None:
    with _session() as session:
        with pytest.raises(ValueError, match="hard_account_not_eligible"):
            compile_source_journey(
                session, session.get(ChannelMessageSourceRevision, "source-revision"),
                task_day=TASK_DAY,
                demands=[JourneyDemand(
                    task_id="view-task", action_class="view", required_count=1,
                    candidate_account_ids=(1,), hard_account_ids=(2,),
                    joint_constraint_hash="joint-graph-proof",
                )],
            )


def test_non_unified_task_cannot_enter_joint_journey() -> None:
    with _session() as session:
        task = session.get(Task, "comment-task")
        task.type_config = {}
        source = session.get(ChannelMessageSourceRevision, "source-revision")
        with pytest.raises(ValueError, match="source_journey_task_not_unified"):
            compile_source_journey(
                session,
                source,
                task_day=TASK_DAY,
                demands=[
                    JourneyDemand("comment-task", "authored_comment", 1, (1,))
                ],
            )


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    _seed(session)
    session.commit()
    return session


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(OperationTarget(
        id=31,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10031",
        title="测试频道",
    ))
    session.add(ChannelMessage(
        id=41,
        tenant_id=1,
        channel_target_id=31,
        message_id=401,
        content_preview="测试来源",
    ))
    session.add(_source_revision())
    session.add_all(_accounts())
    session.add_all([
        _task("comment-task", "channel_comment"),
        _task("like-task", "channel_like"),
        _task("view-task", "channel_view"),
    ])


def _source_revision() -> ChannelMessageSourceRevision:
    observed = datetime(2026, 9, 5, 1, tzinfo=timezone.utc)
    return ChannelMessageSourceRevision(
        id="source-revision",
        tenant_id=1,
        channel_target_id=31,
        channel_message_id=41,
        source_revision=1,
        source_remote_message_id=401,
        source_published_at=observed,
        source_observed_at=observed,
        source_text_snapshot="测试来源",
        source_content_hash="source-hash",
        observation_identity_hash="observation-hash",
        source_length=4,
        captured_length=4,
    )


def _accounts() -> list[TgAccount]:
    return [
        TgAccount(
            id=account_id,
            tenant_id=1,
            display_name=f"账号{account_id}",
            phone_masked=str(account_id),
            status="在线",
        )
        for account_id in range(1, 6)
    ]


def _task(task_id: str, task_type: str) -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type=task_type,
        status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    )


def _three_adapter_demands() -> list[JourneyDemand]:
    account_ids = tuple(range(1, 6))
    return [
        JourneyDemand("comment-task", "authored_comment", 3, account_ids),
        JourneyDemand("like-task", "reaction", 3, account_ids),
        JourneyDemand("view-task", "view", 4, account_ids),
    ]
