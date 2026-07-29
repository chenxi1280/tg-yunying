from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    DispatchAllocationExclusion,
    OperationTarget,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    SearchClickSolverCarrierUnitBinding,
    SearchClickSolverProblemSnapshot,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    WorkerHeartbeat,
)
from app.security import encrypt_secret
from app.services._common import _now
from app.services.task_center.executors import search_click
from app.services.task_center.heartbeat import worker_identity
from app.timezone import BEIJING_TZ
from app.services.task_center.executors.search_click_candidates import (
    SearchClickPathContext,
)
from app.services.task_center.executors.search_join_group import (
    PayloadInput,
    SearchJoinPlan,
)
from app.services.task_center.search_click_assignment_solver import (
    SearchClickCandidatePath,
)
from app.services.task_center.search_click_dispatch_allocation import (
    SearchClickFulfillmentUnit,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed(current)
        yield current


def test_epoch_persists_snapshot_before_binding_action(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = SearchClickFulfillmentUnit(
        "obligation-1",
        "task-1",
        "reservation-1",
        "window-1",
        1,
        1,
    )
    context = _path_context(session)
    monkeypatch.setattr(
        search_click,
        "prepare_search_click_fulfillment_units",
        lambda *_args, **_kwargs: (unit,),
    )
    monkeypatch.setattr(
        search_click,
        "candidate_paths",
        lambda *_args, **_kwargs: {context.candidate.key: context},
    )

    created = search_click.build_plan(session, session.get(Task, "task-1"))
    session.commit()

    epoch = session.scalar(select(SearchClickAssignmentEpoch))
    assignment = session.scalar(select(SearchClickOpportunityAssignment))
    binding = session.scalar(select(SearchClickSolverCarrierUnitBinding))
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    assert created == 1
    assert epoch.finalize_status == "finalized"
    assert epoch.outcome == "optimal"
    assert binding.obligation_id == "obligation-1"
    assert assignment.state == "action_bound"
    assert assignment.action_id is not None
    assert reservation.bound_count == 1


def test_each_matched_ordinal_gets_distinct_bound_action(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = session.get(TaskDayLedger, "ledger-1")
    session.add(SearchClickFulfillmentObligation(
        id="obligation-2",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=1,
        click_obligation_ordinal=2,
    ))
    window = session.get(DispatchClaimWindow, "window-1")
    allocation = session.get(DispatchClaimShardAllocation, "allocation-1")
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    window.claim_capacity = 2
    window.unclaimed_allocated_count = 2
    allocation.required_claims = 2
    allocation.unclaimed_allocated_count = 2
    reservation.required_claims = 2
    reservation.reserved_claims = 2
    session.commit()
    units = tuple(
        SearchClickFulfillmentUnit(
            obligation_id=f"obligation-{ordinal}",
            task_id="task-1",
            reservation_id="reservation-1",
            window_id="window-1",
            dispatch_allocation_epoch=1,
            fulfillment_lane_claim_ordinal=ordinal,
        )
        for ordinal in (1, 2)
    )
    path = _path_context(session)
    candidate = replace(
        path.candidate,
        hard_safe_remaining_capacity=2,
        eligible_obligation_ids=("obligation-1", "obligation-2"),
    )
    path = replace(path, candidate=candidate)
    monkeypatch.setattr(
        search_click,
        "prepare_search_click_fulfillment_units",
        lambda *_args, **_kwargs: units,
    )
    monkeypatch.setattr(
        search_click,
        "candidate_paths",
        lambda *_args, **_kwargs: {candidate.key: path},
    )

    assert search_click.build_plan(session, session.get(Task, "task-1")) == 2
    session.commit()

    assignments = list(session.scalars(select(SearchClickOpportunityAssignment)))
    actions = list(session.scalars(select(Action)))
    assert len(assignments) == 2
    assert len(actions) == 2
    assert len({row.action_id for row in assignments}) == 2


def test_build_plan_finalizes_orphaned_open_epoch_before_current_window(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_value = _now()
    window = session.get(DispatchClaimWindow, "window-1")
    window.bucket_end = now_value.replace(tzinfo=BEIJING_TZ) - timedelta(minutes=1)
    epoch = _add_orphaned_epoch(session, now_value)
    session.commit()
    monkeypatch.setattr(
        search_click,
        "prepare_search_click_fulfillment_units",
        lambda *_args, **_kwargs: (),
    )

    created = search_click.build_plan(session, session.get(Task, "task-1"))
    session.commit()

    session.refresh(epoch)
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    exclusion = session.scalar(select(DispatchAllocationExclusion))
    assert created == 0
    assert epoch.finalize_status == "finalized"
    assert epoch.outcome == "abandoned"
    assert epoch.finalized_at is not None
    assert reservation.released_count == 1
    assert exclusion.carrier_id == epoch.id


def _add_orphaned_epoch(
    session: Session,
    now_value,
) -> SearchClickAssignmentEpoch:
    session.add(WorkerHeartbeat(
        id="worker-1",
        worker_id="old-host:1:search_click_solver",
        process_type="search_click_solver",
        hostname="old-host",
        pid=1,
        heartbeat_metadata={"search_solver_fencing_token": "new-token"},
        last_seen_at=now_value,
    ))
    epoch = SearchClickAssignmentEpoch(
        id="epoch-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        solver_owner_lease_id="worker-1",
        solver_fencing_token="old-token",
        solver_claimed_at=now_value,
        solver_problem_hash="a" * 64,
        solver_input_hash="b" * 64,
    )
    session.add(epoch)
    snapshot = SearchClickSolverProblemSnapshot(
        id="snapshot-1",
        search_click_assignment_epoch_id=epoch.id,
        solver_contract_version="v1",
        canonical_problem_payload={},
        canonical_carrier_payload={},
        solver_problem_hash=epoch.solver_problem_hash,
        solver_input_hash=epoch.solver_input_hash,
    )
    session.add(snapshot)
    session.add(SearchClickSolverCarrierUnitBinding(
        search_click_solver_snapshot_id=snapshot.id,
        dispatch_claim_reservation_id="reservation-1",
        fulfillment_lane_claim_ordinal=1,
        obligation_id="obligation-1",
        task_id="task-1",
        stable_component_key="component-1",
        solver_problem_component_hash="c" * 64,
        canonical_binding_payload={},
    ))
    return epoch


def test_build_plan_leaves_active_open_epoch_owned(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_value = _now()
    worker_id, hostname, pid = worker_identity("search_click_solver")
    session.add(WorkerHeartbeat(
        id="worker-1",
        worker_id=worker_id,
        process_type="search_click_solver",
        hostname=hostname,
        pid=pid,
        heartbeat_metadata={"search_solver_fencing_token": "token-1"},
        last_seen_at=now_value,
    ))
    epoch = SearchClickAssignmentEpoch(
        id="epoch-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        solver_owner_lease_id="worker-1",
        solver_fencing_token="token-1",
        solver_claimed_at=now_value,
        solver_problem_hash="a" * 64,
        solver_input_hash="b" * 64,
    )
    session.add(epoch)
    session.commit()
    monkeypatch.setattr(
        search_click,
        "prepare_search_click_fulfillment_units",
        lambda *_args, **_kwargs: (),
    )

    assert search_click.build_plan(session, session.get(Task, "task-1")) == 0

    assert epoch.finalize_status == "open"
    assert epoch.outcome == "open"
    assert session.scalar(select(DispatchAllocationExclusion)) is None


def test_finalize_locks_allocation_before_owner_and_snapshot_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    context = SimpleNamespace(
        epoch=SimpleNamespace(dispatch_claim_window_id="window-1"),
        now=_now(),
        units=(),
    )
    monkeypatch.setattr(
        search_click,
        "_restart_serializable_finalize_transaction",
        lambda _session: events.append("restart"),
    )
    monkeypatch.setattr(
        search_click,
        "lock_search_finalize_inputs",
        lambda *_args: events.append("lock") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        search_click,
        "solver_owner_is_active",
        lambda *_args: events.append("owner") or False,
    )
    monkeypatch.setattr(
        search_click,
        "_abandon_epoch",
        lambda *_args: events.append("abandon") or {},
    )

    assert search_click._finalize_epoch(SimpleNamespace(), context) == {}
    assert events == ["restart", "lock", "owner", "abandon"]


def test_serialization_failure_abandons_epoch_without_rerunning_solver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    failure = OperationalError("commit", {}, _SerializationFailure())
    session = _CommitFailureSession(failure, events)
    context = SimpleNamespace(
        epoch=SimpleNamespace(
            id="epoch-1",
            dispatch_claim_window_id="window-1",
        ),
        units=(),
        now=_now(),
    )
    monkeypatch.setattr(
        search_click,
        "_finalize_epoch",
        lambda *_args: events.append("finalize") or {"task-1": 1},
    )
    monkeypatch.setattr(
        search_click,
        "_abandon_serialization_failed_epoch",
        lambda *_args, **_kwargs: events.append("abandon"),
    )

    assert search_click._commit_finalize_or_abandon(session, context) == {}
    assert events == ["finalize", "commit", "rollback", "abandon", "commit"]


def test_serialization_failure_during_finalize_also_abandons_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = _CommitFailureSession(None, events)
    context = SimpleNamespace(
        epoch=SimpleNamespace(
            id="epoch-1",
            dispatch_claim_window_id="window-1",
        ),
        units=(),
        now=_now(),
    )
    failure = OperationalError("lock", {}, _SerializationFailure())
    monkeypatch.setattr(
        search_click,
        "_finalize_epoch",
        lambda *_args: events.append("finalize") or (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        search_click,
        "_abandon_serialization_failed_epoch",
        lambda *_args, **_kwargs: events.append("abandon"),
    )

    assert search_click._commit_finalize_or_abandon(session, context) == {}
    assert events == ["finalize", "rollback", "abandon", "commit"]


def test_non_serialization_database_error_is_not_silently_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    failure = OperationalError("commit", {}, _NonSerializationFailure())
    session = _CommitFailureSession(failure, events)
    context = SimpleNamespace(
        epoch=SimpleNamespace(
            id="epoch-1",
            dispatch_claim_window_id="window-1",
        ),
        units=(),
        now=_now(),
    )
    monkeypatch.setattr(
        search_click,
        "_finalize_epoch",
        lambda *_args: events.append("finalize") or {},
    )

    with pytest.raises(OperationalError):
        search_click._commit_finalize_or_abandon(session, context)
    assert events == ["finalize", "commit"]


class _SerializationFailure(Exception):
    sqlstate = "40001"


class _NonSerializationFailure(Exception):
    sqlstate = "23505"


class _CommitFailureSession:
    def __init__(self, failure, events) -> None:
        self.failure = failure
        self.events = events

    def commit(self) -> None:
        self.events.append("commit")
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure

    def rollback(self) -> None:
        self.events.append("rollback")


def _seed(session: Session) -> None:
    now_value = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="1",
        status="在线",
    ))
    target = OperationTarget(
        id=1,
        tenant_id=1,
        target_type="group",
        tg_peer_id="target",
        username="target_group",
        title="target",
    )
    session.add(target)
    task = Task(
        id="task-1",
        tenant_id=1,
        name="click",
        type="search_click",
        status="running",
        type_config={
            "search_execution_mode": "click_only",
            "target_operation_target_id": 1,
            "keyword_hashes": ["a" * 64],
            "keyword_text_ciphertexts": [encrypt_secret("keyword")],
        },
    )
    session.add(task)
    ledger = TaskDayLedger(
        id="ledger-1",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=now_value.date(),
        period_start_at=now_value,
        deadline_at=now_value + timedelta(days=1),
        day_phase="full_day",
        planning_anchor_at=now_value,
    )
    session.add(ledger)
    session.add(SearchClickFulfillmentObligation(
        id="obligation-1",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        target_id=target.id,
        click_obligation_ordinal=1,
    ))
    _seed_dispatch(session, now_value)
    session.commit()


def _seed_dispatch(session: Session, now_value) -> None:
    session.add(DispatchClaimScope(
        id="scope-1",
        dispatcher_scope="task_center_dispatch",
        claim_capacity=1,
    ))
    session.add(DispatchClaimWindow(
        id="window-1",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now_value - timedelta(seconds=1),
        bucket_end=now_value + timedelta(minutes=1),
        claim_capacity=1,
        unclaimed_allocated_count=1,
        allocation_epoch=1,
        allocation_state="ready",
    ))
    session.add(DispatchClaimShardAllocation(
        id="allocation-1",
        dispatch_claim_window_id="window-1",
        dispatch_allocation_epoch=1,
        required_claims=1,
        unclaimed_allocated_count=1,
    ))
    session.add(DispatchClaimReservation(
        id="reservation-1",
        dispatch_claim_shard_allocation_id="allocation-1",
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task-1",
        claim_class="search_join",
        bucket_start=now_value,
        required_claims=1,
        reserved_claims=1,
    ))


def _path_context(session: Session) -> SearchClickPathContext:
    task = session.get(Task, "task-1")
    target = session.get(OperationTarget, 1)
    candidate = SearchClickCandidatePath(
        key="path-1",
        account_id=1,
        authorization_id=1,
        keyword_hash="a" * 64,
        proxy_route_id="proxy-1",
        protocol_sample_version="v1",
        hard_safe_remaining_capacity=1,
        confirmed_click_count_today=0,
        last_click_opportunity_at=None,
        persistent_account_cursor=1,
        eligible_obligation_ids=("obligation-1",),
    )
    environment = SimpleNamespace(
        authorization_id=1,
        session_role="primary",
        client_metadata={
            "device_model": "iPhone",
            "system_version": "iOS 17",
            "app_version": "10.0",
            "platform": "ios",
            "client_identity_key": "identity-1",
        },
        developer_app_id=1,
        developer_app_api_id=100,
        proxy_id=1,
        proxy_name="proxy",
        proxy_binding_id=1,
        binding_id="binding-1",
    )
    plan = SearchJoinPlan(
        "jisou",
        candidate.keyword_hash,
        target,
        {},
        "v1",
        _protocol_profile(),
    )
    return SearchClickPathContext(
        candidate,
        PayloadInput(task.type_config, plan, candidate.keyword_hash, session.get(TgAccount, 1), environment),
    )


def _protocol_profile() -> dict:
    return {
        "page_fingerprints": [
            {"page_phase": "verification_page", "text_enums": ["human_verification"]},
            {"page_phase": "hot_list_page", "text_enums": ["hot_list"]},
            {"page_phase": "search_category_page", "button_text_enums_any": ["jisou_group_category"]},
            {
                "page_phase": "group_result_page",
                "button_effects_any": ["navigate_only", "target_open_only"],
                "membership_side_effects_allowed": ["none"],
            },
        ]
    }
