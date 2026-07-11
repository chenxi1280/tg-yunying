from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Thread
from time import sleep
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, engine
from app.models import AccountPool, Action, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.executors.search_rank_deboost_planner import _lock_task_for_planning
from app.services.task_center.search_rank_deboost_pacing import DeboostPacingStats, account_click_allowed, deboost_pacing_window
from app.services.task_center.search_rank_deboost_reservations import reserve_click


TEST_TENANT_ID = 990_001
TEST_POOL_ID = 9_900_010
TEST_ACCOUNT_ID = 99_000_100


def _seed_concurrency_case() -> tuple[str, int]:
    Base.metadata.create_all(engine)
    task_id = str(uuid4())
    with Session(engine) as session:
        session.add(Tenant(id=TEST_TENANT_ID, name="并发测试租户"))
        session.flush()
        session.add(AccountPool(id=TEST_POOL_ID, tenant_id=TEST_TENANT_ID, name="降权组", pool_purpose="rank_deboost", system_key="rank_deboost"))
        session.add(TgAccount(id=TEST_ACCOUNT_ID, tenant_id=TEST_TENANT_ID, pool_id=TEST_POOL_ID, account_identity="rank_deboost", display_name="降权账号", phone_masked=str(TEST_ACCOUNT_ID), status="在线"))
        session.add(
            Task(
                id=task_id,
                tenant_id=TEST_TENANT_ID,
                name="并发配额测试",
                type="search_rank_deboost",
                status="running",
                timezone="Asia/Shanghai",
                type_config={"per_account_daily_click_limit": 1, "per_account_cooldown_hours": 0},
                account_config={"selection_mode": "group", "account_group_id": TEST_POOL_ID},
            )
        )
        session.commit()
    return task_id, TEST_ACCOUNT_ID


def _reserve_first_click(session: Session, task: Task, account: TgAccount, now_value: datetime) -> None:
    action = Action(
        id=str(uuid4()),
        tenant_id=TEST_TENANT_ID,
        task_id=task.id,
        task_type=task.type,
        action_type="search_rank_deboost",
        account_id=account.id,
        status="pending",
        scheduled_at=now_value,
        payload={},
    )
    session.add(action)
    session.flush()
    reserve_click(
        session,
        task=task,
        action=action,
        account=account,
        account_pool_id=TEST_POOL_ID,
        keyword_hash="a" * 64,
        now_value=now_value,
    )


@dataclass
class _ConcurrencyState:
    task_id: str
    account_id: int
    now_value: datetime
    session_factory: object = field(default_factory=lambda: sessionmaker(bind=engine, future=True))
    first_locked: Event = field(default_factory=Event)
    release_first: Event = field(default_factory=Event)
    second_done: Event = field(default_factory=Event)
    errors: list[BaseException] = field(default_factory=list)
    outcomes: list[tuple[bool, str]] = field(default_factory=list)


def _first_planner(state: _ConcurrencyState) -> None:
    try:
        with state.session_factory() as session:
            task = session.get(Task, state.task_id)
            _lock_task_for_planning(session, task)
            state.first_locked.set()
            state.release_first.wait(timeout=5)
            _reserve_first_click(session, task, session.get(TgAccount, state.account_id), state.now_value)
            session.commit()
    except BaseException as exc:  # noqa: BLE001 - propagate thread failures to the test.
        state.errors.append(exc)


def _second_planner(state: _ConcurrencyState) -> None:
    try:
        state.first_locked.wait(timeout=5)
        with state.session_factory() as session:
            task = session.get(Task, state.task_id)
            account = session.get(TgAccount, state.account_id)
            _lock_task_for_planning(session, task)
            stats = DeboostPacingStats()
            allowed = account_click_allowed(
                session,
                task,
                account.id,
                "b" * 64,
                TEST_POOL_ID,
                deboost_pacing_window(task, state.now_value),
                stats,
            )
            state.outcomes.append((allowed, stats.last_limit_reason))
            session.commit()
    except BaseException as exc:  # noqa: BLE001 - propagate thread failures to the test.
        state.errors.append(exc)
    finally:
        state.second_done.set()


def test_task_planning_lock_serializes_click_quota_reservations() -> None:
    task_id, account_id = _seed_concurrency_case()
    state = _ConcurrencyState(task_id=task_id, account_id=account_id, now_value=_now())

    first = Thread(target=_first_planner, args=(state,))
    second = Thread(target=_second_planner, args=(state,))
    first.start()
    second.start()
    state.first_locked.wait(timeout=5)
    sleep(0.2)
    assert not state.second_done.is_set()
    state.release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert state.errors == []
    assert state.outcomes == [(False, "per_account_daily_click_limit_reached")]
