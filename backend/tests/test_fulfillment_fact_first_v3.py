from __future__ import annotations

from datetime import datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    AiProvider,
    ExecutionAttempt,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
    GenerationJob,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupBotAdmission,
    AccountGroupAdmissionFact,
    Tenant,
    TenantAiSetting,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
)
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from app.services._common import _now
from app.services.task_center.daily_coverage_planning import ready_coverage_plan_batch
from app.services.task_center.ai_generation_worker import drain_ai_generation
from app.services.task_center.ai_generation_parallel import _claim_job, _job_available
from app.services.task_center.ai_generator import _provider_for_exact_model
from app.services.ai_config import update_ai_provider
from app.schemas import AiProviderUpdate
from app.services.task_center.fulfillment_activation import (
    ActivationRequest,
    activate_manifest,
    clone_prepared_task,
    gateway_task_allowed,
    prepare_activation_manifest,
    preview_activation,
)
from app.services.task_center.task_group_bot_admission_v2 import (
    evaluate_task_admission,
)
from app.services.task_center.task_group_bot_admission_recovery import (
    reopen_unproven_task_coverages,
)
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation
from app.services.task_center.executors import group_ai_chat
from app.services.task_center import dispatcher, service as task_service


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add_all([
            Tenant(id=1, name="履约契约测试租户"),
            AiProvider(
                id=1,
                provider_name="test-provider",
                base_url="https://provider.invalid",
                model_name="test-model",
                api_key_ciphertext="encrypted-test-key",
            ),
            TenantAiSetting(
                id=1,
                tenant_id=1,
                default_provider_id=1,
                ai_enabled=True,
            ),
        ])
        current.commit()
        yield current


def _task(task_id: str, *, status: str = "running", contract: str = "fact_first_v3") -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="group_ai_chat",
        status=status,
        fulfillment_contract_version=contract,
        account_config={"mode": "all"},
        type_config={"daily_message_target": 10},
    )


def test_v3_schema_removes_global_account_execution_unique_index(session: Session) -> None:
    indexes = {row["name"] for row in inspect(session.get_bind()).get_indexes("actions")}

    assert "uq_actions_executing_account" not in indexes
    assert "uq_actions_open_obligation" in indexes
    assert inspect(session.get_bind()).has_table("search_click_assignments")
    assert inspect(session.get_bind()).has_table("fulfillment_remote_facts")
    provider_indexes = {
        row["name"] for row in inspect(session.get_bind()).get_indexes("ai_providers")
    }
    assert "uq_ai_provider_single_active" in provider_indexes


