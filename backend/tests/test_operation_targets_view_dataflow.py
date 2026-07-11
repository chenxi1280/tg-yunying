from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGETS_VIEW = PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx"
OVERVIEW_VIEW = PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx"
TARGETS_HOOK = PROJECT_ROOT / "frontend/src/app/hooks/useOperationTargetManagementPage.ts"
TARGETS_TABLE = PROJECT_ROOT / "frontend/src/app/components/OperationTargetManagementTable.tsx"
EXTRACTED_TARGET_MODULES = [
    PROJECT_ROOT / "frontend/src/app/hooks/useOperationTargetManagementPage.ts",
    PROJECT_ROOT / "frontend/src/app/hooks/useOverviewOperationData.ts",
    PROJECT_ROOT / "frontend/src/app/components/OperationTargetManagementTable.tsx",
    PROJECT_ROOT / "frontend/src/app/components/OverviewTargetWorkbench.tsx",
]


def _source() -> str:
    return TARGETS_VIEW.read_text()


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  function", start + 1),
        source.find("\n\n  React.useEffect", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_bounded_target_workbenches_are_owned_by_small_extracted_modules():
    for module in EXTRACTED_TARGET_MODULES:
        assert module.exists(), f"missing extracted owner: {module.relative_to(PROJECT_ROOT)}"
        assert len(module.read_text().splitlines()) < 500

    targets_view = TARGETS_VIEW.read_text()
    overview_view = OVERVIEW_VIEW.read_text()
    assert "useOperationTargetManagementPage" in targets_view
    assert "<OperationTargetManagementTable" in targets_view
    assert "useOverviewOperationData" in overview_view
    assert "<OverviewTargetWorkbench" in overview_view
    assert len(targets_view.splitlines()) <= 817
    assert len(overview_view.splitlines()) <= 1394


def test_operation_target_actions_distinguish_refresh_failure_from_write_failure():
    source = _source()
    hook = TARGETS_HOOK.read_text()

    assert "async function fetchTargetPage(request: TargetListRequest): Promise<TargetListResult>" in hook
    assert "async function refreshTargetList(" in hook
    assert "async function refreshTargetDetailAfterAction(actionLabel: string, target: OperationTarget)" in source
    assert "运营目标数据刷新失败" in hook
    assert "操作已完成" in hook

    list_helper = hook[hook.index("async function refreshTargetList"):hook.index("\n\nfunction focusedTargetPath")]
    assert "const result = await fetchTargetPage(request);" in list_helper
    assert "setError(`运营目标数据刷新失败：" in list_helper

    detail_helper = source[source.index("async function refreshTargetDetailAfterAction"):source.index("\n\n  async function syncTargetMessages")]
    assert "await fetchTargetDetail(target" in detail_helper
    assert "await refreshTargetsListAfterAction(actionLabel);" in detail_helper

    for function_name in [
        "saveTarget",
        "createArchiveFromTarget",
        "saveAccountPolicy",
        "retryAdmission",
        "syncTargetMessages",
        "syncMessageComments",
    ]:
        body = _function_body(source, function_name)
        assert "操作已完成" not in body
        assert "await load();" not in body
        assert "await loadTargetDetail(" not in body
        assert (
            "await refreshTargetsListAfterAction(" in body
            or "await refreshTargetDetailAfterAction(" in body
        )


def test_operation_targets_list_refreshes_ignore_stale_responses():
    source = _source()
    hook = TARGETS_HOOK.read_text()
    fetch_targets = hook[hook.index("async function fetchTargetPage"):hook.index("\n\nfunction useTargetPolling")]
    load_targets = hook[hook.index("const load = React.useCallback"):hook.index("\n  useTargetPolling")]
    refresh_targets = hook[hook.index("async function refreshTargetList"):hook.index("\n\nfunction focusedTargetPath")]

    assert "const identityRef = React.useRef<TargetListRequestIdentity>" in hook
    assert "const controllerRef = React.useRef<AbortController | null>(null);" in hook
    assert "function beginRequest(" in hook
    assert "controllerRef.current?.abort();" in hook
    assert "queryKey: targetListQueryKey(query)" in hook
    assert "function isActiveRequest(" in hook
    assert "apiWithMeta<OperationTarget[]>(operationTargetListPath(request.query)" in fetch_targets
    assert "signal: request.controller.signal" in fetch_targets
    assert "if (!isActiveRequest(identityRef, request)) return;" in load_targets
    assert load_targets.index("if (!isActiveRequest(identityRef, request)) return;") < load_targets.index("setTargets(result.targets);")
    assert "if (isActiveRequest(identityRef, request)) setLoading(false);" in load_targets
    refresh_guard = "if (!isActiveRequest(state.identityRef, request)) return;"
    response_guard_index = refresh_targets.index(refresh_guard)
    assert response_guard_index < refresh_targets.index("state.setTargets(result.targets);")
    assert response_guard_index < refresh_targets.index("state.setTotal(result.total);")
    catch_index = refresh_targets.index("catch (error)")
    catch_guard_index = refresh_targets.index(refresh_guard, catch_index)
    assert catch_guard_index < refresh_targets.index("setError(`运营目标数据刷新失败：", catch_index)
    assert "async function syncAllTargets" in source


def test_operation_targets_use_bounded_server_pagination_and_search():
    source = _source()
    hook = TARGETS_HOOK.read_text()
    table = TARGETS_TABLE.read_text()

    assert "const [query, setQuery] = React.useState<TargetListQuery>" in hook
    assert "const [total, setTotal] = React.useState(0);" in hook
    assert "const [search, setSearch] = React.useState('');" in hook
    assert "params.set('page', String(query.page));" in hook
    assert "params.set('page_size', String(query.pageSize));" in hook
    assert "if (query.q) params.set('q', query.q);" in hook
    assert "headers.get('x-total-count')" in hook
    assert "dataSource={props.targets}" in table
    assert "current: props.query.page" in table
    assert "pageSize: props.query.pageSize" in table
    assert "total: props.total" in table
    assert "onChange={props.onPageChange}" in table
    assert "onSearch={props.onSearch}" in table
    assert "useAntdTableControls" not in hook + table
    assert "filteredRows" not in hook + table
    assert "window.setInterval(() => void load(), 60000)" in hook
    assert "props.error && <Alert" in table
    assert "<OperationTargetManagementTable" in source


def test_operation_target_focus_uses_exact_bounded_hydration():
    source = TARGETS_HOOK.read_text()
    focus_effect = source[source.index("function useFocusedTarget"):source.index("\n\nfunction consumeFocusedTarget")]
    path_builder = source[source.index("function focusedTargetPath"):source.index("\n\nfunction useFocusedTarget")]

    assert "const controllerRef = React.useRef<AbortController | null>(null);" in focus_effect
    assert "params.set('page', '1');" in path_builder
    assert "params.set('page_size', '1');" in path_builder
    assert "params.append('ids', String(targetId));" in path_builder
    assert "apiWithMeta<OperationTarget[]>(focusedTargetPath(focus.targetId)" in focus_effect
    assert "{ signal: controller.signal }" in focus_effect
    assert "const target = response.data.find((item) => item.id === focus.targetId);" in focus_effect
    assert "onMissingFocusedTarget(focus.targetId)" in focus_effect


def test_operation_targets_sync_all_uses_independent_action_sequence():
    source = _source()
    sync_all = source[source.index("async function syncAllTargets"):source.index("\n\n  async function saveTarget")]

    assert "const activeTargetsSyncAllRequestSeq = React.useRef(0);" in source
    assert "function beginTargetsSyncAllRequest()" in source
    assert "activeTargetsSyncAllRequestSeq.current += 1;" in source
    assert "function isActiveTargetsSyncAllRequest(requestSeq: number)" in source

    assert "const requestSeq = beginTargetsSyncAllRequest();" in sync_all
    assert "if (!isActiveTargetsSyncAllRequest(requestSeq)) return;" in sync_all
    assert sync_all.index("if (!isActiveTargetsSyncAllRequest(requestSeq)) return;") < sync_all.index("await refreshTargetsListAfterAction('目标全量同步');")
    assert "setTargets(result.targets);" not in sync_all
    assert "if (isActiveTargetsSyncAllRequest(requestSeq)) setSyncingAllTargets(false);" in sync_all


def test_operation_target_save_binds_payload_signature_and_request_seq():
    source = _source()
    save_target = _function_body(source, "saveTarget")

    assert "const activeTargetSaveRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "function operationTargetSavePayloadSignature(targetId: number | null, values: OperationTargetFormValues)" in source
    assert "function beginTargetSaveRequest(signature: string)" in source
    assert "function currentOperationTargetSavePayloadSignature()" in source
    assert "function isActiveTargetSaveRequest(request: { seq: number; signature: string })" in source
    assert "function isCurrentTargetSaveRequest(request: { seq: number; signature: string })" in source

    assert "const saveRequest = beginTargetSaveRequest(operationTargetSavePayloadSignature(editingTarget?.id ?? null, values));" in save_target
    assert "if (!isCurrentTargetSaveRequest(saveRequest)) return;" in save_target
    assert save_target.index("if (!isCurrentTargetSaveRequest(saveRequest)) return;") > save_target.index("await api<OperationTarget>")
    assert save_target.index("if (!isCurrentTargetSaveRequest(saveRequest)) return;") < save_target.index("setEditingTarget(null);")
    assert "if (!isCurrentTargetSaveRequest(saveRequest)) return;" in save_target[save_target.index("} catch (error) {"):]
    assert "if (isActiveTargetSaveRequest(saveRequest)) setSaving(false);" in save_target


def test_operation_target_detail_fetches_use_request_sequence():
    source = _source()
    fetch_detail = source[source.index("async function fetchTargetDetail"):source.index("\n\n  async function loadTargetDetail")]
    load_detail_start = source.index("async function loadTargetDetail")
    load_detail = source[load_detail_start:source.index("\n\n  async function refreshTargetDetailAfterAction", load_detail_start)]
    refresh_detail = source[source.index("async function refreshTargetDetailAfterAction"):source.index("\n\n  async function syncTargetMessages")]

    assert "const activeDetailTargetRequestSeq = React.useRef(0);" in source
    assert "function beginDetailTargetRequest(targetId: number)" in source
    assert "activeDetailTargetRequestSeq.current += 1;" in source
    assert "function isActiveDetailTargetRequest(targetId: number, requestSeq: number)" in source

    assert "async function fetchTargetDetail(target: OperationTarget, requestSeq: number): Promise<boolean>" in source
    assert "if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;" in fetch_detail
    assert fetch_detail.index("if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;") < fetch_detail.index("setTargetDetail(detail);")

    assert "const requestSeq = beginDetailTargetRequest(target.id);" in load_detail
    assert "return await fetchTargetDetail(target, requestSeq);" in load_detail
    assert "if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;" in load_detail
    assert "if (isActiveDetailTargetRequest(target.id, requestSeq)) setDetailLoading(false);" in load_detail

    assert "const requestSeq = beginDetailTargetRequest(target.id);" in refresh_detail
    assert "const loaded = await fetchTargetDetail(target, requestSeq);" in refresh_detail


def test_operation_target_detail_writebacks_ignore_stale_same_target_responses():
    source = _source()
    sync_messages = source[source.index("async function syncTargetMessages"):source.index("\n\n  async function syncMessageComments")]
    sync_comments = source[source.index("async function syncMessageComments"):source.index("\n\n  async function syncAllTargets")]
    save_policy = source[source.index("async function saveAccountPolicy"):source.index("\n\n  function openAdmissionRetry")]
    retry_admission = source[source.index("async function retryAdmission"):source.index("\n\n  function startEdit")]

    assert "const activeDetailTargetWriteSeq = React.useRef(0);" in source
    assert "function beginDetailTargetWrite(targetId: number)" in source
    assert "activeDetailTargetWriteSeq.current += 1;" in source
    assert "function isActiveDetailTargetWrite(targetId: number, requestSeq: number)" in source

    for block, loading_reset in [
        (sync_messages, "setSyncing(false);"),
        (save_policy, "setAccountPolicySaving('');"),
        (retry_admission, "setAdmissionRetrySaving(false);"),
    ]:
        assert "const requestSeq = beginDetailTargetWrite(target.id);" in block
        assert "if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;" in block
        response_guard = block.index("if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;")
        detail_index = block.index("setTargetDetail(")
        catch_index = block.index("catch (error)")
        catch_guard = block.index("if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;", catch_index)
        finally_index = block.index("finally")
        finally_guard = block.index("if (isActiveDetailTargetWrite(target.id, requestSeq))", finally_index)

        assert response_guard < detail_index
        assert catch_guard < block.index("setFormError(errorMessage(error));", catch_index)
        assert finally_guard < block.index(loading_reset, finally_index)

    assert "const requestSeq = beginDetailTargetWrite(target.id);" in sync_comments
    assert "if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;" in sync_comments
    response_guard = sync_comments.index("if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;")
    refresh_index = sync_comments.index("await refreshTargetDetailAfterAction('评论同步', target);")
    catch_index = sync_comments.index("catch (error)")
    catch_guard = sync_comments.index("if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;", catch_index)
    finally_index = sync_comments.index("finally")
    finally_guard = sync_comments.index("if (isActiveDetailTargetWrite(target.id, requestSeq))", finally_index)

    assert response_guard < refresh_index
    assert catch_guard < sync_comments.index("setFormError(errorMessage(error));", catch_index)
    assert finally_guard < sync_comments.index("setSyncingCommentMessageId(null);", finally_index)
