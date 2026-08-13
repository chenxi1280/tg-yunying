from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_VIEW = PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx"
ACCOUNT_LAZY_AVATAR = PROJECT_ROOT / "frontend/src/app/components/AccountLazyAvatar.tsx"


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