def test_generation_job_expired_lease_compares_naive_database_time_safely() -> None:
    job = GenerationJob(
        state="generating",
        lease_expires_at=datetime(2026, 8, 4, 8, 0),
    )
    now = datetime(2026, 8, 4, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert _job_available(job, now)


def test_generation_job_claim_cas_does_not_run_python_datetime_evaluator(
    session: Session,
) -> None:
    job = GenerationJob(
        tenant_id=1,
        task_id="task-a",
        obligation_type="quantity_slot",
        obligation_id="timezone-cas-obligation",
        generation_sequence=1,
        context_snapshot_version=1,
        state="generating",
        lease_expires_at=datetime(2026, 8, 4, 8, 0),
    )
    session.add(job)
    session.commit()

    changed = _claim_job(
        session,
        job,
        owner="worker-timezone-regression",
        now_value=datetime(2026, 8, 4, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        expected_version=1,
    )

    assert changed == 1


def test_activating_provider_replaces_active_key_and_reuses_family_model(
    session: Session,
) -> None:
    replacement = AiProvider(
        id=2,
        provider_name="MiniMax shared key",
        base_url="https://api.minimax.invalid",
        model_name="MiniMax-M3",
        api_key_ciphertext="encrypted-shared-key",
        is_active=False,
        health_status="禁用",
    )
    session.add(replacement)
    session.commit()

    update_ai_provider(
        session,
        replacement.id,
        AiProviderUpdate(is_active=True),
        "test-actor",
    )

    assert not session.get(AiProvider, 1).is_active
    assert session.get(AiProvider, 2).is_active
    assert session.get(TenantAiSetting, 1).default_provider_id == 2
    assert _provider_for_exact_model(session, "MiniMax-M2.5").id == 2


def test_direct_claim_first_round_covers_each_running_task(session: Session) -> None:
    now = datetime(2000, 1, 1)
    tasks = [_task(f"task-{index}") for index in range(3)]
    session.add_all(tasks)
    session.flush()
    for task in tasks:
        for index in range(8):
            session.add(Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                scheduled_at=now - timedelta(minutes=10 - index),
            ))
    session.commit()

    batch = claim_fact_first_candidates(
        session,
        owner="test-worker",
        limit=3,
        now=now,
        lease_seconds=30,
    )

    task_ids = set(session.scalars(
        select(Action.task_id).where(Action.id.in_(batch.action_ids))
    ))
    assert task_ids == {task.id for task in tasks}


def test_fact_first_stale_lifecycle_epoch_cannot_claim_or_reach_gateway(
    session: Session,
) -> None:
    now = datetime(2000, 1, 1)
    task = _task("epoch-task")
    task.task_lifecycle_epoch = 2
    stale = Action(
        id="stale-epoch-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        task_lifecycle_epoch=1,
        scheduled_at=now,
    )
    current = Action(
        id="current-epoch-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        task_lifecycle_epoch=2,
        scheduled_at=now,
    )
    session.add_all([task, stale, current])
    session.commit()

    batch = claim_fact_first_candidates(
        session,
        owner="epoch-worker",
        limit=10,
        now=now,
        lease_seconds=30,
    )

    assert batch.action_ids == (current.id,)
    assert not dispatcher._fulfillment_route_allows_gateway(session, stale)
    assert dispatcher._fulfillment_route_allows_gateway(session, current)


def test_fact_first_invalid_target_terminates_task_and_fences_siblings(
    session: Session,
) -> None:
    task = _task("terminal-target-task")
    failed = Action(
        id="terminal-target-failed",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="executing",
        task_lifecycle_epoch=1,
    )
    sibling = Action(
        id="terminal-target-sibling",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="pending",
        task_lifecycle_epoch=1,
    )
    session.add_all([task, failed, sibling])
    session.flush()

    dispatcher._terminalize_fact_first_target(session, failed, "peer_id_invalid")

    assert task.status == "failed"
    assert task.task_lifecycle_epoch == 2
    assert task.stats["target_terminal"] is True
    assert sibling.status == "skipped"
    assert not dispatcher._fulfillment_route_allows_gateway(session, failed)


def test_direct_claim_separates_search_lane_and_waits_for_ai_generation(
    session: Session,
) -> None:
    now = datetime(2000, 1, 1)
    task = _task("lane-task")
    session.add(task)
    session.add_all([
        Action(
            id="lane-search",
            tenant_id=1,
            task_id=task.id,
            task_type="search_click",
            action_type="search_join",
            execution_lane="search",
            scheduled_at=now,
        ),
        Action(
            id="lane-interaction",
            tenant_id=1,
            task_id=task.id,
            task_type="channel_view",
            action_type="view_message",
            execution_lane="interaction",
            scheduled_at=now,
        ),
        Action(
            id="lane-ai-pending",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            execution_lane="interaction",
            scheduled_at=now,
            payload={"message_text": "", "ai_generation_status": "pending"},
        ),
    ])
    session.commit()

    search = claim_fact_first_candidates(
        session,
        owner="search-worker",
        limit=3,
        now=now,
        lease_seconds=30,
        execution_lane="search",
    )
    interaction = claim_fact_first_candidates(
        session,
        owner="interaction-worker",
        limit=3,
        now=now,
        lease_seconds=30,
        execution_lane="non_search",
    )

    assert search.action_ids == ("lane-search",)
    assert interaction.action_ids == ("lane-interaction",)
    assert session.get(Action, "lane-ai-pending").status == "pending"


def test_fact_first_ai_slot_ignores_legacy_capacity_without_overwriting_schedule(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("fact-first-capacity")
    task.pacing_config = {
        "operation_profile": {"hourly_activity_curve": [1] * 24},
    }
    accounts = [TgAccount(id=101, tenant_id=1), TgAccount(id=102, tenant_id=1)]
    now_value = datetime(2026, 8, 4, 12, 0)
    planned_at = datetime(2026, 8, 4, 13, 0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: now_value)
    monkeypatch.setattr(
        group_ai_chat,
        "_available_accounts_at",
        lambda *_args, **_kwargs: pytest.fail("legacy capacity must not run"),
    )

    account, planned_at = group_ai_chat._choose_capacity_slot(
        session,
        task,
        selected=accounts,
        planned_at=planned_at,
        index=0,
        used_account_ids=set(),
        allow_repeat=True,
        progress={"deficit": 999},
        reservations=[],
        capacity_cache=object(),
    )

    assert account in accounts
    assert planned_at == datetime(2026, 8, 4, 13, 0)
    schedule = group_ai_chat._schedule_times_for_plan(session, task, {}, 3, mode="正常期")
    assert schedule[0] == now_value
    assert schedule == sorted(schedule)
    assert len(set(schedule)) == 3


@pytest.mark.parametrize(
    "scenario",
    [
        ("channel_comment", 80),
        ("channel_view", 796),
        ("channel_like", 50),
        ("group_ai_chat", 20),
    ],
)
def test_fact_first_human_paced_actions_keep_future_schedule(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    scenario: tuple[str, int],
) -> None:
    task_type, action_count = scenario
    now_value = datetime(2026, 8, 5, 12, 0)
    future = now_value + timedelta(hours=6)
    task = _task(f"paced-{task_type}")
    task.type = task_type
    action_type = {
        "channel_comment": "post_comment",
        "channel_like": "like_message",
        "channel_view": "view_message",
        "group_ai_chat": "send_message",
    }[task_type]
    expected = [future + timedelta(seconds=index) for index in range(action_count)]
    session.add(task)
    session.add_all([
        Action(
            id=f"paced-action-{task_type}-{index}",
            tenant_id=1,
            task_id=task.id,
            task_type=task_type,
            action_type=action_type,
            status="pending",
            scheduled_at=scheduled_at,
        )
        for index, scheduled_at in enumerate(expected)
    ])
    session.flush()
    monkeypatch.setattr(task_service, "_now", lambda: now_value)

    task_service._make_fact_first_actions_immediate(session, task)
    session.flush()
    session.expire_all()
    actual = list(session.scalars(
        select(Action.scheduled_at)
        .where(Action.task_id == task.id)
        .order_by(Action.scheduled_at)
    ))

    assert actual == expected


@pytest.mark.parametrize("task_type", ["search_click"])
def test_fact_first_immediate_task_keeps_existing_behavior(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
) -> None:
    now_value = datetime(2026, 8, 5, 12, 0)
    task = _task(f"immediate-{task_type}")
    task.type = task_type
    action = Action(
        id=f"immediate-{task_type}-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_click" if task_type == "search_click" else "post_comment",
        status="pending",
        scheduled_at=now_value + timedelta(hours=6),
    )
    session.add_all([task, action])
    session.flush()
    monkeypatch.setattr(task_service, "_now", lambda: now_value)

    task_service._make_fact_first_actions_immediate(session, task)
    session.flush()
    session.refresh(action)

    assert action.scheduled_at == now_value


@pytest.mark.parametrize(
    "task_type",
    ["channel_comment", "channel_view", "channel_like", "group_ai_chat"],
)
def test_fact_first_human_paced_task_uses_type_specific_next_run(
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
) -> None:
    expected = datetime(2026, 8, 5, 12, 5)
    task = _task(f"next-run-{task_type}")
    task.type = task_type
    monkeypatch.setattr(task_service, "next_run_after_task", lambda _task: expected)

    assert task_service._next_planner_run_at(task) == expected


def test_fact_first_coverage_need_uses_group_due_instead_of_midnight_debt() -> None:
    task = _task("paced-coverage-need")

    assert group_ai_chat._effective_coverage_due_debt(
        task,
        coverage_due_debt=400,
        volume_need=3,
    ) == 3


def test_open_obligation_rebinds_after_pre_gateway_failure(session: Session) -> None:
    task = _task("rebind-task")
    first = Action(
        id="rebind-first",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        obligation_type="coverage",
        obligation_id="coverage-1",
    )
    session.add_all([task, first])
    session.flush()
    assert ensure_action_obligation(session, first)
    first.status = "failed"
    second = Action(
        id="rebind-second",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        obligation_type="coverage",
        obligation_id="coverage-1",
    )
    session.add(second)
    session.flush()

    assert ensure_action_obligation(session, second)
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_id == "coverage-1"
    ))
    assert projection.active_action_id == second.id
    assert projection.materialization_version == 2


