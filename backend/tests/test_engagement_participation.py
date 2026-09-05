from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountBehaviorBudgetPolicyRevision,
    AccountFleetActivityLedger,
    AccountFleetActivityPolicyRevision,
    AccountProxy,
    AiAccountVoiceProfile,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CrossAdapterSourceJourneyPlanRevision,
    OperationTarget,
    PlanningAdmissionSnapshot,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    TaskParticipationUnitPlan,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
    ViewAccountSourceAllocationPlan,
)
from app.schemas import ChannelViewConfig, ChannelViewTaskCreate
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.daily_coverage import ensure_task_daily_coverage
from app.services.task_center.engagement_participation import (
    ensure_daily_participation_plan,
    ensure_source_participation_plan,
)
from app.services.task_center.engagement_comment_participation import (
    prepare_comment_participation,
)
from app.services.task_center.service import create_channel_view_task
from app.services.task_center.engagement_group_scope import (
    sync_group_participation_scope,
)
from app.services.task_center.channel_membership import linked_channel_group
from app.services.task_center.executors.channel_comment_accounts import (
    prepare_comment_accounts,
)
from app.services.task_center.engagement_view_allocation import (
    allocation_account_ids_by_message,
    ensure_view_allocation_plan,
)
from app.services.task_center.engagement_planning_admission import (
    ensure_planning_admission_snapshot,
)
from app.services.task_center.engagement_source_journey import (
    JourneyDemand, register_source_journey_demand,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    from tests.account_group_revision_test_support import bootstrap_groups

    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="浏览组"))
    session.add_all(_account(account_id) for account_id in range(11, 15))
    session.add(
        OperationTarget(
            id=101,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-100101",
            title="测试频道",
        )
    )
    bootstrap_groups(session, 1, (1,))
    session.commit()
    return create_channel_view_task(
        session,
        1,
        ChannelViewTaskCreate(
            name="统一浏览",
            target_channel_id=101,
            engagement_contract_version="unified_engagement_v1",
            account_group_ids=[1],
            concurrency_limit_per_group=4,
            account_ratio_min_bps=5001,
            account_ratio_max_bps=5001,
            rolling_participation_days=3,
            per_message_daily_view_target=2,
        ),
        "tester",
    )


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        display_name=f"账号{account_id}",
        phone_masked=str(account_id),
        status="在线",
        session_ciphertext=f"session-{account_id}",
    )


def _messages(
    session: Session, count: int, *, start: int = 1
) -> list[ChannelMessage]:
    rows = [
        ChannelMessage(
            id=1000 + index,
            tenant_id=1,
            channel_target_id=101,
            message_id=9000 + index,
            content_preview=f"帖子{index}",
            published_at=datetime(2026, 9, 4, index, tzinfo=timezone.utc),
        )
        for index in range(start, start + count)
    ]
    session.add_all(rows)
    session.flush()
    for row in rows:
        revision = ChannelMessageSourceRevision(
            tenant_id=1,
            channel_target_id=101,
            channel_message_id=row.id,
            source_revision=1,
            source_remote_message_id=row.message_id,
            source_published_at=row.published_at,
            source_observed_at=row.published_at,
            source_text_snapshot=row.content_preview,
            source_content_hash=f"{row.id:064x}",
            observation_identity_hash=f"{row.message_id:064x}",
            source_length=len(row.content_preview),
            captured_length=len(row.content_preview),
        )
        session.add(revision)
        session.flush()
        row.current_source_revision_id = revision.id
    return rows


def _view_allocation(
    session: Session,
    task,
    messages: list[ChannelMessage],
    *,
    forbidden: dict[int, set[int]] | None = None,
    **config,
):
    ledger = ensure_task_day_ledger(
        session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
    )
    participation = ensure_daily_participation_plan(session, task, ledger)
    task.type_config = {**task.type_config, **config}
    admission = ensure_planning_admission_snapshot(
        session,
        task,
        participation,
        planning_horizon="task_day:2026-09-04",
    )
    return ensure_view_allocation_plan(
        session,
        task,
        ledger=ledger,
        participation_plan=participation,
        admission_snapshot=admission,
        messages=messages,
        forbidden_account_ids_by_message=forbidden or {},
        config=task.type_config,
    )


