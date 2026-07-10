from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routers.account_pools import router as account_pools_router
from app.auth import create_admin_access_token
from app.database import Base
from app.database import get_session
from app.models import (
    AccountGroupProxyBinding,
    AccountPool,
    Action,
    AuditLog,
    MessageTask,
    Task,
    TaskStatus,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
)
from app.permission_middleware import permission_middleware
from app.schemas import AccountPoolUpdate, TgAccountCreate
from app.security import encrypt_secret
from app.services.accounts import create_account
from app.services.account_pools import (
    create_rank_deboost_account_pool,
    delete_account_pool,
    ensure_rank_deboost_account_pool,
    move_account_pool,
    set_account_identity,
    update_account_pool,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(AccountPool(id=1, tenant_id=1, name="普通池", is_default=True))
        db.add(
            TelegramDeveloperApp(
                id=1,
                app_name="测试应用",
                api_id=12345,
                api_hash_ciphertext=encrypt_secret("hash"),
                health_status="健康",
            )
        )
        db.commit()
        yield db


@pytest.fixture
def http_client() -> tuple[TestClient, dict[str, str]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(AccountPool(id=1, tenant_id=1, name="普通池", is_default=True))
        db.commit()

    def override_session():
        with session_factory() as db:
            yield db

    app = FastAPI()
    app.middleware("http")(permission_middleware)
    app.include_router(account_pools_router)
    app.dependency_overrides[get_session] = override_session
    headers = {"Authorization": f"Bearer {create_admin_access_token()}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, headers


def _rank_pool(session: Session, name: str = "降权专用一组") -> AccountPool:
    return create_rank_deboost_account_pool(
        session,
        tenant_id=1,
        name=name,
        description="灰度账号",
        actor="tester",
    )


def _account(session: Session, pool: AccountPool, identity: str) -> TgAccount:
    account = TgAccount(
        tenant_id=1,
        pool_id=pool.id,
        display_name="迁移账号",
        phone_masked="1001",
        account_identity=identity,
    )
    session.add(account)
    session.commit()
    return account


def _action(account: TgAccount, action_id: str, action_type: str, status: str) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-lifecycle",
        task_type="group_ai_chat",
        action_type=action_type,
        account_id=account.id,
        status=status,
    )


def _message(account: TgAccount, task_id: int, status: str, *, preferred: bool = False) -> MessageTask:
    return MessageTask(
        id=task_id,
        tenant_id=1,
        account_id=None if preferred else account.id,
        preferred_account_id=account.id if preferred else None,
        content=f"message-{task_id}",
        status=status,
        idempotency_key=f"lifecycle-{task_id}",
    )


def test_custom_rank_pool_is_non_system_and_name_is_unique_per_tenant(session: Session) -> None:
    pool = _rank_pool(session)

    assert pool.pool_purpose == "rank_deboost"
    assert pool.is_system is False
    assert pool.system_key == ""
    with pytest.raises(ValueError, match="同租户.*名称"):
        _rank_pool(session)


def test_default_rank_pool_is_idempotent_and_distinct_from_custom_pool(session: Session) -> None:
    custom = _rank_pool(session)

    first = ensure_rank_deboost_account_pool(session, 1)
    second = ensure_rank_deboost_account_pool(session, 1)

    assert first.id == second.id
    assert first.id != custom.id
    assert first.is_system is True
    assert first.system_key == "rank_deboost"
    assert session.scalars(
        select(AccountPool).where(
            AccountPool.tenant_id == 1,
            AccountPool.system_key == "rank_deboost",
        )
    ).all() == [first]


def test_default_rank_pool_rejects_duplicate_system_rows(session: Session) -> None:
    session.add_all(
        [
            AccountPool(
                tenant_id=1,
                name="系统降权一",
                pool_purpose="rank_deboost",
                is_system=True,
                system_key="rank_deboost",
            ),
            AccountPool(
                tenant_id=1,
                name="系统降权二",
                pool_purpose="rank_deboost",
                is_system=True,
                system_key="rank_deboost",
            ),
        ]
    )
    session.commit()

    with pytest.raises(ValueError, match="最多一个.*系统默认组"):
        ensure_rank_deboost_account_pool(session, 1)


def test_rank_pool_http_body_creates_custom_pool(http_client) -> None:
    client, headers = http_client

    response = client.post(
        "/api/account-pools/rank-deboost",
        headers=headers,
        json={"tenant_id": 1, "name": "HTTP 自定义降权组", "description": "灰度组"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "HTTP 自定义降权组"
    assert response.json()["is_system"] is False
    assert response.json()["system_key"] == ""


def test_rank_pool_http_default_routes_are_idempotent(http_client) -> None:
    client, headers = http_client

    legacy_first = client.post("/api/account-pools/rank-deboost", headers=headers)
    legacy_second = client.post("/api/account-pools/rank-deboost", headers=headers)
    explicit = client.post("/api/account-pools/rank-deboost/default", headers=headers)

    assert legacy_first.status_code == 200
    assert legacy_first.json()["id"] == legacy_second.json()["id"] == explicit.json()["id"]
    assert explicit.json()["is_system"] is True
    assert explicit.json()["system_key"] == "rank_deboost"


def test_rank_pool_http_duplicate_create_and_rename_return_400(http_client) -> None:
    client, headers = http_client
    first = client.post("/api/account-pools/rank-deboost", headers=headers, json={"name": "重复名称"})
    second = client.post("/api/account-pools/rank-deboost", headers=headers, json={"name": "待重命名"})

    duplicate = client.post("/api/account-pools/rank-deboost", headers=headers, json={"name": "重复名称"})
    renamed = client.patch(
        f"/api/account-pools/{second.json()['id']}",
        headers=headers,
        json={"name": "重复名称"},
    )

    assert first.status_code == second.status_code == 200
    assert duplicate.status_code == 400
    assert renamed.status_code == 400


def test_rank_pool_http_requires_permission_dependency(http_client) -> None:
    client, _headers = http_client

    response = client.post("/api/account-pools/rank-deboost", json={"name": "未授权"})

    assert response.status_code == 401


def test_rank_pool_create_integrity_error_rolls_back(session: Session, monkeypatch) -> None:
    original_flush = session.flush

    def fail_pool_flush(*args, **kwargs) -> None:
        if any(isinstance(item, AccountPool) for item in session.new):
            raise IntegrityError("insert account_pools", {}, RuntimeError("duplicate"))
        original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_pool_flush)
    with pytest.raises(ValueError, match="同租户账号组名称必须唯一"):
        _rank_pool(session, "并发创建")

    assert session.in_transaction() is False


def test_rank_pool_update_integrity_error_rolls_back(session: Session, monkeypatch) -> None:
    pool = _rank_pool(session, "并发重命名")

    def fail_commit() -> None:
        raise IntegrityError("update account_pools", {}, RuntimeError("duplicate"))

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(ValueError, match="同租户账号组名称必须唯一"):
        update_account_pool(session, pool.id, AccountPoolUpdate(name="并发冲突"), "tester")

    assert session.in_transaction() is False


def test_rank_pool_can_be_disabled_without_changing_accounts(session: Session) -> None:
    pool = _rank_pool(session)
    account = _account(session, pool, "rank_deboost")

    updated = update_account_pool(
        session,
        pool.id,
        AccountPoolUpdate(is_enabled=False, disable_reason="人工暂停"),
        "tester",
    )

    assert updated["is_enabled"] is False
    assert updated["disable_reason"] == "人工暂停"
    assert session.get(TgAccount, account.id).account_identity == "rank_deboost"
    with pytest.raises(ValueError, match="专用分组不能设为默认"):
        update_account_pool(session, pool.id, AccountPoolUpdate(is_default=True), "tester")


def test_disabled_pool_reason_can_be_patched_independently(session: Session) -> None:
    pool = _rank_pool(session)
    update_account_pool(
        session,
        pool.id,
        AccountPoolUpdate(is_enabled=False, disable_reason="初始原因"),
        "tester",
    )

    updated = update_account_pool(
        session,
        pool.id,
        AccountPoolUpdate(disable_reason="复核后原因"),
        "reviewer",
    )

    assert updated["disable_reason"] == "复核后原因"


def test_move_rejects_disabled_target_pool(session: Session) -> None:
    rank_pool = _rank_pool(session)
    update_account_pool(session, rank_pool.id, AccountPoolUpdate(is_enabled=False), "tester")
    account = _account(session, session.get(AccountPool, 1), "normal")

    with pytest.raises(ValueError, match="account pool disabled"):
        move_account_pool(session, account.id, rank_pool.id, "tester")


def test_create_account_sets_identity_from_locked_enabled_pool(session: Session) -> None:
    rank_pool = _rank_pool(session)

    account = create_account(
        session,
        TgAccountCreate(
            tenant_id=1,
            pool_id=rank_pool.id,
            display_name="降权新账号",
            phone_masked="2001",
        ),
        "tester",
    )

    assert (account.pool_id, account.account_identity) == (rank_pool.id, "rank_deboost")


def test_create_account_rejects_disabled_or_invalid_purpose_pool(session: Session) -> None:
    disabled = _rank_pool(session)
    update_account_pool(session, disabled.id, AccountPoolUpdate(is_enabled=False), "tester")
    invalid = AccountPool(id=9, tenant_id=1, name="非法用途池", pool_purpose="unknown")
    session.add(invalid)
    session.commit()

    with pytest.raises(ValueError, match="account pool disabled"):
        create_account(
            session,
            TgAccountCreate(tenant_id=1, pool_id=disabled.id, display_name="禁用池账号", phone_masked="2002"),
            "tester",
        )
    with pytest.raises(ValueError, match="invalid account pool purpose"):
        create_account(
            session,
            TgAccountCreate(tenant_id=1, pool_id=invalid.id, display_name="非法池账号", phone_masked="2003"),
            "tester",
        )


def test_normal_to_rank_cancels_only_unstarted_ordinary_work(session: Session) -> None:
    normal_pool = session.get(AccountPool, 1)
    rank_pool = _rank_pool(session)
    account = _account(session, normal_pool, "normal")
    session.add(Task(id="task-lifecycle", tenant_id=1, name="生命周期", type="group_ai_chat"))
    session.add_all(
        [
            _action(account, "ordinary-pending", "send_message", "pending"),
            _action(account, "ordinary-claiming", "send_message", "claiming"),
            _action(account, "ordinary-retry", "send_message", "retryable_failed"),
            _action(account, "ordinary-executing", "send_message", "executing"),
            _action(account, "ordinary-success", "send_message", "success"),
            _action(account, "rank-pending", "search_rank_deboost", "pending"),
            _message(account, 1, TaskStatus.QUEUED.value),
            _message(account, 2, TaskStatus.PENDING_REVIEW.value, preferred=True),
            _message(account, 3, TaskStatus.DRAFT.value),
            _message(account, 4, TaskStatus.SENDING.value),
            _message(account, 5, TaskStatus.SENT.value),
        ]
    )
    session.commit()

    moved = move_account_pool(session, account.id, rank_pool.id, "tester")

    assert (moved.pool_id, moved.account_identity) == (rank_pool.id, "rank_deboost")
    assert session.get(Action, "ordinary-pending").status == "skipped"
    assert session.get(Action, "ordinary-claiming").status == "claiming"
    assert session.get(Action, "ordinary-retry").status == "skipped"
    assert session.get(Action, "ordinary-executing").status == "executing"
    assert session.get(Action, "ordinary-success").status == "success"
    assert session.get(Action, "rank-pending").status == "pending"
    assert [session.get(MessageTask, task_id).status for task_id in range(1, 6)] == [
        TaskStatus.CANCELLED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.SENDING.value,
        TaskStatus.SENT.value,
    ]
    assert session.get(MessageTask, 1).failure_type == "account_usage_migrated"
    detail = json.loads(session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first().detail)
    assert detail == {
        "previous_usage": "normal",
        "usage": "rank_deboost",
        "previous_pool_id": normal_pool.id,
        "target_pool_id": rank_pool.id,
        "cancelled_actions": 2,
        "cancelled_message_tasks": 3,
    }


def test_rank_to_normal_cancels_only_unstarted_rank_actions(session: Session) -> None:
    rank_pool = _rank_pool(session)
    account = _account(session, rank_pool, "rank_deboost")
    session.add(Task(id="task-lifecycle", tenant_id=1, name="生命周期", type="search_rank_deboost"))
    session.add_all(
        [
            _action(account, "rank-pending", "search_rank_deboost", "pending"),
            _action(account, "rank-claiming", "search_rank_deboost", "claiming"),
            _action(account, "rank-retry", "search_rank_deboost", "retryable_failed"),
            _action(account, "rank-executing", "search_rank_deboost", "executing"),
            _action(account, "ordinary-pending", "send_message", "pending"),
        ]
    )
    session.commit()

    moved = move_account_pool(session, account.id, 1, "tester")

    assert (moved.pool_id, moved.account_identity) == (1, "normal")
    assert session.get(Action, "rank-pending").status == "skipped"
    assert session.get(Action, "rank-claiming").status == "claiming"
    assert session.get(Action, "rank-retry").status == "skipped"
    assert session.get(Action, "rank-executing").status == "executing"
    assert session.get(Action, "ordinary-pending").status == "pending"


def test_set_identity_uses_enabled_pool_and_unified_rank_exit(session: Session) -> None:
    rank_pool = _rank_pool(session)
    account = _account(session, rank_pool, "rank_deboost")

    normal = set_account_identity(session, account.id, "normal", "tester")

    assert (normal.pool_id, normal.account_identity) == (1, "normal")
    with pytest.raises(ValueError, match="unsupported account identity"):
        set_account_identity(session, account.id, "rank_deboost", "tester")


def test_delete_normal_pool_rejects_active_binding_and_running_task_reference(session: Session) -> None:
    pool = AccountPool(id=2, tenant_id=1, name="待删普通池")
    session.add(pool)
    session.add(
        AccountGroupProxyBinding(
            tenant_id=1,
            account_pool_id=2,
            proxy_airport_node_id=9,
            status="active",
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="active 分组绑定"):
        delete_account_pool(session, pool.id, "tester")

    session.query(AccountGroupProxyBinding).delete()
    session.add(
        Task(
            id="task-running-pool",
            tenant_id=1,
            name="运行任务",
            type="group_ai_chat",
            status="running",
            account_config={"account_group_id": pool.id},
        )
    )
    session.commit()
    with pytest.raises(ValueError, match="running/paused 任务"):
        delete_account_pool(session, pool.id, "tester")