def test_activation_requires_real_canary_fact_before_cutover(session: Session) -> None:
    old = _task("old-task", contract="legacy_v1")
    session.add(old)
    session.flush()
    new = clone_prepared_task(session, old, actor_id=None)
    preview = preview_activation(
        session,
        tenant_id=1,
        old_task_ids=(old.id,),
        new_task_ids=(new.id,),
    )
    manifest = prepare_activation_manifest(session, ActivationRequest(
        tenant_id=1,
        release_train="test-train",
        old_task_ids=(old.id,),
        new_task_ids=(new.id,),
        canary_task_id=new.id,
        expected_old_set_hash=preview.old_set_hash,
        expected_new_config_set_hash=preview.new_config_set_hash,
        approval_ref="user-approved-test",
    ))

    assert gateway_task_allowed(session, old)
    assert gateway_task_allowed(session, new)
    with pytest.raises(ValueError, match="activation_canary_remote_fact_required"):
        activate_manifest(session, manifest.id, expected_version=1)

    canary_action = Action(
        id="canary-action",
        tenant_id=1,
        task_id=new.id,
        task_type=new.type,
        action_type="send_message",
        status="success",
        obligation_type="coverage",
        obligation_id="canary-obligation",
    )
    canary_attempt = ExecutionAttempt(
        id="canary-attempt",
        tenant_id=1,
        action_id=canary_action.id,
        status="success",
    )
    session.add_all([canary_action, canary_attempt, FulfillmentRemoteFact(
        tenant_id=1,
        task_type=new.type,
        task_id=new.id,
        obligation_type="coverage",
        obligation_id="canary-obligation",
        action_id="canary-action",
        attempt_id="canary-attempt",
        mutation_kind="send_message",
        remote_mutation_key_hash="a" * 64,
        gateway_request_hash="b" * 64,
        fact_kind="view_observed",
        fact_identity_hash="c" * 64,
        outcome={"remote_message_id": "1"},
    )])
    session.flush()

    with pytest.raises(ValueError, match="activation_canary_remote_fact_required"):
        activate_manifest(session, manifest.id, expected_version=1)
    session.scalar(select(FulfillmentRemoteFact)).fact_kind = "remote_message_observed"
    session.flush()

    activated = activate_manifest(session, manifest.id, expected_version=1)

    assert activated.state == "active"
    assert session.get(Task, old.id).status == "stopped"
    assert session.get(Task, new.id).status == "running"
    assert not gateway_task_allowed(session, old)


