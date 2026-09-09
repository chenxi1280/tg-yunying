import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, Tenant, TenantAiSetting
from app.schemas.task_center import GroupAIChatTaskCreate, TaskSettingsUpdate
from app.services.task_center.service import create_group_ai_chat_task, update_task_settings


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("enabled", [False, True])
def test_task_emergency_setting_roundtrip_does_not_change_tenant_planned_fallback(enabled):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="test"))
        session.add(TenantAiSetting(tenant_id=1, ai_group_static_fallback_enabled=False))
        session.commit()
        task = create_group_ai_chat_task(session, 1, GroupAIChatTaskCreate(
            name="应急配置", target_group_id=7, topic_participation_rate=0.30,
        ), actor="test")
        assert task.type_config["emergency_fallback_enabled"] is True
        task_id = task.id
        request = TaskSettingsUpdate.model_validate({"emergency_fallback_enabled": enabled})
        update_task_settings(session, 1, task_id, request, actor="test")
        session.expire_all()
        assert session.get(Task, task_id).type_config["emergency_fallback_enabled"] is enabled
        assert session.query(TenantAiSetting).one().ai_group_static_fallback_enabled is False


def test_create_task_accepts_explicit_emergency_disable():
    request = GroupAIChatTaskCreate.model_validate({
        "name": "关闭应急", "target_group_id": 7, "topic_participation_rate": 0.30,
        "emergency_fallback_enabled": False,
    })
    assert request.model_dump()["emergency_fallback_enabled"] is False