def test_view_allocation_is_non_cartesian_and_covers_cohort_and_sources() -> None:
    with _session() as session:
        task = _seed(session)
        plan = _view_allocation(
            session,
            task,
            _messages(session, 5),
            per_account_source_degree_min=2,
            per_account_source_degree_max=2,
        )

        assert plan.decision == "achievable"
        assert plan.edge_count == 6
        assert plan.edge_count < 3 * 5
        assert {item["assigned_degree"] for item in plan.account_degrees} == {2}
        assert min(item["assigned_exposure"] for item in plan.source_exposures) >= 1
        assert all(edge["source_journey_plan_id"] for edge in plan.edge_set)
        journey_ids = {edge["source_journey_plan_id"] for edge in plan.edge_set}
        assert all(
            session.get(CrossAdapterSourceJourneyPlanRevision, plan_id) is not None
            for plan_id in journey_ids
        )


def test_view_allocation_single_source_assigns_every_cohort_account_once() -> None:
    with _session() as session:
        task = _seed(session)
        message = _messages(session, 1)[0]

        plan = _view_allocation(session, task, [message])

        assert plan.edge_count == 3
        assert allocation_account_ids_by_message(plan)[message.id] == set(
            plan.account_degrees[index]["account_id"] for index in range(3)
        )


def test_view_budget_shortage_preserves_requirements_and_only_exposes_reserved_edges() -> None:
    with _session() as session:
        task = _seed(session)
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision).where(
            AccountBehaviorBudgetPolicyRevision.tenant_id == 1,
            AccountBehaviorBudgetPolicyRevision.state == "active",
        ))
        policy.action_budgets = {**policy.action_budgets, "view": 1}
        plan = _view_allocation(
            session, task, _messages(session, 5),
            per_account_source_degree_min=2, per_account_source_degree_max=2,
        )
        assert plan.edge_count == 6
        assert sum(row["assigned_exposure"] for row in plan.source_exposures) == 6
        assert {row["assigned_degree"] for row in plan.account_degrees} == {2}
        allowed = allocation_account_ids_by_message(plan, serviceable_only=True)
        assert sum(map(len, allowed.values())) == 3
        assert sum(not edge["portfolio_reserved_units"] for edge in plan.edge_set) == 3
        assert all(edge["portfolio_plan_id"] for edge in plan.edge_set)


def test_temporary_admission_failure_does_not_shrink_view_cohort_or_degree() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc),
        )
        participation = ensure_daily_participation_plan(session, task, ledger)
        blocked_id = int(participation.selected_account_ids[0])
        session.get(TgAccount, blocked_id).session_ciphertext = None
        plan = _view_allocation(
            session, task, _messages(session, 5),
            per_account_source_degree_min=2, per_account_source_degree_max=2,
        )
        assert plan.edge_count == 6
        assert {row["assigned_degree"] for row in plan.account_degrees} == {2}
        assert blocked_id in {edge["account_id"] for edge in plan.edge_set}
        admission = session.get(PlanningAdmissionSnapshot, plan.planning_admission_snapshot_id)
        assert blocked_id in admission.deficit_account_ids
        assert blocked_id not in admission.admissible_account_ids


def test_other_adapter_overlap_never_overrides_view_cohort_degree_bounds() -> None:
    with _session() as session:
        task = _seed(session)
        messages = _messages(session, 5)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc),
        )
        participation = ensure_daily_participation_plan(session, task, ledger)
        cohort = tuple(participation.selected_account_ids)
        session.add(Task(
            id="comment-owner", tenant_id=1, name="评论", type="channel_comment",
            status="running", type_config={"engagement_contract_version": "unified_engagement_v1"},
        ))
        session.flush()
        for message in messages:
            register_source_journey_demand(
                session, session.get(ChannelMessageSourceRevision, message.current_source_revision_id),
                task_day=ledger.obligation_local_date,
                demand=JourneyDemand("comment-owner", "authored_comment", 2, cohort[:2]),
            )
        plan = _view_allocation(
            session, task, messages,
            per_account_source_degree_min=2, per_account_source_degree_max=2,
        )
        assert plan.decision == "achievable"
        assert plan.edge_count == 6
        assert {row["assigned_degree"] for row in plan.account_degrees} == {2}
        assert {row["account_id"] for row in plan.account_degrees} == set(cohort)
        for edge in plan.edge_set:
            journey = session.get(CrossAdapterSourceJourneyPlanRevision, edge["source_journey_plan_id"])
            constraint = next(item for item in journey.adapter_constraints if item["task_id"] == task.id)
            assert edge["account_id"] in constraint["hard_account_ids"]
            assert set(constraint["candidate_account_ids"]) == set(cohort)
            assert constraint["joint_constraint_hash"]


