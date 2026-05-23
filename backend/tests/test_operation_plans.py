import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationPlanTaskLink, OperationTarget, Task, Tenant
from app.schemas.operation_plans import OperationPlanCreate, OperationPlanGenerateRequest, OperationPlanUpdate
from app.services.operation_plans import (
    apply_operation_plan_to_linked_tasks,
    archive_operation_plan,
    copy_operation_plan,
    create_operation_plan,
    generate_operation_plan_tasks,
    pause_operation_plan,
    preview_operation_plan,
    resume_operation_plan,
    update_operation_plan,
)


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_operation_plan_apply_requires_preview_then_confirmed_reason() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="群目标", can_send=True))
        session.commit()

        plan = create_operation_plan(
            session,
            1,
            OperationPlanCreate(
                name="群活跃方案",
                target_type="group",
                target_ids=[31],
                task_blueprints=[
                    {
                        "task_type": "group_ai_chat",
                        "name": "旧暖场",
                        "priority": 3,
                        "pacing_config": {"mode": "template", "template": "slow"},
                    }
                ],
            ),
            "tester",
        )
        generated = generate_operation_plan_tasks(
            session,
            1,
            plan["id"],
            OperationPlanGenerateRequest(auto_start=True, reason="创建联动任务"),
            "tester",
        )
        task_id = generated["created_task_ids"][0]
        task = session.get(Task, task_id)
        assert task.status == "running"

        update_operation_plan(
            session,
            1,
            plan["id"],
            OperationPlanUpdate(
                task_blueprints=[
                    {
                        "task_type": "group_ai_chat",
                        "name": "新暖场",
                        "priority": 5,
                        "pacing_config": {"mode": "template", "template": "moderate"},
                    }
                ]
            ),
            "tester",
        )
        preview = apply_operation_plan_to_linked_tasks(
            session,
            1,
            plan["id"],
            OperationPlanGenerateRequest(reason="先看影响"),
            "tester",
        )
        session.refresh(task)
        assert preview["requires_confirmation"] is True
        assert preview["applied_task_ids"] == []
        assert task.name == "旧暖场"

        with pytest.raises(ValueError, match="必须填写原因"):
            apply_operation_plan_to_linked_tasks(
                session,
                1,
                plan["id"],
                OperationPlanGenerateRequest(confirm_apply=True),
                "tester",
            )

        applied = apply_operation_plan_to_linked_tasks(
            session,
            1,
            plan["id"],
            OperationPlanGenerateRequest(confirm_apply=True, reason="确认同步方案配置"),
            "tester",
        )
        session.refresh(task)
        link = session.query(OperationPlanTaskLink).filter_by(task_id=task_id).one()

        assert applied["applied_task_ids"] == [task_id]
        assert task.name == "新暖场"
        assert task.priority == 5
        assert task.pacing_config["template"] == "moderate"
        assert link.status == "active"


def test_group_relay_operation_plan_requires_source_groups_before_generating_tasks() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=41, tenant_id=1, target_type="group", tg_peer_id="-10041", title="目标群", can_send=True))
        session.commit()

        plan = create_operation_plan(
            session,
            1,
            OperationPlanCreate(
                name="转发监听方案",
                target_type="group",
                target_ids=[41],
                task_blueprints=[{"task_type": "group_relay", "name": "转发监听"}],
            ),
            "tester",
        )

        preview = preview_operation_plan(session, 1, plan["id"], OperationPlanGenerateRequest(), "tester")
        generated = generate_operation_plan_tasks(session, 1, plan["id"], OperationPlanGenerateRequest(auto_start=True, reason="生成任务"), "tester")

        assert any("转发监听任务缺少来源群" in item for item in preview["blockers"])
        assert generated["created_task_ids"] == []
        assert session.query(Task).count() == 0


def test_operation_plan_lifecycle_resume_copy_and_archive() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=51, tenant_id=1, target_type="channel", tg_peer_id="-10051", title="频道目标", can_send=True))
        session.commit()

        plan = create_operation_plan(
            session,
            1,
            OperationPlanCreate(
                name="频道互动方案",
                target_type="channel",
                target_ids=[51],
                strategy_config={"cadence": "daily"},
                task_blueprints=[{"task_type": "channel_like", "name": "频道点赞"}],
            ),
            "tester",
        )
        generated = generate_operation_plan_tasks(
            session,
            1,
            plan["id"],
            OperationPlanGenerateRequest(auto_start=False, reason="创建草稿"),
            "tester",
        )
        assert generated["linked_task_count"] == 1

        paused = pause_operation_plan(session, 1, plan["id"], "tester")
        assert paused["status"] == "paused"
        resumed = resume_operation_plan(session, 1, plan["id"], "tester")
        assert resumed["status"] == "active"

        copied = copy_operation_plan(session, 1, plan["id"], "tester")
        assert copied["id"] != plan["id"]
        assert copied["name"] == "频道互动方案 副本"
        assert copied["status"] == "draft"
        assert [target.target_id for target in copied["targets"]] == [51]
        assert copied["task_blueprints"] == plan["task_blueprints"]
        assert copied["task_links"] == []

        archived = archive_operation_plan(session, 1, plan["id"], "tester")
        assert archived["status"] == "archived"

        with pytest.raises(ValueError, match="已归档"):
            preview_operation_plan(session, 1, plan["id"], OperationPlanGenerateRequest(), "tester")
        with pytest.raises(ValueError, match="已归档"):
            generate_operation_plan_tasks(session, 1, plan["id"], OperationPlanGenerateRequest(auto_start=True, reason="归档后生成"), "tester")
        with pytest.raises(ValueError, match="已归档"):
            apply_operation_plan_to_linked_tasks(session, 1, plan["id"], OperationPlanGenerateRequest(reason="归档后调整"), "tester")
