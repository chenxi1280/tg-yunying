"""search_rank_deboost 任务账号组隔离测试（Task 15）。

覆盖 spec「Requirement: 账号组隔离」全部 scenario：
- 分组创建/禁用/删除逻辑
- 同账号不得同时存在于 rank_deboost 分组和普通分组
- 任务候选池硬过滤 rank_deboost 分组账号
- search_rank_deboost 任务只能使用 rank_deboost 分组（简化回归断言）

参考现有 test_search_join_group_config.py / test_task_account_pool.py 的 fixture 风格：
SQLite 内存数据库 + 真实模型。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, AccountStatus, Tenant, TgAccount
from app.services.account_pools import (
    RANK_DEBOOST_POOL_KEY,
    assert_account_not_in_rank_deboost_conflict,
    create_rank_deboost_account_pool,
    delete_account_pool,
    ensure_rank_deboost_account_pool,
    move_account_pool,
)
from app.services.task_center.account_pool import select_task_accounts


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(AccountPool(id=1, tenant_id=1, name="普通分组", is_default=True, pool_purpose="normal"))
        db.commit()
        yield db


# --- SubTask 15.1: 分组创建/禁用逻辑 ---


def test_create_rank_deboost_pool_succeeds(session: Session) -> None:
    """Scenario: 创建 rank_deboost 分组成功。

    调用 create_rank_deboost_account_pool 应返回 pool_purpose='rank_deboost' 的分组，
    持久化到 account_pools 表。
    """
    pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 A",
        description="测试降权分组",
        actor="tester",
    )
    assert pool.id is not None
    assert pool.pool_purpose == RANK_DEBOOST_POOL_KEY
    assert pool.tenant_id == 1
    assert pool.name == "降权专用 A"
    assert pool.is_system is False
    assert pool.system_key == ""

    refreshed = session.get(AccountPool, pool.id)
    assert refreshed is not None
    assert refreshed.pool_purpose == RANK_DEBOOST_POOL_KEY


def test_ensure_rank_deboost_account_pool_creates_system_pool_once(session: Session) -> None:
    """Scenario: ensure_rank_deboost_account_pool 创建系统级降权分组且幂等。"""
    first = ensure_rank_deboost_account_pool(session, 1)
    session.flush()
    second = ensure_rank_deboost_account_pool(session, 1)
    assert first.id == second.id
    assert first.pool_purpose == RANK_DEBOOST_POOL_KEY
    assert first.is_system is True
    assert first.system_key == RANK_DEBOOST_POOL_KEY


def test_rank_deboost_pool_cannot_be_deleted(session: Session) -> None:
    """Scenario: 删除 rank_deboost 分组应失败（不可删除，只能禁用）。

    覆盖 spec：该类型分组不可删除，只能禁用（is_default=False，软删除标记或 is_system=True）。
    """
    pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 B",
        actor="tester",
    )
    with pytest.raises(ValueError, match="rank_deboost 分组不可删除"):
        delete_account_pool(session, pool.id, "tester")

    # 系统级降权分组同样不可删除
    system_pool = ensure_rank_deboost_account_pool(session, 1)
    session.flush()
    with pytest.raises(ValueError, match="rank_deboost 分组不可删除"):
        delete_account_pool(session, system_pool.id, "tester")


def test_normal_pool_can_be_deleted_when_empty(session: Session) -> None:
    """对照测试：普通空分组可删除（验证 delete_account_pool 不会误拦截普通分组）。"""
    session.add(AccountPool(id=50, tenant_id=1, name="临时普通分组", pool_purpose="normal"))
    session.commit()
    delete_account_pool(session, 50, "tester")
    assert session.get(AccountPool, 50) is None


# --- SubTask 15.1: 同账号不得同时存在于 rank_deboost 和普通分组 ---


def test_account_cannot_be_in_both_rank_deboost_and_normal_pool(session: Session) -> None:
    """Scenario: 同账号同时存在于 rank_deboost 和普通分组应失败（数据一致性校验）。

    直接构造不一致状态：account_identity='rank_deboost' 但 pool_id 指向普通分组。
    assert_account_not_in_rank_deboost_conflict 应 raise ValueError
    「rank_deboost 分组内账号不得同时存在于普通分组」。
    """
    normal_pool = session.get(AccountPool, 1)
    account = TgAccount(
        id=100,
        tenant_id=1,
        pool_id=normal_pool.id,
        display_name="异常账号",
        phone_masked="100",
        status=AccountStatus.ACTIVE.value,
        account_identity=RANK_DEBOOST_POOL_KEY,  # 故意构造不一致状态
    )
    session.add(account)
    session.commit()

    with pytest.raises(ValueError, match="rank_deboost 分组内账号不得同时存在于普通分组"):
        assert_account_not_in_rank_deboost_conflict(session, account)

    # 反向不一致：在 rank_deboost 分组但 account_identity='normal' 也应失败
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 C",
        actor="tester",
    )
    account2 = TgAccount(
        id=101,
        tenant_id=1,
        pool_id=rank_deboost_pool.id,
        display_name="异常账号2",
        phone_masked="101",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",  # 故意构造不一致状态
    )
    session.add(account2)
    session.commit()
    with pytest.raises(ValueError, match="rank_deboost 分组内账号不得同时存在于普通分组"):
        assert_account_not_in_rank_deboost_conflict(session, account2)


def test_consistent_rank_deboost_account_passes_validation(session: Session) -> None:
    """对照测试：account_identity='rank_deboost' 且 pool_id 指向 rank_deboost 分组 → 校验通过。"""
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 D",
        actor="tester",
    )
    account = TgAccount(
        id=102,
        tenant_id=1,
        pool_id=rank_deboost_pool.id,
        display_name="一致账号",
        phone_masked="102",
        status=AccountStatus.ACTIVE.value,
        account_identity=RANK_DEBOOST_POOL_KEY,
    )
    session.add(account)
    session.commit()
    # 不应抛出
    assert_account_not_in_rank_deboost_conflict(session, account)


# --- SubTask 15.1: 移动账号隔离硬校验 ---


def test_move_account_to_rank_deboost_pool_syncs_usage(session: Session) -> None:
    normal_pool = session.get(AccountPool, 1)
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 E",
        actor="tester",
    )
    account = TgAccount(
        id=200,
        tenant_id=1,
        pool_id=normal_pool.id,
        display_name="普通账号",
        phone_masked="200",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",
    )
    session.add(account)
    session.commit()

    moved = move_account_pool(session, account.id, rank_deboost_pool.id, "tester")
    assert moved.pool_id == rank_deboost_pool.id
    assert moved.account_identity == RANK_DEBOOST_POOL_KEY


def test_move_account_to_normal_pool_syncs_usage(session: Session) -> None:
    normal_pool = session.get(AccountPool, 1)
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 F",
        actor="tester",
    )
    account = TgAccount(
        id=201,
        tenant_id=1,
        pool_id=rank_deboost_pool.id,
        display_name="降权账号",
        phone_masked="201",
        status=AccountStatus.ACTIVE.value,
        account_identity=RANK_DEBOOST_POOL_KEY,
    )
    session.add(account)
    session.commit()

    moved = move_account_pool(session, account.id, normal_pool.id, "tester")
    assert moved.pool_id == normal_pool.id
    assert moved.account_identity == "normal"


def test_move_ungrouped_account_to_rank_deboost_pool_succeeds(session: Session) -> None:
    """对照测试：未分组账号（无 pool_id）可移入 rank_deboost 分组。

    未分组账号不在普通分组，不触发隔离硬校验。这是新账号进入降权分组的标准路径。
    """
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 G",
        actor="tester",
    )
    account = TgAccount(
        id=202,
        tenant_id=1,
        pool_id=None,
        display_name="未分组账号",
        phone_masked="202",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",
    )
    session.add(account)
    session.commit()

    moved = move_account_pool(session, account.id, rank_deboost_pool.id, "tester")
    assert moved.pool_id == rank_deboost_pool.id
    assert moved.account_identity == RANK_DEBOOST_POOL_KEY


def test_move_account_between_rank_deboost_pools_succeeds(session: Session) -> None:
    """对照测试：rank_deboost 分组之间互移应成功（不触发普通分组隔离）。"""
    pool_a = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 H1",
        actor="tester",
    )
    pool_b = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 H2",
        actor="tester",
    )
    account = TgAccount(
        id=203,
        tenant_id=1,
        pool_id=pool_a.id,
        display_name="降权账号",
        phone_masked="203",
        status=AccountStatus.ACTIVE.value,
        account_identity=RANK_DEBOOST_POOL_KEY,
    )
    session.add(account)
    session.commit()

    moved = move_account_pool(session, account.id, pool_b.id, "tester")
    assert moved.pool_id == pool_b.id
    assert moved.account_identity == RANK_DEBOOST_POOL_KEY


# --- SubTask 15.2: 任务候选池硬过滤 ---


def test_other_tasks_exclude_rank_deboost_accounts(session: Session) -> None:
    """Scenario: group_ai_chat 等任务候选池筛选时排除 rank_deboost 分组账号。

    覆盖 spec：任务候选池筛选时必须硬过滤 pool_purpose='rank_deboost' 分组内账号，
    避免被其他任务通过「全部可用账号」语义误选。

    构造：1 个普通账号 + 1 个 rank_deboost 账号，selection_mode='all'，
    select_task_accounts 应只返回普通账号。
    """
    normal_account = TgAccount(
        id=300,
        tenant_id=1,
        pool_id=1,
        display_name="普通账号",
        phone_masked="300",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",
        health_score=90,
    )
    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 I",
        actor="tester",
    )
    rank_deboost_account = TgAccount(
        id=301,
        tenant_id=1,
        pool_id=rank_deboost_pool.id,
        display_name="降权账号",
        phone_masked="301",
        status=AccountStatus.ACTIVE.value,
        account_identity=RANK_DEBOOST_POOL_KEY,
        health_score=95,  # 健康分更高，验证不会被误选
    )
    session.add_all([normal_account, rank_deboost_account])
    session.commit()

    # selection_mode='all' 模拟「全部可用账号」语义
    selected = select_task_accounts(
        session,
        1,
        {"max_concurrent": 10, "selection_mode": "all"},
        limit=10,
        enforce_capacity=False,
    )
    selected_ids = {account.id for account in selected}

    # 普通账号应被选中，rank_deboost 账号应被排除
    assert 300 in selected_ids
    assert 301 not in selected_ids


def test_select_task_accounts_still_excludes_code_receiver(session: Session) -> None:
    """回归断言：code_receiver 接码专用分组隔离逻辑不被破坏。

    新增 rank_deboost 过滤后，code_receiver 账号仍应被排除。
    """
    code_receiver_pool = AccountPool(
        id=80,
        tenant_id=1,
        name="接码专用",
        pool_purpose="code_receiver",
        is_system=True,
        system_key="code_receiver",
    )
    normal_account = TgAccount(
        id=302,
        tenant_id=1,
        pool_id=1,
        display_name="普通账号",
        phone_masked="302",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",
        health_score=90,
    )
    code_receiver_account = TgAccount(
        id=303,
        tenant_id=1,
        pool_id=80,
        display_name="接码账号",
        phone_masked="303",
        status=AccountStatus.ACTIVE.value,
        account_identity="code_receiver",
        health_score=95,
    )
    session.add_all([code_receiver_pool, normal_account, code_receiver_account])
    session.commit()

    selected = select_task_accounts(
        session,
        1,
        {"max_concurrent": 10, "selection_mode": "all"},
        limit=10,
        enforce_capacity=False,
    )
    selected_ids = {account.id for account in selected}
    assert 302 in selected_ids
    assert 303 not in selected_ids


# --- SubTask 15.3: search_rank_deboost 任务只能使用 rank_deboost 分组（简化回归） ---


def test_search_rank_deboost_task_only_uses_rank_deboost_pool(session: Session) -> None:
    """Scenario: search_rank_deboost 任务只能使用 rank_deboost 分组（简化回归）。

    Task 10 已覆盖 create_search_rank_deboost_task 拒绝非 rank_deboost 分组的完整流程。
    这里仅做简化回归：assert_account_pool_for_rank_deboost 校验函数对普通分组应 raise，
    对 rank_deboost 分组应通过。
    """
    from app.services.task_center.search_rank_deboost import (
        assert_account_pool_for_rank_deboost,
    )

    normal_pool = session.get(AccountPool, 1)
    with pytest.raises(ValueError, match="必须为 rank_deboost"):
        assert_account_pool_for_rank_deboost(
            session,
            tenant_id=1,
            account_pool_id=normal_pool.id,
        )

    rank_deboost_pool = create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name="降权专用 J",
        actor="tester",
    )
    # 不应抛出
    result = assert_account_pool_for_rank_deboost(
        session,
        tenant_id=1,
        account_pool_id=rank_deboost_pool.id,
    )
    assert result.id == rank_deboost_pool.id
    assert result.pool_purpose == RANK_DEBOOST_POOL_KEY
