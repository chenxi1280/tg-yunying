from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGETS_VIEW = PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx"


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


def test_operation_target_actions_distinguish_refresh_failure_from_write_failure():
    source = _source()

    assert "async function fetchTargets(request: TargetListRequest): Promise<boolean>" in source
    assert "async function refreshTargetsListAfterAction(actionLabel: string)" in source
    assert "async function refreshTargetDetailAfterAction(actionLabel: string, target: OperationTarget)" in source
    assert "运营目标数据刷新失败" in source
    assert "操作已完成" in source

    list_helper = source[source.index("async function refreshTargetsListAfterAction"):source.index("\n\n  async function refreshTargetDetailAfterAction")]
    assert "await fetchTargets(request);" in list_helper
    assert "setFormError(`运营目标数据刷新失败：" in list_helper

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
    fetch_targets = source[source.index("async function fetchTargets"):source.index("\n\n  async function fetchTargetDetail")]
    load_targets = source[source.index("async function load()"):source.index("\n\n  async function refreshTargetsListAfterAction")]
    refresh_targets = source[source.index("async function refreshTargetsListAfterAction"):source.index("\n\n  async function refreshTargetDetailAfterAction")]

    assert "const activeTargetsListRequestRef = React.useRef<TargetListRequestIdentity>" in source
    assert "const targetsListAbortController = React.useRef<AbortController | null>(null);" in source
    assert "function beginTargetsListRequest(query: TargetListQuery)" in source
    assert "targetsListAbortController.current?.abort();" in source
    assert "queryKey: targetListQueryKey(query)" in source
    assert "function isActiveTargetsListRequest(request: TargetListRequest)" in source

    assert "async function fetchTargets(request: TargetListRequest): Promise<boolean>" in fetch_targets
    assert "apiWithMeta<OperationTarget[]>(operationTargetListPath(request.query)" in fetch_targets
    assert "{ signal: request.controller.signal }" in fetch_targets
    assert "if (!isActiveTargetsListRequest(request)) return false;" in fetch_targets
    assert fetch_targets.index("if (!isActiveTargetsListRequest(request)) return false;") < fetch_targets.index("setTargets(response.data);")
    assert "const responseTotal = operationTargetResponseTotal(response.headers);" in fetch_targets
    assert "setTargetTotal(responseTotal);" in fetch_targets

    assert "const request = beginTargetsListRequest(targetQueryRef.current);" in load_targets
    assert "await fetchTargets(request);" in load_targets
    assert "if (!isActiveTargetsListRequest(request)) return;" in load_targets
    assert "if (isActiveTargetsListRequest(request)) setLoading(false);" in load_targets

    assert "const request = beginTargetsListRequest(targetQueryRef.current);" in refresh_targets
    assert "await fetchTargets(request);" in refresh_targets
    assert "if (!isActiveTargetsListRequest(request)) return;" in refresh_targets

    assert "async function syncAllTargets" in source


def test_operation_targets_use_bounded_server_pagination_and_search():
    source = _source()

    assert "const [targetQuery, setTargetQuery] = React.useState<TargetListQuery>" in source
    assert "const [targetTotal, setTargetTotal] = React.useState(0);" in source
    assert "const [targetSearch, setTargetSearch] = React.useState('');" in source
    assert "params.set('page', String(query.page));" in source
    assert "params.set('page_size', String(query.pageSize));" in source
    assert "if (query.q) params.set('q', query.q);" in source
    assert "headers.get('x-total-count')" in source
    assert "dataSource={targets}" in source
    assert "current: targetQuery.page" in source
    assert "pageSize: targetQuery.pageSize" in source
    assert "total: targetTotal" in source
    assert "onChange={handleTargetTableChange}" in source
    assert "onSearch={submitTargetSearch}" in source
    assert "useAntdTableControls" not in source
    assert "filteredRows" not in source
    assert "window.setInterval(() => void load(), 60000)" in source
    assert "{formError && <Alert className=\"form-alert\" type=\"error\" showIcon message={formError} />}" in source


def test_operation_target_focus_uses_exact_bounded_hydration():
    source = _source()
    focus_effect = source[source.index("if (!focusTarget || appliedFocusNonce.current === focusTarget.nonce)"):source.index("\n\n  async function saveTarget")]

    assert "const focusTargetAbortController = React.useRef<AbortController | null>(null);" in source
    assert "params.set('page', '1');" in focus_effect
    assert "params.set('page_size', '1');" in focus_effect
    assert "params.append('ids', String(focusTarget.targetId));" in focus_effect
    assert "apiWithMeta<OperationTarget[]>(`/operation-targets?${params.toString()}`" in focus_effect
    assert "{ signal: controller.signal }" in focus_effect
    assert "const target = response.data.find((item) => item.id === focusTarget.targetId);" in focus_effect
    assert focus_effect.index("if (!target)") < focus_effect.index("未找到目标 #")


def test_operation_targets_sync_all_uses_independent_action_sequence():
    source = _source()
    sync_all = source[source.index("async function syncAllTargets"):source.index("\n\n  React.useEffect")]

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
    load_detail = source[load_detail_start:source.index("\n\n  async function load()", load_detail_start)]
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
