from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Tenant
from app.services.task_center.config_normalization import normalize_operation_target_references


def test_group_ai_config_prefers_stable_duplicate_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=2149,
                tenant_id=1,
                target_type="group",
                tg_peer_id=" @qdsfxy",
                title="青岛师范学院",
                username="",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.add(
            OperationTarget(
                id=2761,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1003426646531",
                title="青岛师范学院",
                username="qdsfxy",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.commit()

        normalized = normalize_operation_target_references(
            session,
            1,
            "group_ai_chat",
            {"target_operation_target_id": 2149},
        )

    assert normalized["target_operation_target_id"] == 2761
    assert normalized["target_group_id"] > 0
    assert normalized["target_group_name"] == "青岛师范学院"


def test_group_ai_config_prefers_stable_duplicate_target_by_username() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=485,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1003583171851",
                title="天津",
                username="zzjinli",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.add(
            OperationTarget(
                id=1251,
                tenant_id=1,
                target_type="group",
                tg_peer_id="zzjinli",
                title="zzjinli",
                username="zzjinli",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        session.commit()

        normalized = normalize_operation_target_references(
            session,
            1,
            "group_ai_chat",
            {"target_operation_target_id": 1251},
        )

    assert normalized["target_operation_target_id"] == 485
    assert normalized["target_group_name"] == "天津"