def test_activation_manifest_allows_deleted_old_tasks_without_replacements(
    session: Session,
) -> None:
    old_running = _task("old-running", contract="legacy_v1")
    old_stopped = _task(
        "old-stopped",
        status="stopped",
        contract="legacy_v1",
    )
    session.add_all([old_running, old_stopped])
    session.flush()
    replacement = clone_prepared_task(session, old_running, actor_id=None)
    preview = preview_activation(
        session,
        tenant_id=1,
        old_task_ids=(old_running.id, old_stopped.id),
        new_task_ids=(replacement.id,),
    )

    manifest = prepare_activation_manifest(session, ActivationRequest(
        tenant_id=1,
        release_train="unequal-count-train",
        old_task_ids=(old_running.id, old_stopped.id),
        new_task_ids=(replacement.id,),
        canary_task_id=replacement.id,
        expected_old_set_hash=preview.old_set_hash,
        expected_new_config_set_hash=preview.new_config_set_hash,
        approval_ref="user-approved-test",
    ))

    assert manifest.old_task_ids == [old_running.id, old_stopped.id]
    assert manifest.new_task_ids == [replacement.id]


def test_c2_no_prompt_passes_only_after_exact_30_second_surface(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("c2-task")
    account = TgAccount(
        id=21,
        tenant_id=1,
        display_name="小明",
        phone_masked="***0021",
        session_ciphertext="encrypted-session",
    )
    group = TgGroup(id=31, tenant_id=1, tg_peer_id="-10031", title="目标群")
    authorization = TgAccountAuthorization(
        id=41,
        tenant_id=1,
        account_id=account.id,
        status="active",
        is_current=True,
    )
    session.add_all([task, account, group, authorization])
    session.flush()
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.credentials_for_account",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.gateway.fetch_group_messages",
        lambda *_args, **_kwargs: [],
    )

    first = evaluate_task_admission(
        session,
        task_id=task.id,
        tenant_id=1,
        group_id=group.id,
        account_id=account.id,
    )
    assert not first.allowed
    assert first.code == "c2_observation_started"

    admission = session.get(TaskGroupBotAdmission, first.admission_id)
    admission.no_prompt_pass_at = datetime(2000, 1, 1)
    session.flush()
    second = evaluate_task_admission(
        session,
        task_id=task.id,
        tenant_id=1,
        group_id=group.id,
        account_id=account.id,
    )
    assert second.allowed
    assert second.code == "c2_no_prompt_30s_passed"


