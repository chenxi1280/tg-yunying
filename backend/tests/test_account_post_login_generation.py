from __future__ import annotations

import pytest

from app.models import TgAccountFullInitialization
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


def test_older_terminal_debt_does_not_override_newer_success(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "older-failed-generation")
        failed = create_or_attach_full_initialization(session, first_item, actor="操作员")
        failed.status = failed.stage = "failed"
        failed.failure_type = "old_profile_failure"

        succeeded = TgAccountFullInitialization(
            tenant_id=1,
            account_id=40,
            generation=failed.generation + 1,
            authorization_generation=failed.authorization_generation,
            fixed_two_fa_version=failed.fixed_two_fa_version,
            target_pool_id=10,
            status="succeeded",
            stage="succeeded",
            two_fa_status="succeeded",
            profile_status="succeeded",
            abc_status="succeeded",
        )
        session.add(succeeded)
        session.flush()

        _, current_item = _new_login_item(session, "current-gap-generation")
        current = create_or_attach_full_initialization(session, current_item, actor="操作员")
        session.commit()

    assert current.id not in {failed.id, succeeded.id}
    assert current.generation == succeeded.generation + 1
    assert current.predecessor_initialization_id == succeeded.id
