from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiProvider, AiProviderHealthStatus
from app.services.task_center.ai_provider_candidate_runtime import (
    is_ai_provider_quota_exhausted,
    quota_rotation_providers,
    provider_candidates,
)
from app.services.task_center.ai_generation_stage_config import fallback_stages


pytestmark = pytest.mark.no_postgres


def test_ai_provider_quota_exhausted_recognizes_token_plan_limit():
    error_1 = Exception('AI provider HTTP 429: {"type":"error","error":{"type":"rate_limit_error","message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)","http_code":"429"}}')
    assert is_ai_provider_quota_exhausted(error_1) is True

    error_2 = Exception("token plan quota exhausted")
    assert is_ai_provider_quota_exhausted(error_2) is True

    error_3 = Exception("购买积分补充用量")
    assert is_ai_provider_quota_exhausted(error_3) is True

    error_4 = Exception("普通网络超时 timeout")
    assert is_ai_provider_quota_exhausted(error_4) is False


def test_quota_rotation_providers_rotates_for_general_models():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        p1 = AiProvider(id=1, provider_name="Provider 1", base_url="https://api.p1.com", api_key_ciphertext="sk-1", model_name="model-a", is_active=True, credential_enabled=True, health_status=AiProviderHealthStatus.HEALTHY.value)
        p2 = AiProvider(id=2, provider_name="Provider 2", base_url="https://api.p2.com", api_key_ciphertext="sk-2", model_name="model-b", is_active=True, credential_enabled=True, health_status=AiProviderHealthStatus.HEALTHY.value)
        p3 = AiProvider(id=3, provider_name="Provider 3", base_url="https://api.p3.com", api_key_ciphertext="sk-3", model_name="model-c", is_active=False, credential_enabled=True, health_status=AiProviderHealthStatus.HEALTHY.value)
        session.add_all([p1, p2, p3])
        session.commit()

        candidates = quota_rotation_providers(session, p1, required_family="")
        assert len(candidates) == 1
        assert candidates[0].id == 2

        all_candidates = provider_candidates(session, p1, required_model_family="", allow_quota_rotation=True)
        assert [p.id for p in all_candidates] == [1, 2]


def test_fallback_stages_preserves_direct_configured_model():
    config_with_model = {"ai_model": "MiniMax-M2.5", "_ai_group_model_fallback_enabled": True}
    stages = fallback_stages(config_with_model)
    assert stages == ("direct_configured_model",)