def test_view_allocation_successor_only_appends_frozen_edges() -> None:
    with _session() as session:
        task = _seed(session)
        first_message = _messages(session, 1)[0]
        first = _view_allocation(
            session,
            task,
            [first_message],
            per_account_source_degree_min=2,
            per_account_source_degree_max=2,
        )
        second_message = _messages(session, 1, start=2)[0]

        successor = _view_allocation(session, task, [first_message, second_message])
        repeated = _view_allocation(session, task, [first_message, second_message])

        assert successor.allocation_revision == 2
        assert successor.supersedes_plan_id == first.id
        assert {tuple(sorted(edge.items())) for edge in first.edge_set}.issubset(
            {tuple(sorted(edge.items())) for edge in successor.edge_set}
        )
        assert successor.edge_count == 6
        assert repeated is successor
        assert session.scalars(select(ViewAccountSourceAllocationPlan)).all() == [
            first,
            successor,
        ]


def test_view_allocation_records_joint_infeasibility_without_partial_edges() -> None:
    with _session() as session:
        task = _seed(session)

        plan = _view_allocation(
            session,
            task,
            _messages(session, 10),
            per_account_source_degree_min=2,
            per_account_source_degree_max=2,
        )

        assert plan.decision == "view_allocation_unachievable"
        assert plan.edge_count == 0
        assert len(plan.unallocated_sources) == 10


def test_view_allocation_rejects_edges_already_confirmed_by_same_account() -> None:
    with _session() as session:
        task = _seed(session)
        message = _messages(session, 1)[0]
        cohort = _view_allocation(session, task, [message])
        blocked_account = int(cohort.account_degrees[0]["account_id"])
        session.delete(cohort)
        session.flush()

        plan = _view_allocation(
            session,
            task,
            [message],
            forbidden={message.id: {blocked_account}},
        )

        assert plan.decision == "view_allocation_unachievable"
        assert plan.edge_count == 0


def test_unified_explicit_view_mode_requires_exactly_one_source_target() -> None:
    base = {
        "target_channel_id": 101,
        "engagement_contract_version": "unified_engagement_v1",
        "account_group_ids": [7],
        "view_exposure_mode": "explicit_per_source",
    }

    with pytest.raises(ValueError, match="必须且只能配置"):
        ChannelViewConfig(**base)
    valid = ChannelViewConfig(**base, per_source_exposure_target=2)
    assert valid.per_source_exposure_target == 2
    with pytest.raises(ValueError, match="必须且只能配置"):
        ChannelViewConfig(
            **base,
            per_source_exposure_target=2,
            per_source_exposure_ratio_bps=6000,
        )


def test_unified_view_modes_reject_ambiguous_exposure_targets() -> None:
    base = {
        "target_channel_id": 101,
        "engagement_contract_version": "unified_engagement_v1",
        "account_group_ids": [7],
    }
    with pytest.raises(ValueError, match="自然曝光模式"):
        ChannelViewConfig(**base, per_source_exposure_target=2)
    with pytest.raises(ValueError, match="逐帖全刷模式"):
        ChannelViewConfig(
            **base,
            every_active_message=True,
            per_source_exposure_target=2,
        )


def test_planning_admission_preserves_required_denominator_and_exposes_proxy_deficit() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        participation = ensure_daily_participation_plan(session, task, ledger)
        selected = [session.get(TgAccount, item) for item in participation.selected_account_ids]
        for account in selected:
            account.session_ciphertext = f"session-{account.id}"
        proxy = AccountProxy(
            id=91,
            tenant_id=1,
            name="异常代理",
            port=1080,
            status="unhealthy",
            alert_status="alerting",
        )
        session.add(proxy)
        selected[0].proxy = proxy
        session.flush()

        snapshot = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="task_day:2026-09-04",
        )

        assert len(snapshot.account_paths) == 3
        assert len(snapshot.admissible_account_ids) == 2
        assert snapshot.deficit_account_ids == [selected[0].id]
        assert snapshot.decision == "partially_serviceable"
        assert task.stats["planning_required_account_count"] == 3
        assert task.stats["planning_deficit_account_count"] == 1


