from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.routers.accounts import _parse_account_ids


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_VIEW = PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx"
ACCOUNT_LAZY_AVATAR = PROJECT_ROOT / "frontend/src/app/components/AccountLazyAvatar.tsx"
ACCOUNT_PAGE_HOOK = PROJECT_ROOT / "frontend/src/app/hooks/useAccountsServerPage.tsx"
REFRESH_SOURCE = PROJECT_ROOT / "frontend/src/app/context/refresh.ts"


def _source() -> str:
    return ACCOUNTS_VIEW.read_text()


def _function_body(source: str, name: str) -> str:
    start = source.index(f"async function {name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_accounts_view_actions_distinguish_refresh_failure_from_write_failure():
    source = _source()

    assert "async function fetchAvailabilitySummary(requestSeq: number)" in source
    assert "async function refreshAvailabilityAfterAction(actionLabel: string)" in source
    assert "账号中心数据刷新失败" in source
    assert "操作已完成" in source

    helper_start = source.index("async function refreshAvailabilityAfterAction")
    helper_end = source.index("\n\n  async function rebuildAvailability", helper_start)
    helper = source[helper_start:helper_end]
    assert "await fetchAvailabilitySummary(requestSeq);" in helper
    assert "setError(`账号中心数据刷新失败：" in helper

    for function_name in ["rebuildAvailability", "refreshSelectedSecurity"]:
        body = _function_body(source, function_name)
        assert "await refreshAvailabilityAfterAction(" in body


def test_accounts_view_availability_refreshes_ignore_stale_responses():
    source = _source()

    fetch_data = _function_body(source, "fetchAvailabilitySummary")
    load_data = _function_body(source, "loadAvailability")
    refresh_data = _function_body(source, "refreshAvailabilityAfterAction")

    assert "const availabilityRequestSeq = React.useRef(0);" in source
    assert "function beginAvailabilityRequest()" in source
    assert "availabilityRequestSeq.current += 1;" in source
    assert "function isActiveAvailabilityRequest(requestSeq: number)" in source
    assert "async function fetchAvailabilitySummary(requestSeq: number)" in source

    stale_guard = "if (!isActiveAvailabilityRequest(requestSeq)) return false;"
    assert stale_guard in fetch_data
    assert fetch_data.index(stale_guard) < fetch_data.index("setAvailabilityByAccountId(")
    assert "return true;" in fetch_data

    assert "const requestSeq = beginAvailabilityRequest();" in load_data
    assert "await fetchAvailabilitySummary(requestSeq);" in load_data
    load_error_guard = "if (!isActiveAvailabilityRequest(requestSeq)) return false;"
    assert load_error_guard in load_data
    assert load_data.index(load_error_guard) < load_data.index("setError(error instanceof Error ? error.message : '读取账号可用性汇总失败');")
    assert "if (isActiveAvailabilityRequest(requestSeq)) setAvailabilityLoading(false);" in load_data

    assert "const requestSeq = beginAvailabilityRequest();" in refresh_data
    assert "await fetchAvailabilitySummary(requestSeq);" in refresh_data
    refresh_error_guard = "if (!isActiveAvailabilityRequest(requestSeq)) return;"
    assert refresh_error_guard in refresh_data
    assert refresh_data.index(refresh_error_guard) < refresh_data.index("setError(`账号中心数据刷新失败：")


def test_accounts_view_loads_known_avatars_only_after_they_become_visible():
    accounts_source = _source()
    avatar_source = ACCOUNT_LAZY_AVATAR.read_text()

    assert "<AccountIdentityCell" in accounts_source
    assert "hasAvatar={Boolean(account.avatar_object_key)}" in accounts_source
    assert "previewUrl={account.avatar_preview_url}" in accounts_source
    assert "new IntersectionObserver" in avatar_source
    assert "if (!entry.isIntersecting) return;" in avatar_source
    assert "setLoadState('loading');" in avatar_source
    assert "src={imageUrl}" in avatar_source


def test_account_lazy_avatar_exposes_waiting_loading_success_and_failure_states():
    source = ACCOUNT_LAZY_AVATAR.read_text()

    assert "if (state === 'waiting') return '有头像';" in source
    assert "if (state === 'loading') return '加载中';" in source
    assert "if (state === 'loaded') return '头像已加载';" in source
    assert "return '加载失败';" in source
    assert "onLoad={() => setLoadState('loaded')}" in source
    assert "onError={() => setLoadState('failed')}" in source
    assert "if (!hasAvatar)" in source
    assert "<Avatar>{displayName.slice(0, 1)}</Avatar>" in source


def test_account_center_uses_server_side_twenty_row_pages():
    refresh = REFRESH_SOURCE.read_text()
    hook = ACCOUNT_PAGE_HOOK.read_text()
    accounts = _source()

    assert "const ACCOUNT_LIST_PAGE_SIZE = 20;" in refresh
    assert "loadFirstAccountPage(context.selectedPoolId)" in refresh
    accounts_loader = refresh[refresh.index("async function loadAccountsPage"):refresh.index("function messageTaskPath")]
    assert "loadAccountList(context.selectedPoolId)" not in accounts_loader
    assert "Promise.allSettled" in accounts_loader
    assert "if (pageResult.status === 'rejected') throw pageResult.reason;" in accounts_loader
    assert "const ACCOUNT_PAGE_SIZE = 20;" in hook
    assert "page_size: String(ACCOUNT_PAGE_SIZE)" in hook
    assert "X-Total-Count" in hook
    assert "showSizeChanger: false" in hook
    assert "dataSource={accountTable.rows}" in accounts
    assert "loading={accountTable.loading}" in accounts


def test_account_center_limits_availability_to_current_page():
    source = _source()

    assert "availability/summary?account_ids=${accountIds}" in source
    assert "当前页账号级受限" in source
    assert "当前页登录有问题" in source
    assert "setAvailabilityByAccountId(new Map())" in source
    assert "setSelectedAccountIds([])" in source


def test_account_summary_shortcuts_run_server_search_even_when_current_page_count_is_zero():
    source = _source()

    for query in ["账号级受限", "登录有问题", "健康分偏低", "代理异常", "session_missing", "资料待初始化"]:
        assert f"onClick={{() => accountTable.setQuery('{query}')}}" in source
    assert "disabled={!restrictedAccounts.length}" not in source


def test_account_availability_filter_accepts_only_one_to_twenty_positive_unique_ids():
    assert _parse_account_ids(None) is None
    assert _parse_account_ids("3,2,3") == (3, 2)

    for value in ["", "0", "abc", ",".join(str(item) for item in range(1, 22))]:
        with pytest.raises(HTTPException) as exc_info:
            _parse_account_ids(value)
        assert exc_info.value.status_code == 422


def test_session_expired_account_exposes_existing_login_action():
    source = _source()
    login_statuses = source[source.index("const LOGIN_REQUIRED_STATUSES"):source.index("const LOGIN_PROBLEM_STATUSES")]
    actions = source[source.index("title: '操作'"):]

    assert "'Session失效'" in login_statuses
    assert "LOGIN_REQUIRED_STATUSES.has(account.status)" in actions
    assert "onClick={() => onVerifyAccount(account)}" in actions


def test_account_pool_navigation_is_horizontally_reachable_without_fallback():
    source = _source()
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    styles = (PROJECT_ROOT / "frontend/src/styles/_legacy.css").read_text()

    assert "Segmented" not in source
    assert "role=\"tablist\"" in source
    assert "role=\"tab\"" in source
    assert "ArrowLeft" in source and "ArrowRight" in source
    assert "选中分组已删除或不可用" in source
    assert "setSelectedPoolId('')" in source
    assert "overflow-x: auto;" in styles
    assert "accountPools.find((pool) => pool.id === selectedPoolId) ?? null" in context
    assert "accountPools.find((pool) => pool.is_default)" not in context
