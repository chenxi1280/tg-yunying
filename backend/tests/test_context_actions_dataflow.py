from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def _async_function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    async_end = source.find("\n\n  async function", start + 1)
    function_end = source.find("\n\n  function", start + 1)
    candidates = [index for index in [async_end, function_end] if index != -1]
    return source[start:min(candidates)]


def test_group_and_archive_actions_distinguish_refresh_failure_from_write_failure():
    source = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    assert "async function refreshPageDataAfterAction(actionLabel: string)" in source
    assert "showResult('页面数据刷新失败'" in source
    assert "操作已完成，但刷新页面数据失败" in source

    helper_start = source.index("async function refreshPageDataAfterAction")
    helper_end = source.index("\n\n  async function authorizeSelectedGroup", helper_start)
    helper_body = source[helper_start:helper_end]
    assert "await refresh();" in helper_body
    assert "handleActionError(" not in helper_body

    for function_name in [
        "authorizeSelectedGroup",
        "createArchive",
        "saveGroupPolicy",
        "exportArchive",
        "rerunArchive",
    ]:
        body = _async_function_body(source, function_name)
        assert "await refreshPageDataAfterAction(" in body
        refresh_block = body[body.index("await refreshPageDataAfterAction(") :]
        if "} catch" in refresh_block:
            assert "handleActionError(" not in refresh_block[: refresh_block.index("} catch")]
        else:
            assert "handleActionError(" not in refresh_block

    open_detail_body = _async_function_body(source, "openArchiveDetail")
    assert "await refreshPageDataAfterAction(" not in open_detail_body


def test_global_refresh_ignores_stale_snapshot_state_and_busy_updates():
    source = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    refresh_body = _async_function_body(source, "refresh")

    assert "const appRefreshRequestSeq = React.useRef(0);" in source
    assert "function beginAppRefreshRequest()" in source
    assert "appRefreshRequestSeq.current += 1;" in source
    assert "function isActiveAppRefreshRequest(requestSeq: number)" in source
    assert "return appRefreshRequestSeq.current === requestSeq;" in source

    assert "const requestSeq = beginAppRefreshRequest();" in refresh_body
    assert (
        "const refreshContext = { activeView, selectedPoolId, taskStatusFilter, auditFilters };"
        in refresh_body
    )
    assert "const snapshot = await loadAppSnapshot(refreshContext);" in refresh_body

    stale_guard = "if (!isActiveAppRefreshRequest(requestSeq)) return;"
    assert stale_guard in refresh_body
    assert refresh_body.index(stale_guard) < refresh_body.index("setCurrentUser(snapshot.me);")

    assert "catch (error)" in refresh_body
    catch_body = refresh_body[refresh_body.index("catch (error)") :]
    assert stale_guard in catch_body
    assert "throw error;" in catch_body
    assert "if (isActiveAppRefreshRequest(requestSeq)) setBusy('');" in refresh_body


def test_run_with_loading_keeps_newer_busy_state_and_duplicate_pending_actions():
    source = (PROJECT_ROOT / "frontend/src/app/context/actionRunner.ts").read_text()

    assert "const busyRequestSeq = React.useRef(0);" in source
    assert "const requestSeq = busyRequestSeq.current + 1;" in source
    assert "busyRequestSeq.current = requestSeq;" in source
    assert "if (busyRequestSeq.current === requestSeq) setBusy('');" in source
    assert "const index = current.indexOf(key);" in source
    assert "next.splice(index, 1);" in source
