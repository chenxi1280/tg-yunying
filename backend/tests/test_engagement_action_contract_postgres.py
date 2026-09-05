from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models import AccountPool, Action, Task, Tenant
from app.services.task_center.engagement_action_contract import action_uses_unified_contract
from app.services.task_center.engagement_binding import freeze_initial_binding, validate_engagement_binding


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 950_801
POOL_ID = 950_802
BOUNDARY = datetime(2026, 9, 5, 7, tzinfo=timezone.utc)


def test_postgres_action_contract_boundary_survives_utc_readback_and_flag_change():
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "postgresql"
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        session.add(Tenant(id=TENANT_ID, name="合同归属测试"))
        session.flush()
        session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="普通组"))
        session.flush()
        config = {"engagement_contract_version": "unified_engagement_v1",
                  "account_selection_mode": "group", "account_group_ids": [POOL_ID]}
        task = Task(tenant_id=TENANT_ID, type="channel_like", name="合同边界", type_config=config)
        session.add(task)
        session.flush()
        spec = validate_engagement_binding(session, TENANT_ID, task.type, config)
        binding = freeze_initial_binding(session, task, spec)
        binding.effective_from = BOUNDARY
        before = Action(tenant_id=TENANT_ID, task_id=task.id, task_type=task.type,
            action_type="like_message", created_at=BOUNDARY - timedelta(seconds=1),
            status="unknown_after_send", result={"remote_outcome": "unknown"})
        after = Action(tenant_id=TENANT_ID, task_id=task.id, task_type=task.type,
            action_type="like_message", created_at=BOUNDARY)
        session.add_all([before, after])
        session.flush()
        session.expire_all()
        assert not action_uses_unified_contract(session, before)
        assert action_uses_unified_contract(session, after)
        task.type_config = {**config, "engagement_contract_version": "legacy_v0"}
        session.flush()
        session.expire_all()
        assert action_uses_unified_contract(session, after)
        assert (before.status, before.result) == ("unknown_after_send", {"remote_outcome": "unknown"})
        session.rollback()