def test_c2_unresolvable_target_entity_becomes_stable_task_terminal(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _seed_unresolvable_c2_scope(session, monkeypatch)
    first = _evaluate_c2_scope(session, scope)
    admission = session.get(TaskGroupBotAdmission, first.admission_id)
    admission.no_prompt_pass_at = datetime(2000, 1, 1)
    terminal = _evaluate_c2_scope(session, scope)
    terminal_version = admission.version
    repeated = _evaluate_c2_scope(session, scope)

    assert terminal.code == "c2_account_abandoned"
    assert admission.state == "abandoned"
    assert admission.terminal_reason == "target_entity_unresolvable"
    assert repeated.code == "c2_account_abandoned"
    assert admission.version == terminal_version
    admission.terminal_evidence = {
        **admission.terminal_evidence,
        "terminal_date": (_now().date() - timedelta(days=1)).isoformat(),
    }

    next_day = _evaluate_c2_scope(session, scope)

    assert next_day.code == "c2_observation_restarted_for_task_day"
    assert admission.state == "observing"


def _seed_unresolvable_c2_scope(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Task, TgAccount, TgGroup]:
    task = _task("c2-unresolvable-target")
    account = TgAccount(
        id=26, tenant_id=1, display_name="小磊", phone_masked="***0026",
        session_ciphertext="encrypted-session", status=AccountStatus.ACTIVE.value,
    )
    group = TgGroup(id=36, tenant_id=1, tg_peer_id="-10036", title="不可解析目标群")
    session.add_all([task, account, group])
    session.flush()
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.credentials_for_account",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.gateway.fetch_group_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Could not find the input entity for PeerChannel(channel_id=36)")
        ),
    )
    return task, account, group


def _evaluate_c2_scope(
    session: Session,
    scope: tuple[Task, TgAccount, TgGroup],
):
    task, account, group = scope
    return evaluate_task_admission(
        session,
        task_id=task.id,
        tenant_id=task.tenant_id,
        group_id=group.id,
        account_id=account.id,
    )


def test_c2_abandonment_projects_only_current_task_day_with_specific_reason(
    session: Session,
) -> None:
    task = _task("c2-current-day-terminal")
    action = Action(
        id="c2-current-day-action",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=27,
        status="pending",
        payload={"group_id": 37},
    )
    current = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=37,
        account_id=27,
        coverage_date=_now().date(),
        state="reserved",
    )
    historical = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=37,
        account_id=27,
        coverage_date=_now().date() - timedelta(days=1),
        state="ready",
    )
    session.add_all([task, action, current, historical])
    session.flush()

    dispatcher._abandon_fact_first_account_for_task(
        session,
        action,
        reason="target_entity_unresolvable",
    )

    assert current.state == "abandoned_for_day"
    assert current.blocker_code == "target_entity_unresolvable"
    assert historical.state == "ready"


def test_c2_missing_current_authorization_observes_with_session_identity(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("c2-missing-auth")
    account = TgAccount(
        id=22,
        tenant_id=1,
        display_name="小红",
        phone_masked="***0022",
        session_ciphertext="encrypted-session",
    )
    group = TgGroup(id=32, tenant_id=1, tg_peer_id="-10032", title="目标群二")
    session.add_all([task, account, group])
    session.flush()

    decision = evaluate_task_admission(
        session,
        task_id=task.id,
        tenant_id=1,
        group_id=group.id,
        account_id=account.id,
    )

    assert decision.code == "c2_observation_started"
    admission = session.get(TaskGroupBotAdmission, decision.admission_id)
    assert admission.state == "observing"
    assert admission.surface_identity["viewer_authorization_id"] == ""
    assert admission.surface_identity["viewer_session_identity_hash"]
    admission.no_prompt_pass_at = datetime(2000, 1, 1)
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.credentials_for_account",
        lambda *_: object(),
    )
    monkeypatch.setattr(
        "app.services.task_center.task_group_bot_admission_v2.gateway.fetch_group_messages",
        lambda *_args, **_kwargs: [],
    )

    ready = evaluate_task_admission(
        session,
        task_id=task.id,
        tenant_id=1,
        group_id=group.id,
        account_id=account.id,
    )

    assert ready.allowed
    assert ready.code == "c2_no_prompt_30s_passed"