def test_planning_admission_requires_masks_only_for_content_tasks() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        participation = ensure_daily_participation_plan(session, task, ledger)
        for account_id in participation.selected_account_ids:
            session.get(TgAccount, account_id).session_ciphertext = f"session-{account_id}"
        passive = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="view-day",
        )
        task.type = "channel_comment"
        content = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="comment-day",
        )

        assert passive.decision == "achievable"
        assert content.decision == "blocked"
        assert all(
            "account_mask_unavailable" in path["blocking_reasons"]
            for path in content.account_paths
        )


def test_planning_admission_reuses_only_unexpired_observation() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session,
            task,
            now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc),
        )
        participation = ensure_daily_participation_plan(session, task, ledger)
        for account_id in participation.selected_account_ids:
            session.get(TgAccount, account_id).session_ciphertext = (
                f"session-{account_id}"
            )
        observed_at = datetime(2026, 9, 4, 12, 0)

        first = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="task_day:2026-09-04",
            now=observed_at,
        )
        within_ttl = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="task_day:2026-09-04",
            now=observed_at + timedelta(minutes=1),
        )
        refreshed = ensure_planning_admission_snapshot(
            session,
            task,
            participation,
            planning_horizon="task_day:2026-09-04",
            now=observed_at + timedelta(minutes=6),
        )

        assert within_ttl.id == first.id
        assert refreshed.id != first.id
        assert session.query(PlanningAdmissionSnapshot).count() == 2


def test_comment_participation_uses_full_policy_denominator_while_ready_partition_runs() -> None:
    with _session() as session:
        task = _seed(session)
        task.type = "channel_comment"
        channel = session.get(OperationTarget, 101)
        group = linked_channel_group(session, channel, create=True)
        accounts = list(session.scalars(select(TgAccount).order_by(TgAccount.id)))
        session.add_all(
            TgGroupAccount(
                tenant_id=1,
                group_id=group.id,
                account_id=account.id,
                permission_label="已关注",
                can_send=True,
            )
            for account in accounts
        )
        for account in accounts:
            account.session_ciphertext = f"session-{account.id}"
        for account in accounts[:2]:
            account.tg_first_name = "测试"
            account.username = f"user_{account.id}"
            account.avatar_object_key = f"avatar/{account.id}.jpg"
            account.profile_sync_status = "已同步"
            session.add(
                AiAccountVoiceProfile(
                    tenant_id=1,
                    account_id=account.id,
                    version=1,
                    short_prompt_summary="成年男性日常聊天，表达自然克制",
                    status="active",
                    quality_status="active",
                )
            )
        session.flush()

        setup = prepare_comment_accounts(
            session, task, channel, config=task.type_config
        )

        assert len(setup.policy_accounts) == 4
        assert len(setup.accounts) == 2
        assert setup.admission_snapshot.decision == "partially_serviceable"
        assert len(setup.admission_snapshot.deficit_account_ids) == 2


def test_daily_view_cohort_is_deterministic_and_frozen() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )

        first = ensure_daily_participation_plan(session, task, ledger)
        second = ensure_daily_participation_plan(session, task, ledger)

        assert first is second
        assert first.rounded_selected_count == 2
        assert first.participation_min_count == 3
        assert first.required_count == 3
        assert first.sampled_ratio_bps == 5001
        assert first.realized_participation_bps == 7500
        assert first.integer_quantization_adjustment
        assert len(first.selected_account_ids) == 3
        assert session.scalars(select(TaskParticipationUnitPlan)).all() == [first]


def test_membership_change_does_not_rewrite_existing_unit() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        first = ensure_daily_participation_plan(session, task, ledger)
        session.add(_account(15))
        session.flush()

        same_unit = ensure_daily_participation_plan(session, task, ledger)

        assert same_unit.policy_eligible_account_ids == first.policy_eligible_account_ids
        assert 15 not in same_unit.policy_eligible_account_ids


def test_next_day_prefers_accounts_not_selected_previous_day() -> None:
    with _session() as session:
        task = _seed(session)
        first_ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        first = ensure_daily_participation_plan(session, task, first_ledger)
        second_ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 5, 3, tzinfo=timezone.utc)
        )

        second = ensure_daily_participation_plan(session, task, second_ledger)

        assert set(first.selected_account_ids) != set(second.selected_account_ids)
        assert set(first.selected_account_ids) | set(second.selected_account_ids) == {
            11,
            12,
            13,
            14,
        }


