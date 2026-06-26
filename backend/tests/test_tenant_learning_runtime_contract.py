from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import OperationTarget, Tenant, TenantLearningSample, TenantLearningSource, TgGroup
from app.services.operations_center_learning import listener_learning_profile, listener_learning_samples

from test_tenant_target_profile import _session


pytestmark = pytest.mark.no_postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GROUP_LISTENERS = PROJECT_ROOT / "backend/app/services/group_listeners.py"
OPERATIONS_CENTER_LEARNING = PROJECT_ROOT / "backend/app/services/operations_center_learning.py"


def test_group_listener_runtime_writes_only_tenant_learning_samples():
    source = GROUP_LISTENERS.read_text()

    assert "record_tenant_group_learning_sample" in source
    assert "record_target_group_learning_sample" not in source
    assert "from .target_learning import" not in source


def test_operations_center_learning_uses_tenant_profile_services_only():
    source = OPERATIONS_CENTER_LEARNING.read_text()

    assert "tenant_target_profile" in source
    assert "tenant_learning_samples" in source
    assert "app.services.target_learning" not in source
    assert "get_learning_profile_payload" not in source
    assert "list_learning_samples_payload" not in source
    assert "operation_target_for_group" not in source


def test_operations_center_learning_returns_tenant_profile_and_samples():
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=41, tenant_id=1, tg_peer_id="-10041", title="学习群"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10041", title="学习群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        session.add(
            TenantLearningSample(
                tenant_id=1,
                source_id=source.id,
                source_message_id="runtime-1",
                source_scene="group_chat",
                text="真实样本",
                learning_status="accepted",
            )
        )
        session.commit()

        profile = listener_learning_profile(session, 1, "group", 41)
        samples = listener_learning_samples(session, 1, "group", 41)
        sample_count = session.scalar(select(func.count()).select_from(TenantLearningSample).where(TenantLearningSample.source_message_id == "runtime-1"))

    assert profile["profile_version"] == 0
    assert profile["status"] == "sample_insufficient"
    assert samples["items"][0]["source_message_id"] == "runtime-1"
    assert sample_count == 1