def test_c2_reopens_unproven_authorization_terminal_and_coverage(
    session: Session,
) -> None:
    task = _task("c2-reopen-unproven")
    account = TgAccount(
        id=23,
        tenant_id=1,
        display_name="小刚",
        phone_masked="***0023",
        session_ciphertext="encrypted-session",
        status=AccountStatus.ACTIVE.value,
    )
    group = TgGroup(id=33, tenant_id=1, tg_peer_id="-10033", title="目标群三")
    admission = TaskGroupBotAdmission(
        tenant_id=1,
        task_id=task.id,
        account_id=account.id,
        target_group_id=group.id,
        state="abandoned",
        no_prompt_pass_at=_now(),
        surface_identity_hash="old",
        surface_identity={},
        terminal_reason="current_authorization_missing",
    )
    coverage = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=group.id,
        account_id=account.id,
        coverage_date=_now().date(),
        state="abandoned_for_day",
        blocker_code="account_task_abandoned",
    )
    session.add_all([task, account, group, admission, coverage])
    session.flush()

    reopened = reopen_unproven_task_coverages(
        session,
        task,
        group,
        limit=20,
    )

    assert reopened == 1
    assert admission.state == "observing"
    assert admission.terminal_reason == ""
    assert coverage.state == "pending_admission"
    assert coverage.blocker_code == "group_bot_admission_wait"


def test_fact_first_planning_materializes_pending_admission_coverage(
    session: Session,
) -> None:
    task = _task("c2-pending-materialization")
    account = TgAccount(
        id=24,
        tenant_id=1,
        display_name="小强",
        phone_masked="***0024",
        session_ciphertext="encrypted-session",
    )
    group = TgGroup(id=34, tenant_id=1, tg_peer_id="-10034", title="目标群四")
    coverage = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=group.id,
        account_id=account.id,
        coverage_date=_now().date(),
        state="pending_admission",
        targeted_at=_now() - timedelta(minutes=1),
    )
    session.add_all([task, account, group, coverage])
    session.flush()

    rows = ready_coverage_plan_batch(session, task, now=_now(), limit=20).rows

    assert [row.id for row in rows] == [coverage.id]


def test_fact_first_c2_action_reserves_pending_admission_coverage(
    session: Session,
) -> None:
    task = _task("c2-pending-reservation")
    coverage = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id=task.id,
        group_id=35,
        account_id=25,
        coverage_date=_now().date(),
        state="pending_admission",
    )
    session.add_all([task, coverage])
    session.flush()

    reserved = group_ai_chat._reserve_coverage_before_action(
        session,
        coverage.id,
        "c2-reservation-token",
        allow_pending_admission=True,
    )

    assert reserved
    session.refresh(coverage)
    assert coverage.state == "reserved"
    assert coverage.reservation_token == "c2-reservation-token"


def test_v3_ai_generation_calls_provider_concurrently(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'parallel.db'}", future=True)
    Base.metadata.create_all(engine)
    now = datetime(2000, 1, 1)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="并发生成租户"))
        task = _task("parallel-generation-task")
        session.add(task)
        session.add_all([
            TgAccount(id=51, tenant_id=1, display_name="账号51", phone_masked="***0051"),
            TgAccount(id=52, tenant_id=1, display_name="账号52", phone_masked="***0052"),
        ])
        session.flush()
        for index, account_id in enumerate((51, 52), start=1):
            session.add(Action(
                id=f"parallel-action-{index}",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=account_id,
                scheduled_at=now,
                payload={
                    "group_id": 99,
                    "message_text": "",
                    "ai_generation_status": "pending",
                },
            ))
        session.commit()

    barrier = Barrier(2)

    def generate(session: Session, action: Action, _account: TgAccount) -> None:
        barrier.wait(timeout=3)
        action.payload = {
            **dict(action.payload or {}),
            "message_text": f"generated-{action.id}",
            "ai_generation_status": "ready",
        }
        session.commit()

    processed = drain_ai_generation(
        lambda: Session(engine),
        limit=2,
        generate_action=generate,
    )

    assert processed == 2
    with Session(engine) as session:
        assert session.query(GenerationJob).filter_by(state="ready").count() == 2
