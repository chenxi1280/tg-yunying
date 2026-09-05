from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.models import Task, TenantAiSetting
from app.services.task_center.ai_generation_runtime_config import _bind_fact_first_provider
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres


def test_request_provider_binding_keeps_persisted_task_configuration_unchanged():
    with _session() as session:
        original = {"ai_content_route_v2_enabled": True, "ai_two_stage_enabled": True}
        task = Task(id="config-owner", tenant_id=1, name="原配置",
            type="group_ai_chat", status="running", type_config=original,
            config_revision=3, fulfillment_contract_version="fact_first_v3")
        setting = TenantAiSetting(tenant_id=1, default_provider_id=8)
        session.add_all([task, setting])
        session.commit()
        original = dict(task.type_config)
        statements = []
        event.listen(session.connection(), "before_cursor_execute",
            lambda _c, _cur, sql, _params, _ctx, _many: statements.append(sql.split()[0]))
        request = dict(original)

        _bind_fact_first_provider(session, task, request)
        session.flush()

        assert request["ai_provider_id"] == 8
        assert request["provider_binding_policy"] == "single_provider_key"
        assert task.type_config == original
        assert task.config_revision == 3
        assert not session.is_modified(task)
        assert "UPDATE" not in statements


def test_legacy_binding_keeps_original_request_contract():
    config = {"ai_provider_id": 4}
    task = SimpleNamespace(fulfillment_contract_version="legacy_v1")

    _bind_fact_first_provider(SimpleNamespace(), task, config)

    assert config == {"ai_provider_id": 4}
