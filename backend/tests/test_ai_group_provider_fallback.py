from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant, TenantAiSetting
from app.schemas.ai_config import TenantAiSettingUpdate
from app.services.ai_config import update_tenant_ai_setting


pytestmark = pytest.mark.no_postgres


def test_tenant_ai_group_fallback_switches_default_enabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        setting = TenantAiSetting(tenant_id=1)
        session.add(setting)
        session.commit()
        session.refresh(setting)

        assert setting.ai_group_model_fallback_enabled is True
        assert setting.ai_group_grok_fallback_enabled is True
        assert setting.ai_group_static_fallback_enabled is True


def test_tenant_ai_group_fallback_switches_can_be_disabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
        session.commit()

        updated = update_tenant_ai_setting(
            session,
            1,
            TenantAiSettingUpdate(
                ai_group_model_fallback_enabled=False,
                ai_group_grok_fallback_enabled=False,
                ai_group_static_fallback_enabled=False,
            ),
            "pytest",
        )

        assert updated.ai_group_model_fallback_enabled is False
        assert updated.ai_group_grok_fallback_enabled is False
        assert updated.ai_group_static_fallback_enabled is False