def test_daily_cohort_prioritizes_accounts_missing_cross_task_fleet_activity() -> None:
    with _session() as session:
        task = _seed(session)
        policy = session.scalar(select(AccountFleetActivityPolicyRevision))
        assert policy is not None
        for account_id in (11, 12, 13):
            session.add(AccountFleetActivityLedger(
                tenant_id=1, account_pool_id=1, account_id=account_id,
                policy_revision_id=policy.id, period_kind="calendar_day",
                period_start=date(2026, 9, 4), period_end=date(2026, 9, 4),
                activity_counts={"passive_operation": 1},
                qualified_activity_classes=["passive_operation"],
            ))
        session.flush()
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )

        plan = ensure_daily_participation_plan(session, task, ledger)

        assert len(plan.selected_account_ids) == 3
        assert 14 in plan.selected_account_ids


def test_source_plan_freezes_exact_required_count() -> None:
    with _session() as session:
        task = _seed(session)
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )

        plan = ensure_source_participation_plan(
            session,
            task,
            ledger,
            source_identity="message:9001",
            required_count=3,
        )

        assert plan.required_count == 3
        assert len(plan.selected_account_ids) == 3
        assert plan.participation_unit.endswith("source:message:9001")


def test_legacy_task_does_not_create_unified_plan() -> None:
    with _session() as session:
        task = _seed(session)
        task.type_config = {
            **task.type_config,
            "engagement_contract_version": "legacy_v0",
        }
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )

        assert ensure_daily_participation_plan(session, task, ledger) is None
        assert session.scalar(select(TaskParticipationUnitPlan)) is None


def test_comment_uses_configured_source_ratio_and_exposes_quantization() -> None:
    with _session() as session:
        task = _seed(session)
        task.type = "channel_comment"
        task.type_config = {
            **task.type_config,
            "account_ratio_min_bps": 6000,
            "account_ratio_max_bps": 6000,
        }
        message = type("Message", (), {"id": 99})()
        source = type("Source", (), {})()
        accounts = [_account(account_id) for account_id in range(21, 25)]

        decision = prepare_comment_participation(
            session,
            task,
            message,
            source=source,
            ledger=None,
            accounts=accounts,
            business_max=80,
        )

        assert decision.sampled_bps == 6000
        assert decision.rounded_count == 2
        assert decision.required_count == 2
        assert decision.realized_bps == 5000
        assert decision.integer_quantization_adjustment


def test_unified_comment_source_selection_is_owned_by_journey() -> None:
    with _session() as session:
        task = _seed(session)
        task.type = "channel_comment"
        task.type_config = {
            **task.type_config,
            "account_ratio_min_bps": 5000,
            "account_ratio_max_bps": 5000,
        }
        message = _messages(session, 1)[0]
        source = session.get(
            ChannelMessageSourceRevision,
            message.current_source_revision_id,
        )
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        accounts = list(session.scalars(select(TgAccount).order_by(TgAccount.id)))

        decision = prepare_comment_participation(
            session,
            task,
            message,
            source=source,
            ledger=ledger,
            accounts=accounts,
            business_max=80,
        )

        journey = session.get(
            CrossAdapterSourceJourneyPlanRevision,
            decision.journey_plan_id,
        )
        assert journey is not None
        assert decision.source_plan.plan_revision in {1, 2}
        assert decision.source_plan.selected_account_ids == [
            account.id for account in decision.ranked_accounts[:decision.required_count]
        ]


def test_group_daily_plan_keeps_unready_accounts_in_coverage_denominator() -> None:
    with _session() as session:
        task = _seed(session)
        group = TgGroup(id=201, tenant_id=1, tg_peer_id="-100201", title="活群")
        target = OperationTarget(
            id=201,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100201",
            title="活群",
        )
        session.add_all([group, target])
        task.type = "group_ai_chat"
        task.type_config = {
            **task.type_config,
            "target_group_id": group.id,
            "target_operation_target_id": target.id,
            "account_coverage_mode": "all_accounts_daily",
        }
        ledger = ensure_task_day_ledger(
            session, task, now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc)
        )
        plan = ensure_daily_participation_plan(session, task, ledger)

        created = sync_group_participation_scope(
            session,
            task,
            group,
            account_ids=list(plan.selected_account_ids),
        )
        ensure_task_daily_coverage(
            session,
            task,
            now=datetime(2026, 9, 4, 3, tzinfo=timezone.utc),
            account_ids=list(plan.selected_account_ids),
            target_group=group,
            refresh_existing=True,
        )

        assert created == 4
        assert session.query(TaskMembershipAdmissionItem).count() == 4
        assert session.query(TaskAccountDailyCoverage).count() == 4
