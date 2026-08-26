from __future__ import annotations

import pytest

from app.models import DeveloperAppSlotAssignment, Material, TelegramDeveloperApp
from app.services.account_login.batches import create_login_batch
from app.services.account_login.contracts import BatchLoginError
from app.services.account_login.preview import precheck_login_batch
from tests.test_account_batch_login_core import _create_payload, _lines, session_factory


pytestmark = pytest.mark.no_postgres


def test_normal_batch_requires_approved_avatar_material(session_factory) -> None:
    with session_factory() as session:
        material = session.get(Material, 50)
        material.review_status = "待审核"
        session.commit()

        with pytest.raises(BatchLoginError, match="没有已审核头像素材"):
            precheck_login_batch(session, 1, 20, _lines(), 10)


def test_normal_batch_requires_all_three_abc_assignments(session_factory) -> None:
    with session_factory() as session:
        assignment = session.get(DeveloperAppSlotAssignment, "standby_2_my")
        assignment.status = "inactive"
        session.commit()

        with pytest.raises(BatchLoginError, match="角色配置不完整"):
            precheck_login_batch(session, 1, 20, _lines(), 10)


@pytest.mark.parametrize("drift", ["inactive", "credentials_version"])
def test_normal_batch_requires_usable_abc_apps(session_factory, drift: str) -> None:
    with session_factory() as session:
        app = session.get(TelegramDeveloperApp, 32)
        if drift == "inactive":
            app.is_active = False
        else:
            app.credentials_version += 1
        session.commit()

        with pytest.raises(BatchLoginError, match="角色配置不完整"):
            precheck_login_batch(session, 1, 20, _lines(), 10)


@pytest.mark.parametrize("revision", ["material", "abc_assignment"])
def test_preview_invalidates_when_post_init_dependencies_change(
    session_factory,
    revision: str,
) -> None:
    with session_factory() as session:
        payload = _create_payload(session, _lines(), key=f"post-init-{revision}-drift")
        if revision == "material":
            session.get(Material, 50).asset_version_id += 1
        else:
            assignment = session.get(DeveloperAppSlotAssignment, "standby_1_sv")
            assignment.assignment_version += 1
        session.commit()

        with pytest.raises(BatchLoginError, match="预检结果已变化"):
            create_login_batch(session, 1, 20, "测试操作员", payload)
