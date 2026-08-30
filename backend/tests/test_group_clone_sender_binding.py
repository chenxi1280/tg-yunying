from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import Tenant, TgAccount, TgAccountAuthorization, Task
from app.models.group_clone import CloneAccountSlot, CloneSenderBindingHistory
from app.services.task_center.executors.group_clone import CloneSenderBindingManager


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    tenant = Tenant(id=1, name="Tenant 1")
    session.add(tenant)
    session.flush()

    for acc_id in [10, 20, 30]:
        acc = TgAccount(id=acc_id, tenant_id=1, display_name=f"Acc {acc_id}", phone_masked=f"+1000{acc_id}", status="online")
        session.add(acc)
    session.flush()

    authorizations = []
    for acc_id in [10, 20, 30]:
        authorization = TgAccountAuthorization(
            id=100 + acc_id,
            tenant_id=1,
            account_id=acc_id,
            is_current=True,
            status="active",
            telegram_user_id_digest=f"digest-{acc_id}",
        )
        authorizations.append(authorization)
        session.add(authorization)
    session.flush()

    task = Task(
        id="task-bind-1",
        tenant_id=1,
        name="Binding Test Task",
        type="group_clone",
        status="running",
        type_config={
            "sender_pool": {
                "account_ids": [10, 20, 30],
                "minimum_tenure_minutes": 1,
            }
        },
        task_lifecycle_epoch=1,
    )
    session.add(task)
    session.flush()
    for authorization in authorizations:
        session.add(CloneAccountSlot(
            task_id=task.id,
            account_id=authorization.account_id,
            authorization_id=authorization.id,
        ))
    session.commit()

    yield session
    session.close()


def test_basic_sender_allocation(db_session: Session):
    task = db_session.get(Task, "task-bind-1")
    binding, err = CloneSenderBindingManager.get_or_assign_sender_binding(
        session=db_session,
        task=task,
        source_sender_peer_type="user",
        source_sender_peer_id="user_alice",
        source_sender_name="Alice",
    )
    assert err == ""
    assert binding is not None
    assert binding.assigned_account_id in [10, 20, 30]

    # 二次获取同一发言人返回相同绑定
    binding2, err2 = CloneSenderBindingManager.get_or_assign_sender_binding(
        session=db_session,
        task=task,
        source_sender_peer_type="user",
        source_sender_peer_id="user_alice",
        source_sender_name="Alice",
    )
    assert err2 == ""
    assert binding2.id == binding.id
    assert binding2.assigned_account_id == binding.assigned_account_id


def test_gate_2_reply_self_collision_prevention(db_session: Session):
    task = db_session.get(Task, "task-bind-1")
    # Alice 分配到号 10
    b_alice, _ = CloneSenderBindingManager.get_or_assign_sender_binding(
        session=db_session,
        task=task,
        source_sender_peer_type="user",
        source_sender_peer_id="user_alice",
        source_sender_name="Alice",
    )
    # Bob 回复 Alice，分配账号必须排除 Alice 的账号 (号 10)
    b_bob, err = CloneSenderBindingManager.get_or_assign_sender_binding(
        session=db_session,
        task=task,
        source_sender_peer_type="user",
        source_sender_peer_id="user_bob",
        source_sender_name="Bob",
        reply_to_sender_peer_id="user_alice",
    )
    assert err == ""
    assert b_bob is not None
    assert b_bob.assigned_account_id != b_alice.assigned_account_id


def test_gate_3_cooldown_and_lru_reclaim(db_session: Session):
    task = db_session.get(Task, "task-bind-1")
    # 占满 3 个可用号
    b1, _ = _bind(db_session, task, "u1")
    b2, _ = _bind(db_session, task, "u2")
    b3, _ = _bind(db_session, task, "u3")

    # 第 4 个发言人进来，若所有人都未冷却，应拒绝
    b4, err = _bind(db_session, task, "u4")
    assert b4 is None
    assert "sender_pool_exhausted" in err

    # 只有 lifecycle 已进入 eligible 且达到最低 tenure 的绑定才可回收。
    b1.status = "eligible"
    b1.valid_from = datetime.now(timezone.utc) - timedelta(minutes=2)
    db_session.flush()

    # 现在 u4 应该能够成功 LRU 回收 u1 的账号
    b4, err = _bind(db_session, task, "u4")
    assert err == ""
    assert b4 is not None
    assert b4.assigned_account_id == b1.assigned_account_id
    assert b1.status == "expired"


def test_vip_pinning_protection(db_session: Session):
    task = db_session.get(Task, "task-bind-1")
    # u1 作为 VIP
    b1, _ = CloneSenderBindingManager.get_or_assign_sender_binding(
        db_session,
        task,
        source_sender_peer_type="user",
        source_sender_peer_id="u1_vip",
        source_sender_name="U1_VIP",
        is_vip=True,
    )
    b2, _ = _bind(db_session, task, "u2")
    b3, _ = _bind(db_session, task, "u3")

    # 将所有人的发言时间都设为过去（已过冷却）
    past = datetime.now(timezone.utc) - timedelta(minutes=2)
    for binding in (b1, b2, b3):
        binding.last_spoken_at = past
        binding.valid_from = past
        binding.status = "eligible"
    db_session.flush()

    # 分配 u4 时，必须跳过 VIP u1，只能回收普通账号 (u2 或 u3)
    b4, err = _bind(db_session, task, "u4")
    assert err == ""
    assert b4 is not None
    assert b4.assigned_account_id != b1.assigned_account_id
    assert b1.status == "eligible"


def _bind(session: Session, task: Task, sender_id: str):
    return CloneSenderBindingManager.get_or_assign_sender_binding(
        session,
        task,
        source_sender_peer_type="user",
        source_sender_peer_id=sender_id,
        source_sender_name=sender_id.upper(),
    )
