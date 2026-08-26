from __future__ import annotations

import pytest

from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.reconcile import request_post_login_reconciliation
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    "case",
    [
        ("two_fa_source_resolution_failed", "pending", "required", "two_fa"),
        ("profile_prerequisite_unavailable", "manual_required", "required", "profile"),
        ("abc_manual_required", "succeeded", "manual_required", "abc"),
    ],
)
def test_safe_terminal_stage_can_recheck_same_owner(
    session_factory,
    case,
) -> None:
    failure_type, profile_status, abc_status, expected_stage = case
    with session_factory() as session:
        _, item = _new_login_item(session, f"safe-recheck-{expected_stage}")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.failure_type = failure_type
        owner.profile_status = profile_status
        owner.abc_status = abc_status
        expected_version = owner.version
        session.commit()

        result = request_post_login_reconciliation(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="依赖已修复，重新检查原阶段",
        )

    assert result.id == owner.id
    assert result.status == "pending"
    assert result.stage == expected_stage


def test_current_two_fa_manual_debt_requires_candidate_not_generic_recheck(
    session_factory,
) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "unsafe-generic-two-fa-recheck")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        expected_version = owner.version
        session.commit()

        with pytest.raises(ValueError, match="no safe recheck"):
            request_post_login_reconciliation(
                session,
                1,
                owner.id,
                expected_version=expected_version,
                actor="操作员",
                reason="不能盲目重试改密",
            )
