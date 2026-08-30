from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import Tenant
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthorityHolder,
)
from app.services.task_center.group_mutation_authority import (
    check_and_claim_exclusive_authority,
    ensure_legacy_shared_holder,
    ensure_platform_writer_admission,
    release_platform_writer_admission,
    release_exclusive_authority,
    verify_gateway_admission,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    tenant = Tenant(id=1, name="Tenant 1")
    session.add(tenant)
    session.commit()

    yield session
    session.close()


def test_exclusive_authority_claim_and_collision(db_session: Session):
    # Task 1 成功申请独占
    claimed1, err1, auth1 = check_and_claim_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-1",
        route_hash="hash-1",
    )
    assert claimed1
    assert err1 == ""
    assert auth1 is not None
    assert auth1.mode == "exclusive_clone"

    # Task 2 尝试申请同一目标群独占，应被拒绝
    claimed2, err2, auth2 = check_and_claim_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-2",
        route_hash="hash-2",
    )
    assert not claimed2
    assert "已被其他克隆任务" in err2
    assert auth2 is None


def test_verify_gateway_admission(db_session: Session):
    check_and_claim_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-1",
        route_hash="hash-1",
    )

    # 1. 独占持有人拥有写权限
    allowed1, reason1 = verify_gateway_admission(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-1",
    )
    assert allowed1
    assert reason1 == ""

    # 2. 其他业务 (如 group_ai_chat) 尝试写入该群，被拦截
    allowed2, reason2 = verify_gateway_admission(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_ai_chat",
        writer_id="task-ai-99",
    )
    assert not allowed2
    assert "已被独占克隆锁定" in reason2


def test_release_authority(db_session: Session):
    check_and_claim_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-1",
        route_hash="hash-1",
    )

    # 释放权限
    released = release_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-1",
    )
    assert released

    # 释放后，Task 2 可以成功申请
    claimed2, _, _ = check_and_claim_exclusive_authority(
        session=db_session,
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="task-clone-2",
        route_hash="hash-2",
    )
    assert claimed2


def test_platform_writer_bootstraps_shared_but_cannot_bypass_clone(db_session: Session):
    allowed, reason = ensure_platform_writer_admission(
        db_session,
        1,
        target_peer_type="channel",
        target_peer_id="-100777",
        writer_kind="group_ai_chat",
        writer_id="ai-task-1",
    )
    assert allowed and reason == ""
    claimed, error, _authority = check_and_claim_exclusive_authority(
        db_session,
        1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_clone",
        writer_id="clone-task-1",
        route_hash="clone-route",
    )
    assert claimed and error == ""

    blocked, reason = ensure_platform_writer_admission(
        db_session,
        1,
        target_peer_type="channel",
        target_peer_id="-100888",
        writer_kind="group_ai_chat",
        writer_id="ai-task-2",
    )
    assert not blocked
    assert "独占克隆" in reason


def test_shared_one_shot_writer_can_release_and_reactivate(db_session: Session):
    for _index in range(2):
        allowed, reason = ensure_platform_writer_admission(
            db_session,
            1,
            target_peer_type="channel",
            target_peer_id="-100779",
            writer_kind="message_task",
            writer_id="message-1",
        )
        assert allowed and reason == ""
        release_platform_writer_admission(
            db_session,
            1,
            target_peer_type="channel",
            target_peer_id="-100779",
            writer_kind="message_task",
            writer_id="message-1",
        )
    holder = db_session.query(TelegramGroupMutationAuthorityHolder).filter_by(
        writer_kind="message_task", writer_id="message-1",
    ).one()
    assert holder.state == "released"


def test_handoff_admission_requires_exact_active_side_holder(db_session: Session):
    claimed, _reason, authority = check_and_claim_exclusive_authority(
        db_session,
        1,
        target_peer_type="channel",
        target_peer_id="-100780",
        writer_kind="group_clone",
        writer_id="clone-owner",
        route_hash="clone-owner-route",
    )
    assert claimed and authority is not None
    holder = db_session.query(TelegramGroupMutationAuthorityHolder).filter_by(
        authority_id=authority.id,
        writer_id="clone-owner",
    ).one()
    authority.mode = "handoff"
    authority.gateway_admission_side = "new"
    holder.holder_role = "new_handoff"
    db_session.flush()

    allowed, _reason = verify_gateway_admission(
        db_session, 1,
        target_peer_type="channel", target_peer_id="-100780",
        writer_kind="group_clone", writer_id="clone-owner",
    )
    blocked, reason = verify_gateway_admission(
        db_session, 1,
        target_peer_type="channel", target_peer_id="-100780",
        writer_kind="group_clone", writer_id="unrelated-clone",
    )
    assert allowed
    assert not blocked and "不属于当前写入侧" in reason
