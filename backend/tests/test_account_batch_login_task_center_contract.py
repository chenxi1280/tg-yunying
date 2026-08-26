from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def _source(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text()


def test_accounts_page_keeps_login_task_entry_visible_without_create_permission():
    accounts = _source("frontend/src/app/views/AccountsView.tsx")
    control = _source("frontend/src/app/views/AccountBatchLoginControl.tsx")

    assert "canCreateBatch={canBatchLogin}" in accounts
    assert "canBatchLogin && <AccountBatchLoginControl" not in accounts
    assert '>登录任务</Button>' in control
    assert "count={activeBatchCount}" in control
    assert "{canCreateBatch && <Button" in control


def test_login_task_center_recovers_server_batches_and_polls_active_count():
    source = _source("frontend/src/app/views/AccountBatchLoginTaskCenter.tsx")
    presentation = _source("frontend/src/app/views/accountBatchLoginPresentation.ts")

    assert "TASK_LIST_LIMIT = 200" in source
    assert "/tg-accounts/login-batches?limit=${TASK_LIST_LIMIT}&offset=0" in source
    assert "TASK_LIST_POLL_MS = 5_000" in source
    assert "window.setInterval" in source
    assert "onActiveCountChange" in source
    assert "查看详情" in source
    for status in ["queued", "running", "cancelling"]:
        assert status in presentation
    assert "localStorage" not in source


def test_login_batch_detail_drawer_requests_200_items_and_api_allows_them():
    drawer = _source("frontend/src/app/views/AccountBatchLoginDrawer.tsx")
    control = _source("frontend/src/app/views/AccountBatchLoginControl.tsx")
    router = _source("backend/app/api/routers/account_login_batches.py")

    assert "LOGIN_BATCH_DETAIL_ITEM_LIMIT = 200" in drawer
    assert "item_limit=${LOGIN_BATCH_DETAIL_ITEM_LIMIT}" in drawer
    assert "DRAWER_STACK_OFFSET_PX" in drawer
    assert "stackIndex={index}" in control
    assert "重试失败行" in drawer
    assert "canBulkRetryFailed" in drawer
    assert "LOGIN_BATCH_DETAIL_ITEM_LIMIT = 200" in router
    assert "le=LOGIN_BATCH_DETAIL_ITEM_LIMIT" in router


def test_login_batch_detail_restarts_polling_after_manual_recovery():
    drawer = _source("frontend/src/app/views/AccountBatchLoginDrawer.tsx")

    assert "pollRevision" in drawer
    assert "setPollRevision((value) => value + 1)" in drawer
    assert "[batchId, pollRevision]" in drawer


def test_batch_login_capability_exposes_parallel_worker_slots():
    frontend_type = _source("frontend/src/app/types/accountLogin.ts")
    compose = _source("docker-compose.server.yml")

    assert "worker_concurrency: number" in frontend_type
    assert "ACCOUNT_BATCH_LOGIN_WORKER_CONCURRENCY" in compose


def test_batch_login_read_routes_enforce_accounts_view_permission():
    router = _source("backend/app/api/routers/account_login_batches.py")

    for function_name in ["get_login_batches", "get_login_batch_detail", "get_login_batch_notifications"]:
        start = router.index(f"def {function_name}(")
        next_route = router.find("\n@router.", start)
        block = router[start:next_route if next_route >= 0 else len(router)]
        assert 'ensure_permission(current_user, "accounts.view")' in block
