from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_account_detail_filters_operation_attempts_in_database():
    accounts_source = (PROJECT_ROOT / "backend/app/services/accounts.py").read_text()
    operations_source = (PROJECT_ROOT / "backend/app/services/operations.py").read_text()

    assert "account_id: int | None = None" in operations_source
    assert "OperationTaskAttempt.account_id == account_id" in operations_source
    assert "list_operation_attempts(session, account.tenant_id, account_id=account.id)[:50]" in accounts_source
    assert "[attempt for attempt in list_operation_attempts(session, account.tenant_id) if attempt.account_id == account.id]" not in accounts_source


def test_account_detail_filters_operation_targets_in_database():
    accounts_source = (PROJECT_ROOT / "backend/app/services/accounts.py").read_text()
    operations_source = (PROJECT_ROOT / "backend/app/services/operations.py").read_text()

    assert "operation_targets = filter_operation_targets(session, account.tenant_id, account_id=account.id)" in accounts_source
    assert "def _account_operation_targets_stmt(" in operations_source
    assert "TgGroupAccount.account_id == account_id" in operations_source
    assert "TgGroupAccount.can_send.is_(True)" in operations_source
