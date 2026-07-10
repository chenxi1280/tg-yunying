from pathlib import Path

import pytest

from app.permission_middleware import required_permission


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_operation_target_detail_does_not_auto_sync_for_read_only_users():
    source = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    start = source.index("function openDetail")
    end = source.index("\n  function openCreate", start)
    open_detail = source[start:end]

    assert "syncTargetMessages(target)" in open_detail
    assert "if (loaded && canManageTargets)" in open_detail
    assert open_detail.index("if (loaded && canManageTargets)") < open_detail.index("syncTargetMessages(target)")


def test_operation_target_focus_opens_detail_once():
    view = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    hook = (PROJECT_ROOT / "frontend/src/app/hooks/useOperationTargetManagementPage.ts").read_text()
    focus_effect = hook[hook.index("function useFocusedTarget"):hook.index("\n\nfunction consumeFocusedTarget")]
    consume = hook[hook.index("function consumeFocusedTarget"):hook.index("\n\nexport function useOperationTargetManagementPage")]

    assert focus_effect.count("consumeFocusedTarget({ target: current") == 1
    assert focus_effect.count("consumeFocusedTarget({ target, nonce:") == 1
    assert consume.index("context.appliedNonce.current = context.nonce;") < consume.index("onOpenFocusedTarget(context.target);")
    assert consume.index("onOpenFocusedTarget(context.target);") < consume.index("onFocusTargetConsumed?.();")
    assert "onOpenFocusedTarget: openDetail" in view


def test_operation_targets_load_surfaces_backend_error_detail():
    view = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    hook = (PROJECT_ROOT / "frontend/src/app/hooks/useOperationTargetManagementPage.ts").read_text()
    fetch_targets = hook[hook.index("async function fetchTargetPage"):hook.index("\n\nfunction useTargetPolling")]
    load_block = hook[hook.index("const load = React.useCallback"):hook.index("\n  useTargetPolling")]

    assert "const [formError, setFormError] = React.useState('');" in view
    assert "setError: setFormError" in view
    assert "apiWithMeta<OperationTarget[]>(operationTargetListPath(request.query)" in fetch_targets
    assert "const result = await fetchTargetPage(request);" in load_block
    assert "catch (error)" in load_block
    assert "if (isActiveRequest(identityRef, request)) setError(requestError(error));" in load_block


def test_operation_target_detail_sync_only_runs_after_detail_load_success():
    source = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    fetch_detail = source[source.index("async function fetchTargetDetail"):source.index("\n\n  async function loadTargetDetail")]
    load_detail = source[source.index("async function loadTargetDetail"):source.index("\n  async function syncTargetMessages")]
    open_detail = source[source.index("function openDetail"):source.index("\n  function openCreate")]

    assert "async function loadTargetDetail(target: OperationTarget): Promise<boolean>" in source
    assert "return true;" in fetch_detail
    assert "return false;" in load_detail
    assert "then((loaded) => {" in open_detail
    assert "if (loaded && canManageTargets) void syncTargetMessages(target);" in open_detail


def test_operation_target_detail_actions_ignore_stale_target_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    load_detail = source[source.index("async function loadTargetDetail"):source.index("\n  async function syncTargetMessages")]
    sync_messages = source[source.index("async function syncTargetMessages"):source.index("\n  async function syncMessageComments")]
    sync_comments = source[source.index("async function syncMessageComments"):source.index("\n  async function syncAllTargets")]
    create_archive = source[source.index("async function createArchiveFromTarget"):source.index("\n  async function saveAccountPolicy")]
    save_policy = source[source.index("async function saveAccountPolicy"):source.index("\n  function openAdmissionRetry")]
    retry_admission = source[source.index("async function retryAdmission"):source.index("\n  function startEdit")]
    open_detail = source[source.index("function openDetail"):source.index("\n  function openCreate")]
    close_detail = source[source.index("function closeDetail"):source.index("\n\n  const failedAdmissionAccounts")]

    assert "const activeDetailTargetId = React.useRef<number | null>(null);" in source
    assert "function isActiveDetailTarget(targetId: number)" in source
    assert "activeDetailTargetId.current = target.id;" in open_detail
    assert "activeDetailTargetId.current = null;" in close_detail
    assert "if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;" in load_detail
    assert "if (isActiveDetailTargetRequest(target.id, requestSeq)) setDetailLoading(false);" in load_detail
    assert "if (!isActiveDetailTarget(target.id)) return;" in create_archive
    catch_block = create_archive[create_archive.index("catch (error)"):]
    assert catch_block.index("if (!isActiveDetailTarget(target.id)) return;") < catch_block.index("setFormError(errorMessage(error));")
    for block in [sync_messages, sync_comments, save_policy, retry_admission]:
        assert "if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;" in block
        catch_block = block[block.index("catch (error)"):]
        assert catch_block.index("if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;") < catch_block.index("setFormError(errorMessage(error));")
    assert "if (isActiveDetailTargetWrite(target.id, requestSeq)) setSyncing(false);" in sync_messages
    assert "if (isActiveDetailTargetWrite(target.id, requestSeq)) setSyncingCommentMessageId(null);" in sync_comments
    assert "if (isActiveDetailTarget(target.id)) setCreatingArchiveId(null);" in create_archive
    assert "if (isActiveDetailTargetWrite(target.id, requestSeq)) setAccountPolicySaving('');" in save_policy
    assert "if (isActiveDetailTargetWrite(target.id, requestSeq)) setAdmissionRetrySaving(false);" in retry_admission


def test_task_center_load_and_focus_task_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    fetch_block = source[source.index("async function fetchTaskListData"):source.index("\n\n  async function load(")]
    load_block = source[source.index("async function load("):source.index("\n\n  async function refreshTaskListAfterAction")]
    focus_effect = source[source.index("if (!focusTask || appliedFocusNonce.current === focusTask.nonce)"):source.index("\n  async function fetchTaskDetail")]

    assert "api<TaskCenterTask[]>" in fetch_block
    assert "api<SchedulingSetting>" in fetch_block
    assert "await fetchTaskListData(requestSeq, nextTaskTypeFilter);" in load_block
    assert "catch (error)" in load_block
    assert "if (!isActiveTaskListRequest(requestSeq)) return;" in load_block
    assert "setActionError(`读取任务列表失败：${errorMessage(error)}`);" in load_block
    assert ".catch((error) => {" in focus_effect
    assert "if (!isActiveDetailRequest(focusTask.taskId, requestSeq)) return;" in focus_effect
    assert "setActionError(`读取任务 ${focusTask.taskId} 详情失败：${errorMessage(error)}`);" in focus_effect


def test_task_center_form_support_load_failures_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    prefill_effect = source[source.index("if (!prefill || appliedPrefillNonce.current === prefill.nonce)"):source.index("\n  React.useEffect(() => {\n    if (!focusTask")]
    create_task = source[source.index("async function openCreateTask"):source.index("\n\n  function editValuesFromTask")]
    edit_task = source[source.index("async function openEditTask"):source.index("\n\n  function closeEditTaskModal")]
    reset_type_fields = source[source.index("function resetTypeFields"):source.index("\n  const table")]

    assert "setActionError(`读取任务表单支撑数据失败：${errorMessage(error)}`)" in create_task
    assert "setActionError(`读取任务表单支撑数据失败：${errorMessage(error)}`)" in edit_task
    assert "setActionError(`读取任务预填支撑数据失败：${errorMessage(error)}`)" in prefill_effect
    assert "setActionError(`读取任务类型支撑数据失败：${errorMessage(error)}`)" in reset_type_fields
    assert "void ensureTargets();" not in prefill_effect
    assert "void ensureTaskFormData(nextType).then" not in reset_type_fields


def test_task_center_detail_section_pages_ignore_stale_task_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    section_loader = source[source.index("async function loadDetailSectionPage"):source.index("\n  function loadDetailSectionsForDetail")]

    assert "const response = await apiWithMeta<any[]>(`/tasks/${taskDetail.task.id}/${endpoints[kind]}?${params.toString()}`);" in section_loader
    assert "if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;" in section_loader
    response_index = section_loader.index("const response = await apiWithMeta")
    guard_index = section_loader.index("if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;", response_index)
    success_page_index = section_loader.index("setDetailSectionPage(kind, { current: page, pageSize, total, loading: false });")
    catch_index = section_loader.index("} catch (error) {")
    catch_guard_index = section_loader.index("if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;", catch_index)
    catch_page_index = section_loader.index("setDetailSectionPage(kind, (current) => ({ ...current, loading: false }));", catch_index)
    catch_error_index = section_loader.index("setActionError(`读取详情分页失败：${errorMessage(error)}`);", catch_index)

    assert guard_index < success_page_index
    assert catch_guard_index < catch_page_index < catch_error_index


def test_task_center_frontend_supports_search_join_group_contract():
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    detail = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    grouping = (PROJECT_ROOT / "frontend/src/app/views/taskCenterListGrouping.ts").read_text()

    assert "'search_join_group'" in types
    assert "搜索目标群点击任务" in view_model
    assert "search_join_group: '/tasks/search-join-group'" in view_model
    assert "search_join_group: '/tasks/search-join-group/create-and-start'" in view_model
    for field in ["search_bots", "keyword_hashes", "pre_join_decoy_click_max", "post_join_safe_navigation_max"]:
        assert field in wizard
        assert field in view_model
    for label in ["目标资料相关性", "内容健康", "极搜生态", "付费关键词广告", "排名观察", "联动状态"]:
        assert label in detail
    assert "search_join_stats" in detail
    assert "search_join_group" in grouping


def test_task_center_membership_pages_ignore_stale_task_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    load_for_detail_start = source.index("async function loadMembershipForDetail")
    load_for_detail = source[load_for_detail_start:source.index("\n  async function loadDetail", load_for_detail_start)]
    fetch_membership = source[source.index("async function fetchMembershipItems"):source.index("\n  async function loadMembershipPage")]
    load_page = source[source.index("async function loadMembershipPage"):source.index("\n  function updateMembershipFilters")]

    assert "async function fetchMembershipItems(taskId: string, page: number, pageSize: number, filters: MembershipFilters = membershipFilters): Promise<TaskMembershipItem[] | null>" in source
    assert "if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;" in fetch_membership
    response_index = fetch_membership.index("const response = await apiWithMeta<TaskMembershipItem[]>")
    guard_index = fetch_membership.index("if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;", response_index)
    page_index = fetch_membership.index("setMembershipPage({ current: page, pageSize, total, loading: false });")
    catch_index = fetch_membership.index("} catch (error) {")
    catch_guard_index = fetch_membership.index("if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;", catch_index)
    catch_page_index = fetch_membership.index("setMembershipPage((current) => ({ ...current, loading: false }));", catch_index)

    assert guard_index < page_index
    assert catch_guard_index < catch_page_index
    assert "if (!membershipItems || activeDetailTaskId.current !== taskDetail.task.id) return taskDetail;" in load_for_detail
    assert "if (activeDetailTaskId.current !== taskDetail.task.id) return taskDetail;" in load_for_detail[load_for_detail.index("} catch (error) {"):]
    assert "if (!membershipItems || activeDetailTaskId.current !== detail.task.id) return;" in load_page


def test_login_flow_has_no_unimplemented_self_registration_path():
    frontend_files = [
        "frontend/src/app/context.tsx",
        "frontend/src/app/context/types.ts",
        "frontend/src/app/context/authActions.ts",
        "frontend/src/app/AppShell.tsx",
    ]
    frontend = "\n".join((PROJECT_ROOT / path).read_text() for path in frontend_files)
    backend_schema = (PROJECT_ROOT / "backend/app/schemas/auth.py").read_text()

    assert "/auth/register" not in frontend
    assert "authMode" not in frontend
    assert "registerForm" not in frontend
    assert "register:" not in frontend
    assert "async function register" not in frontend
    assert "AuthRegisterRequest" not in backend_schema


def test_login_form_submits_with_enter_key():
    source = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    login_block = source[source.index("if (!token) {"):source.index("\n\n  return (", source.index("if (!token) {"))]

    assert '<Form layout="vertical" onFinish={login}>' in login_block
    assert '<Button type="primary" htmlType="submit" block' in login_block


def test_login_captcha_errors_surface_backend_detail_but_password_stays_generic():
    source = (PROJECT_ROOT / "frontend/src/app/context/authActions.ts").read_text()
    refresh_block = source[source.index("async function refreshCaptchaChallenge"):source.index("\n  async function requestCaptchaToken")]
    verify_block = source[source.index("async function requestCaptchaToken"):source.index("\n  async function verifyCaptcha")]
    login_block = source[source.index("async function login"):source.index("\n  async function changePassword")]

    assert "import { API_BASE, api, ApiError }" in source
    assert "function authErrorMessage(error: unknown)" in source
    assert "setCaptchaError(`验证码加载失败：${authErrorMessage(error)}`);" in refresh_block
    assert "setCaptchaError(`验证码验证失败：${authErrorMessage(error)}`);" in verify_block
    assert "setNotice('登录失败，请检查账号和密码')" in login_block
    bad_credentials_branch = login_block[
        login_block.index("if (response.status === 401)"):
        login_block.index("} else {", login_block.index("if (response.status === 401)"))
    ]
    assert "authErrorMessage" not in bad_credentials_branch


def test_login_submit_distinguishes_captcha_token_and_network_errors_from_bad_credentials():
    source = (PROJECT_ROOT / "frontend/src/app/context/authActions.ts").read_text()
    login_block = source[source.index("async function login"):source.index("\n  async function changePassword")]

    assert "const text = await response.text().catch(() => '');" in login_block
    assert "if (response.status === 401)" in login_block
    assert "params.setNotice('登录失败，请检查账号和密码');" in login_block
    assert "params.setNotice(`登录失败：${authErrorMessage(new ApiError(response.status, text))}`);" in login_block
    assert "catch (error)" in login_block
    assert "params.setNotice(`登录请求失败：${authErrorMessage(error)}`);" in login_block
    assert "finally" in login_block
    assert "params.setBusy('');" in login_block


def test_api_error_message_formats_fastapi_detail_for_direct_error_message_views():
    source = (PROJECT_ROOT / "frontend/src/shared/api/client.ts").read_text()
    modal_state = (PROJECT_ROOT / "frontend/src/app/context/modalState.ts").read_text()
    api_error_class = source[source.index("export class ApiError"):source.index("\nexport class AuthExpiredError")]

    assert "function apiErrorMessage(status: number, body: string)" in source
    assert "function apiDetailMessage(detail: unknown)" in source
    assert "super(apiErrorMessage(status, body));" in api_error_class
    assert "const parsed = JSON.parse(body) as { detail?: unknown };" in source
    assert "const detailMessage = apiDetailMessage(parsed.detail);" in source
    assert "if (detailMessage) return detailMessage;" in source
    assert "if (Array.isArray(detail))" in source
    assert "const path = Array.isArray(item.loc) ? item.loc.join('.') : String(item.loc ?? '');" in source
    assert "record.message ?? record.failure_detail" in source
    assert "record.trace_id" in source
    assert "return body || `HTTP ${status}`;" in source
    assert "if (error instanceof ApiError) return error.message;" in modal_state
    assert "JSON.parse(error.body)" not in modal_state


def _required_frontend_source(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path
    assert path.exists(), f"missing frontend source: {relative_path}"
    return path.read_text()


def test_remote_operation_target_hook_uses_bounded_query_identity_and_immutable_hydration():
    hook = _required_frontend_source("frontend/src/app/hooks/useOperationTargetOptions.ts")
    types = _required_frontend_source("frontend/src/app/types/operations.ts")
    client = _required_frontend_source("frontend/src/shared/api/client.ts")

    assert "export type OperationTargetOptionQuery = Readonly<{" in types
    assert "readonly ids?: readonly number[];" in types
    for capability in ["'send'", "'listen'", "'archive'", "'task'"]:
        assert capability in types

    assert "const OPERATION_TARGET_PAGE_SIZE = 50;" in hook
    assert "params.set('page', '1');" in hook
    assert "params.set('page_size', String(OPERATION_TARGET_PAGE_SIZE));" in hook
    assert "params.set('q', query.q);" in hook
    assert "params.set('target_type', query.targetType);" in hook
    assert "params.set('account_id', String(query.accountId));" in hook
    assert "params.set('capability', query.capability);" in hook
    assert "params.append('ids', String(id));" in hook
    assert "apiWithMeta<OperationTarget[]>" in hook
    assert "response.headers.get('x-total-count')" in hook

    assert "type OperationTargetRequestIdentity = Readonly<{" in hook
    assert "sequence: number;" in hook
    assert "queryKey: string;" in hook
    assert "searchRequestRef" in hook
    assert "hydrationRequestRef" in hook
    assert "isCurrentRequest" in hook
    assert "setPageTargets(response.data);" in hook
    assert "return [...byId.values()];" in hook

    hydration_start = hook.index("const hydrateIds = React.useCallback")
    hydration_end = hook.index("const ensureIds = React.useCallback", hydration_start)
    hydration = hook[hydration_start:hydration_end]
    assert "setSelectedTargets([]);" in hydration
    assert hydration.index("setSelectedTargets([]);") < hydration.index("setLoading(true);")
    assert "const hydrated = mergeOperationTargets([], responses.flatMap((response) => response.data));" in hydration
    assert "setSelectedTargets(hydrated);" in hydration
    assert "setSelectedTargets((current) => mergeOperationTargets(current, response.data));" not in hydration

    assert "const { timeoutMs = 15_000" in client
    assert "timeoutMs:" not in hook


def test_remote_operation_target_requests_debounce_and_cancel_stale_lifecycles():
    hook = _required_frontend_source("frontend/src/app/hooks/useOperationTargetOptions.ts")

    assert "const OPERATION_TARGET_SEARCH_DEBOUNCE_MS = 250;" in hook
    assert "window.setTimeout(" in hook
    assert "OPERATION_TARGET_SEARCH_DEBOUNCE_MS" in hook
    assert "window.clearTimeout(timerId);" in hook
    assert hook.count("const controller = new AbortController();") >= 2
    assert "controller.abort();" in hook
    assert "loadSearchPage(controller.signal)" in hook
    assert "ensureIds(selectedIds)" in hook
    assert "loadTargetIdBatches(normalized, signal)" in hook
    assert "{ signal }" in hook
    assert hook.count("signal.aborted || !isCurrentRequest") >= 4
    search_request = hook[hook.index("const loadSearchPage = React.useCallback"):hook.index("React.useEffect(() => {", hook.index("const loadSearchPage = React.useCallback"))]
    hydration_request = hook[hook.index("const hydrateIds = React.useCallback"):hook.index("const ensureIds = React.useCallback")]
    for request in [search_request, hydration_request]:
        assert "if (signal.aborted) return;" in request
        assert request.index("if (signal.aborted) return;") < request.index("setLoading(")


def test_remote_operation_target_hook_owns_public_hydration_cancellation():
    hook = _required_frontend_source("frontend/src/app/hooks/useOperationTargetOptions.ts")

    assert "signal: AbortSignal = new AbortController().signal" not in hook
    assert "const hydrationControllerRef = React.useRef<AbortController | null>(null);" in hook
    assert "const hydrateIds = React.useCallback(async (ids: readonly number[], signal: AbortSignal)" in hook
    assert "const ensureIds = React.useCallback(async (ids: readonly number[])" in hook
    assert "hydrationControllerRef.current?.abort();" in hook
    assert "hydrationControllerRef.current = controller;" in hook
    assert "await hydrateIds(ids, controller.signal);" in hook
    assert "if (hydrationControllerRef.current === controller)" in hook
    assert "hydrationControllerRef.current = null;" in hook
    assert "return () => hydrationControllerRef.current?.abort();" in hook


def test_remote_operation_target_api_client_composes_caller_abort_and_timeout():
    client = _required_frontend_source("frontend/src/shared/api/client.ts")
    request = client[client.index("export async function apiWithMeta"):]

    assert "type RequestAbortCause = 'none' | 'caller' | 'timeout';" in client
    assert "function createRequestAbortControls(" in client
    assert "callerSignal.addEventListener('abort', abortFromCaller, { once: true });" in client
    assert "callerSignal.removeEventListener('abort', abortFromCaller);" in client
    assert "cause === 'timeout'" in client
    assert "const { timeoutMs = 15_000, signal: callerSignal, ...fetchOptions }" in request
    assert "const abortControls = createRequestAbortControls(callerSignal, timeoutMs);" in request
    fetch_options = request[request.index("const response = await fetch"):request.index("if (!response.ok)")]
    assert fetch_options.index("...fetchOptions") < fetch_options.index("signal: abortControls.signal")
    assert "if (abortControls.didTimeout()) throw new ApiError(408, 'request timeout');" in request
    assert "error instanceof DOMException && error.name === 'AbortError'" not in request
    assert "abortControls.cleanup();" in request


def test_frontend_operator_template_can_open_and_manage_ai_voice_profiles():
    source = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    operator_template = source[source.index("'运营管理员'"):source.index("'账号添加专员'")]

    assert "'account_masks.view'" in operator_template
    assert "'ai_voice_profiles.manage'" in operator_template
    assert "'account_environment.manage'" in operator_template


def test_account_masks_is_first_level_menu_and_system_config_has_clash_only():
    routes = (PROJECT_ROOT / "frontend/src/app/routes.ts").read_text()
    utils = (PROJECT_ROOT / "frontend/src/app/utils.ts").read_text()
    shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    system_config = (PROJECT_ROOT / "frontend/src/app/views/SystemConfigView.tsx").read_text()
    account_masks = (PROJECT_ROOT / "frontend/src/app/views/AccountMasksView.tsx").read_text()

    assert "accountMasks: '/account-masks'" in routes
    assert "accountMasks: 'account_masks.view'" in utils
    assert "['accountMasks', '账号面具'" in shell
    assert "const AccountMasksView = React.lazy(() => import('./views/AccountMasksView'));" in shell
    assert "activeView === 'accountMasks'" in shell
    assert "AIAccountVoiceProfilesView" not in system_config
    assert "label: 'Clash 配置'" in system_config
    for label in ["面具管理", "账号代理", "授权指纹", "异常与审计"]:
        assert label in account_masks


def test_page_error_formatters_reuse_api_error_message():
    formatter_files = [
        PROJECT_ROOT / "frontend/src/app/context/authActions.ts",
        PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx",
        PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx",
        PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx",
        PROJECT_ROOT / "frontend/src/app/views/targetProfileViewModel.ts",
    ]

    for path in formatter_files:
        source = path.read_text()
        assert "if (error instanceof ApiError) return error.message;" in source
        assert "JSON.parse(error.body)" not in source
        assert "error.body || error.message" not in source

    task_center_view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    task_error = task_center_view_model[
        task_center_view_model.index("export function errorMessage"):
        task_center_view_model.index("\n\nexport function words")
    ]
    assert "if (error.status === 408)" in task_error
    assert "return error.message;" in task_error
    assert "JSON.parse(error.body)" not in task_error


def test_message_send_row_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/context/messageActions.ts").read_text()
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    assert "handleActionError: (error: unknown) => void;" in source
    assert "handleActionError," in context[context.index("createMessageActions({"):context.index("});", context.index("createMessageActions({"))]
    for function_name in ["cancelTask", "dispatchTask", "drainQueue", "retryTask"]:
        function_body = source[source.index(f"async function {function_name}"):source.index("\n  async function" if function_name != "retryTask" else "\n\n  return", source.index(f"async function {function_name}"))]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "params.handleActionError(error);" in function_body
        assert "throw error;" not in function_body


def test_account_center_actions_surface_backend_error_detail():
    account_source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    message_source = (PROJECT_ROOT / "frontend/src/app/context/messageActions.ts").read_text()

    for source, function_name in [
        (account_source, "healthCheck"),
        (account_source, "syncAccountGroups"),
        (message_source, "createDirectMessageTask"),
    ]:
        next_function = "\n  async function"
        end_marker = next_function if function_name != "syncAccountGroups" else "\n\n  return"
        function_body = source[source.index(f"async function {function_name}"):source.index(end_marker, source.index(f"async function {function_name}") + 1)]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "handleActionError(error);" in function_body
        assert "throw error;" not in function_body
        assert "finally" in function_body


def test_account_center_detail_sync_clone_and_profile_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    handled_functions = [
        "openAccountDetail",
        "openAccountVerificationCodes",
        "openAccountMovePool",
        "openAccountPoolDetail",
        "refreshAccountPoolDetail",
        "createAccountPool",
        "moveCurrentAccountPool",
        "createClonePlan",
        "confirmClonePlan",
        "retryCloneItem",
        "refreshAccountDetail",
        "syncAccountContacts",
        "queueAccountSyncNow",
        "openGroupDetail",
        "pollVerificationCodes",
        "saveAccountProfile",
        "retryAccountProfileSync",
    ]
    busy_functions = set(handled_functions) - {"refreshAccountPoolDetail", "refreshAccountDetail"}

    for function_name in handled_functions:
        start = source.index(f"\n  async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        candidates = [index for index in [async_end, function_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "params.handleActionError(error);" in function_body
        assert "throw error;" not in function_body
        if function_name in busy_functions:
            assert "finally" in function_body
            assert "params.setBusy('');" in function_body


def test_account_detail_modal_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()

    assert "const [actionError, setActionError] = React.useState('');" in source
    assert "message={actionError}" in source
    for function_name in [
        "loadAvailabilitySummary",
        "rebuildAvailabilitySummary",
        "loadSecurityDetail",
        "refreshSecurityDetail",
        "createSingleSecurityBatch",
        "syncTargets",
        "manualSendNow",
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        const_end = source.find("\n  const", start + 1)
        candidates = [index for index in [async_end, function_end, const_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "catch (error)" in function_body
        assert "setActionError(error instanceof Error ? error.message" in function_body
        assert "throw error;" not in function_body


def test_account_detail_modal_availability_and_security_ignore_stale_account_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()

    assert "const activeAccountDetailId = React.useRef(accountDetail.account.id);" in source
    assert "function isActiveAccountDetail(accountId: number)" in source
    assert "activeAccountDetailId.current = accountDetail.account.id;" in source
    for function_name in [
        "loadAvailabilitySummary",
        "rebuildAvailabilitySummary",
        "loadSecurityDetail",
        "refreshSecurityDetail",
        "createSingleSecurityBatch",
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        const_end = source.find("\n  const", start + 1)
        candidates = [index for index in [async_end, function_end, const_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "const accountId = accountDetail.account.id;" in function_body
        assert "if (!isActiveAccountDetail(accountId)) return;" in function_body
        catch_block = function_body[function_body.index("catch (error)"):]
        assert catch_block.index("if (!isActiveAccountDetail(accountId)) return;") < catch_block.index("setActionError")
        finally_block = function_body[function_body.index("finally"):]
        assert "if (isActiveAccountDetail(accountId)) set" in finally_block


def test_account_detail_context_actions_ignore_stale_account_responses():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()

    assert "const accountDetailRequestRef = React.useRef({ accountId: null as number | null, seq: 0 });" in context
    assert "const accountPoolDetailRequestRef = React.useRef({ poolId: null as number | null, seq: 0 });" in context
    assert "const groupDetailRequestRef = React.useRef({ groupId: null as number | null, seq: 0 });" in context
    assert "accountDetailRequestRef," in context[context.index("const accountActions = createAccountActions"):context.index("});", context.index("const accountActions = createAccountActions"))]
    assert "accountPoolDetailRequestRef," in context[context.index("const accountActions = createAccountActions"):context.index("});", context.index("const accountActions = createAccountActions"))]
    assert "groupDetailRequestRef," in context[context.index("const accountActions = createAccountActions"):context.index("});", context.index("const accountActions = createAccountActions"))]
    assert "accountDetailRequestRef: { current: { accountId: number | null; seq: number } };" in source
    assert "accountPoolDetailRequestRef: { current: { poolId: number | null; seq: number } };" in source
    assert "groupDetailRequestRef: { current: { groupId: number | null; seq: number } };" in source
    assert "function beginAccountDetailRequest(accountId: number)" in source
    assert "function isActiveAccountDetailRequest(accountId: number, seq: number)" in source
    assert "function clearAccountDetailRequest(accountId: number, seq: number)" in source
    for function_name in ["openAccountDetail", "openAccountVerificationCodes", "openAccountMovePool", "refreshAccountDetail"]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        candidates = [index for index in [async_end, function_end] if index != -1]
        function_body = source[start:min(candidates)]
        stale_return = "if (!isActiveAccountDetailRequest(accountId, requestSeq)) return false;" if function_name == "openAccountDetail" else "if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;"
        assert "const accountId =" in function_body
        assert "const requestSeq = beginAccountDetailRequest(accountId);" in function_body
        assert stale_return in function_body
        catch_block = function_body[function_body.index("catch (error)"):]
        assert catch_block.index(stale_return) < catch_block.index("params.handleActionError(error);")
        assert "clearAccountDetailRequest(accountId, requestSeq);" in function_body[function_body.index("finally"):]

    for function_name, id_name, begin_name, active_name, clear_name in [
        ("openAccountPoolDetail", "poolId", "beginAccountPoolDetailRequest", "isActiveAccountPoolDetailRequest", "clearAccountPoolDetailRequest"),
        ("refreshAccountPoolDetail", "poolId", "beginAccountPoolDetailRequest", "isActiveAccountPoolDetailRequest", "clearAccountPoolDetailRequest"),
        ("openGroupDetail", "groupId", "beginGroupDetailRequest", "isActiveGroupDetailRequest", "clearGroupDetailRequest"),
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        candidates = [index for index in [async_end, function_end] if index != -1]
        function_body = source[start:min(candidates)]
        stale_return = f"if (!{active_name}({id_name}, requestSeq)) return false;" if function_name == "openGroupDetail" else f"if (!{active_name}({id_name}, requestSeq)) return;"
        assert f"const {id_name} =" in function_body
        assert f"const requestSeq = {begin_name}({id_name});" in function_body
        assert stale_return in function_body
        catch_block = function_body[function_body.index("catch (error)"):]
        assert catch_block.index(stale_return) < catch_block.index("params.handleActionError(error);")
        assert f"{clear_name}({id_name}, requestSeq);" in function_body[function_body.index("finally"):]

    refresh_action_group = source[source.index("async function refreshActionGroupDetail"):source.index("\n\n  function relatedDetailRefreshersForAction")]
    assert "const groupId = params.groupDetail.group.id;" in refresh_action_group
    assert "const requestSeq = beginGroupDetailRequest(groupId);" in refresh_action_group
    assert "if (!isActiveGroupDetailRequest(groupId, requestSeq)) return;" in refresh_action_group
    assert "clearGroupDetailRequest(groupId, requestSeq);" in refresh_action_group[refresh_action_group.index("finally"):]


def test_account_authorization_assets_panel_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()

    assert "const [error, setError] = React.useState('');" in source
    assert "message={error}" in source
    for function_name in [
        "loadAssets",
        "openLoginModal",
        "startStandbyLogin",
        "verifyStandbyLogin",
        "checkQrLogin",
        "switchPrimary",
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        const_end = source.find("\n  const", start + 1)
        candidates = [index for index in [async_end, function_end, const_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "catch (error)" in function_body
        assert "setError(error instanceof Error ? error.message" in function_body
        assert "throw error;" not in function_body


def test_account_authorization_assets_panel_ignores_stale_account_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()
    effect = source[source.index("React.useEffect(() => {"):source.index("\n  function isActiveAccount")]
    load_assets = source[source.index("async function loadAssets"):source.index("\n  async function openLoginModal")]
    complete_login = source[source.index("async function completeLoginModal"):source.index("\n  function confirmSwitch")]

    assert "const activeAccountId = React.useRef(accountId);" in source
    assert "function isActiveAccount(targetAccountId: number)" in source
    assert "activeAccountId.current = accountId;" in effect
    assert "setLoginOpen(false);" in effect
    assert "setLoginFlow(null);" in effect
    assert "setLoginLoading(false);" in effect
    assert "setSwitchingId(null);" in effect
    assert "async function loadAssets(targetAccountId = accountId): Promise<boolean>" in source
    assert "`/tg-accounts/${targetAccountId}/authorizations`" in load_assets
    assert "if (!isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) return false;" in load_assets
    for function_name in ["openLoginModal", "verifyStandbyLogin", "checkQrLogin"]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        candidates = [index for index in [async_end, function_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "const targetAccountId = accountId;" in function_body
        assert "if (!isActiveLoginSession(targetAccountId, loginSeq)) return;" in function_body
        catch_block = function_body[function_body.index("catch (error)"):]
        assert catch_block.index("if (!isActiveLoginSession(targetAccountId, loginSeq)) return;") < catch_block.index("setError")
        finally_block = function_body[function_body.index("finally"):]
        assert "if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);" in finally_block
    start_login = source[source.index("async function startStandbyLogin"):source.index("\n  async function verifyStandbyLogin")]
    assert "const targetAccountId = accountId;" in start_login
    assert "const payload = loginStartPayload;" in start_login
    assert "const payloadSignature = loginStartPayloadSignature;" in start_login
    assert "if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;" in start_login
    catch_block = start_login[start_login.index("catch (error)"):]
    assert catch_block.index("if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;") < catch_block.index("setError")
    finally_block = start_login[start_login.index("finally"):]
    assert "if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);" in finally_block
    switch_primary = source[source.index("async function switchPrimary"):source.index("\n  function closeLoginModal")]
    assert "const targetAccountId = accountId;" in switch_primary
    assert "if (!isActiveAccount(targetAccountId)) return;" in switch_primary
    catch_block = switch_primary[switch_primary.index("catch (error)"):]
    assert catch_block.index("if (!isActiveAccount(targetAccountId)) return;") < catch_block.index("setError")
    assert "if (isActiveAccount(targetAccountId)) setSwitchingId(null);" in switch_primary[switch_primary.index("finally"):]
    assert "async function completeLoginModal(targetAccountId: number, loginSeq: number)" in source
    assert "const loaded = await loadAssets(targetAccountId);" in complete_login
    assert "if (!loaded || !isActiveLoginSession(targetAccountId, loginSeq)) return;" in complete_login
    assert "onClick={() => void loadAssets()}" in source


def test_authorization_assets_loads_bind_account_and_request_sequence():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()
    load_assets = source[source.index("async function loadAssets"):source.index("\n  async function openLoginModal")]
    complete_login = source[source.index("async function completeLoginModal"):source.index("\n  function confirmSwitch")]
    switch_primary = source[source.index("async function switchPrimary"):source.index("\n  function closeLoginModal")]

    assert "const authorizationAssetsRequestRef = React.useRef({ accountId, seq: 0 });" in source
    assert "function beginAuthorizationAssetsRequest(targetAccountId: number)" in source
    assert "function isActiveAuthorizationAssetsRequest(targetAccountId: number, requestSeq: number)" in source
    assert "authorizationAssetsRequestRef.current.accountId === targetAccountId" in source
    assert "authorizationAssetsRequestRef.current.seq === requestSeq" in source

    assert "const requestSeq = beginAuthorizationAssetsRequest(targetAccountId);" in load_assets
    assert "if (!isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) return false;" in load_assets
    assert load_assets.index("if (!isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) return false;") < load_assets.index("setAssets(nextAssets);")
    assert "if (isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) setLoading(false);" in load_assets

    assert "const loaded = await loadAssets(targetAccountId);" in complete_login
    assert "const loaded = await loadAssets(targetAccountId);" in switch_primary
    assert "onClick={() => void loadAssets()}" in source


def test_account_authorization_login_modal_ignores_stale_sessions():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()
    open_login = source[source.index("async function openLoginModal"):source.index("\n  async function startStandbyLogin")]
    start_login = source[source.index("async function startStandbyLogin"):source.index("\n  async function verifyStandbyLogin")]
    verify_login = source[source.index("async function verifyStandbyLogin"):source.index("\n  async function checkQrLogin")]
    check_qr = source[source.index("async function checkQrLogin"):source.index("\n  async function completeLoginModal")]
    close_modal = source[source.index("function closeLoginModal"):source.index("\n  function assetForRole")]
    modal_start = source.index('title="新增备用授权"')
    modal = source[modal_start:source.index("\n      </Modal>", modal_start)]

    assert "const loginSessionSeq = React.useRef(0);" in source
    assert "function beginLoginSession()" in source
    assert "function currentLoginSession()" in source
    assert "function isActiveLoginSession(targetAccountId: number, loginSeq: number)" in source
    assert "function closeLoginModal()" in source
    assert "loginSessionSeq.current += 1;" in close_modal
    assert "setLoginOpen(false);" in close_modal
    assert "setLoginFlow(null);" in close_modal
    assert "onCancel={closeLoginModal}" in modal
    assert "const loginSeq = beginLoginSession();" in open_login
    assert "const loginSeq = currentLoginSession();" in start_login
    for block in [open_login, verify_login, check_qr]:
        assert "if (!isActiveLoginSession(targetAccountId, loginSeq)) return;" in block
        catch_block = block[block.index("catch (error)"):]
        assert catch_block.index("if (!isActiveLoginSession(targetAccountId, loginSeq)) return;") < catch_block.index("setError")
        finally_block = block[block.index("finally"):]
        assert "if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);" in finally_block
    assert "function isActiveLoginStart(targetAccountId: number, loginSeq: number, payloadSignature: string)" in source
    assert "latestLoginStartPayloadSignature.current === payloadSignature" in source
    assert "if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;" in start_login
    catch_block = start_login[start_login.index("catch (error)"):]
    assert catch_block.index("if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;") < catch_block.index("setError")
    finally_block = start_login[start_login.index("finally"):]
    assert "if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);" in finally_block
    assert "await completeLoginModal(targetAccountId, loginSeq);" in verify_login
    assert "await completeLoginModal(targetAccountId, loginSeq);" in check_qr
    assert "async function completeLoginModal(targetAccountId: number, loginSeq: number)" in source


def test_managed_2fa_panel_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()

    assert "const [error, setError] = React.useState('');" in source
    assert "message={error}" in source
    start = source.index("async function saveManagedPassword")
    end = source.index("\n\n  return", start)
    save_body = source[start:end]
    assert "catch (error)" in save_body
    assert "setError(error instanceof Error ? error.message" in save_body
    assert "throw error;" not in save_body


def test_managed_2fa_panel_ignores_stale_account_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    effect = source[source.index("React.useEffect(() => {"):source.index("\n  function isActiveAccount")]
    save_body = source[source.index("async function saveManagedPassword"):source.index("\n\n  return")]

    assert "type Managed2FaAction = 'save' | 'rotate' | 'reveal';" in source
    assert "function managed2FaPath(accountId: number, action: Managed2FaAction)" in source
    assert "const activeAccountId = React.useRef(accountId);" in source
    assert "function isActiveAccount(targetAccountId: number)" in source
    assert "activeAccountId.current = accountId;" in effect
    assert "setPassword('');" in effect
    assert "setReason('');" in effect
    assert "async function saveManagedPassword(action: Managed2FaAction)" in source
    assert "const targetAccountId = accountId;" in save_body
    assert "const payload = managed2FaPayload;" in save_body
    assert "const payloadSignature = managed2FaPayloadSignature;" in save_body
    assert "const path = managed2FaPath(targetAccountId, action);" in save_body
    assert "if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;" in save_body
    catch_block = save_body[save_body.index("catch (error)"):]
    assert catch_block.index("if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;") < catch_block.index("setError")
    finally_block = save_body[save_body.index("finally"):]
    assert "if (isCurrentManaged2FaRequest(targetAccountId, action, requestSeq)) setLoading(false);" in finally_block
    assert "onClick={() => saveManagedPassword(`/tg-accounts/${accountId}/security/managed-2fa`)}" not in source
    assert "onClick={() => saveManagedPassword('save')}" in source
    assert "onClick={() => saveManagedPassword('rotate')}" in source
    assert "onClick={() => void revealManagedPassword()}" in source


def test_managed_2fa_panel_ignores_stale_same_account_actions():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    save_body = source[source.index("async function saveManagedPassword"):source.index("\n\n  return")]

    assert "const managed2FaRequestRef = React.useRef({ accountId, action: '' as Managed2FaAction | '', seq: 0 });" in source
    assert "const latestManaged2FaPayloadSignature = React.useRef('');" in source
    assert "const managed2FaPayload = React.useMemo(() => ({" in source
    assert "const managed2FaPayloadSignature = React.useMemo(() => JSON.stringify(managed2FaPayload), [managed2FaPayload]);" in source
    assert "latestManaged2FaPayloadSignature.current = managed2FaPayloadSignature;" in source
    assert "function beginManaged2FaRequest(targetAccountId: number, action: Managed2FaAction)" in source
    assert "function isCurrentManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number)" in source
    assert "function isActiveManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number, payloadSignature: string)" in source
    assert "managed2FaRequestRef.current.accountId === targetAccountId" in source
    assert "managed2FaRequestRef.current.action === action" in source
    assert "managed2FaRequestRef.current.seq === requestSeq" in source
    assert "latestManaged2FaPayloadSignature.current === payloadSignature" in source

    assert "const requestSeq = beginManaged2FaRequest(targetAccountId, action);" in save_body
    assert "const payloadSignature = managed2FaPayloadSignature;" in save_body
    assert "if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;" in save_body
    assert save_body.index("if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;") < save_body.index("setPassword('');")
    catch_block = save_body[save_body.index("catch (error)"):]
    assert catch_block.index("if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;") < catch_block.index("setError")
    finally_block = save_body[save_body.index("finally"):]
    assert "if (isCurrentManaged2FaRequest(targetAccountId, action, requestSeq)) setLoading(false);" in finally_block


def test_group_and_archive_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    for function_name in [
        "authorizeSelectedGroup",
        "createArchive",
        "saveGroupPolicy",
        "openArchiveDetail",
        "exportArchive",
        "rerunArchive",
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n\n  async function", start + 1)
        function_end = source.find("\n\n  function", start + 1)
        candidates = [index for index in [async_end, function_end] if index != -1]
        end = min(candidates)
        function_body = source[start:end]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "handleActionError(error);" in function_body
        assert "throw error;" not in function_body
        assert "finally" in function_body


def test_archives_view_create_archive_from_target_surfaces_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/ArchivesView.tsx").read_text()

    assert "const [createError, setCreateError] = React.useState('');" in source
    assert "message=\"归档创建失败\"" in source
    assert "description={createError}" in source
    start = source.index("async function createArchiveFromTarget")
    end = source.index("\n\n  return", start)
    function_body = source[start:end]
    assert "catch (error)" in function_body
    assert "setCreateError(error instanceof Error ? error.message" in function_body
    assert "throw error;" not in function_body


def test_content_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/context/contentActions.ts").read_text()
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    assert "handleActionError: (error: unknown) => void;" in source
    assert "handleActionError," in context[context.index("createContentActions({"):context.index("});", context.index("createContentActions({"))]
    for function_name in [
        "createMaterial",
        "saveMaterial",
        "disableMaterial",
        "restoreMaterial",
        "createContentKeywordRule",
        "saveContentKeywordRule",
    ]:
        start = source.index(f"\n  async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        return_end = source.find("\n  return", start + 1)
        candidates = [index for index in [async_end, function_end, return_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "params.handleActionError(error);" in function_body
        assert "throw error;" not in function_body
        assert "finally" in function_body


def test_system_prompt_and_token_ledger_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()

    for function_name in [
        "loadUserTokenLedgers",
        "createPromptTemplate",
        "savePromptTemplate",
    ]:
        start = source.index(f"\n  async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        return_end = source.find("\n  return", start + 1)
        candidates = [index for index in [async_end, function_end, return_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "try {" in function_body
        assert "catch (error)" in function_body
        assert "params.handleActionError(error);" in function_body
        assert "throw error;" not in function_body
        assert "finally" in function_body


def test_admin_user_token_ledgers_are_cleared_before_loading_selected_user():
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    open_edit = source[source.index("function openAdminUserEdit"):source.index("\n\n  function openAdminUserCreate")]
    load_ledgers = source[source.index("async function loadUserTokenLedgers"):source.index("\n\n  async function toggleDeveloperApp")]

    assert "params.setSelectedAdminUserId(user.id);" in open_edit
    assert "void loadUserTokenLedgers(user.id);" in open_edit
    assert "params.setSelectedAdminUserId(userId);" in load_ledgers
    assert "params.setSelectedUserTokenLedgers([]);" in load_ledgers
    assert load_ledgers.index("params.setSelectedUserTokenLedgers([]);") < load_ledgers.index("const ledgers = await api<TokenLedger[]>")
    assert load_ledgers.index("params.setSelectedUserTokenLedgers([]);") < load_ledgers.index("catch (error)")


def test_system_config_top_refresh_surfaces_tab_loader_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    start = source.index("async function refreshCurrentView")
    end = source.index("\n\n  React.useEffect(() => {\n    if (!notice)", start)
    function_body = source[start:end]

    assert "try {" in function_body
    assert "catch (error)" in function_body
    assert "const prefix = activeView === 'systemConfig' ? '系统设置数据读取异常' : '刷新当前数据失败';" in function_body
    assert "setNotice(`${prefix}：${error instanceof Error ? error.message : String(error)}`);" in function_body
    assert "await loadSystemConfigTabData(systemConfigTab, requestSeq);" in function_body
    assert "throw error;" not in function_body


def test_system_config_tab_lazy_loads_bind_tab_and_request_sequence():
    source = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    loader_start = source.index("const loadSystemConfigTabData = React.useCallback")
    loader_end = source.index("\n\n  React.useEffect(() => {\n    if (!token || activeView !== 'systemConfig') return;", loader_start)
    loader_body = source[loader_start:loader_end]
    effect_start = loader_end
    effect_body = source[effect_start:source.index("\n\n  async function refreshCurrentView", effect_start)]
    refresh_body = source[source.index("async function refreshCurrentView"):source.index("\n\n  React.useEffect(() => {\n    if (!notice)", loader_start)]

    assert "const systemConfigTabRequestRef = React.useRef({ tab: '', seq: 0 });" in source
    assert "function beginSystemConfigTabRequest(tab: string)" in source
    assert "function isActiveSystemConfigTabRequest(tab: string, requestSeq: number)" in source
    assert "return activeView === 'systemConfig' && systemConfigTab === tab && systemConfigTabRequestRef.current.tab === tab && systemConfigTabRequestRef.current.seq === requestSeq;" in source

    assert "requestSeq: number" in loader_body
    assert "const loadDeveloperConfig = React.useCallback(async (requestSeq: number)" in source
    assert "const loadAiProviderConfig = React.useCallback(async (requestSeq: number)" in source
    assert "const loadResourceConfig = React.useCallback(async (requestSeq: number)" in source
    assert "if (!isActiveSystemConfigTabRequest('developer-apps', requestSeq)) return;" in source
    assert "if (!isActiveSystemConfigTabRequest('ai-providers', requestSeq)) return;" in source
    assert "if (!isActiveSystemConfigTabRequest('resources', requestSeq)) return;" in source
    assert "if (tab === 'admin-users' && hasPermission(currentUser, 'permissions.view'))" in loader_body
    assert "const adminRows = await api<AdminUser[]>('/admin/users');" in loader_body
    assert "if (!isActiveSystemConfigTabRequest(tab, requestSeq)) return;" in loader_body
    assert loader_body.index("const adminRows = await api<AdminUser[]>('/admin/users');") < loader_body.index("if (!isActiveSystemConfigTabRequest(tab, requestSeq)) return;")
    assert loader_body.index("if (!isActiveSystemConfigTabRequest(tab, requestSeq)) return;") < loader_body.index("setAdminUsers(adminRows);")

    assert "const requestSeq = beginSystemConfigTabRequest(systemConfigTab);" in effect_body
    assert "loadSystemConfigTabData(systemConfigTab, requestSeq).catch((error)" in effect_body
    assert "if (!isActiveSystemConfigTabRequest(systemConfigTab, requestSeq)) return;" in effect_body
    assert "const requestSeq = beginSystemConfigTabRequest(systemConfigTab);" in refresh_body
    assert "await loadSystemConfigTabData(systemConfigTab, requestSeq);" in refresh_body


def test_ai_voice_profile_manage_permission_is_assignable():
    account_masks_source = (PROJECT_ROOT / "frontend/src/app/views/AccountMasksView.tsx").read_text()
    app_modals_source = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()

    assert "hasPermission(currentUser, 'ai_voice_profiles.manage')" in account_masks_source
    assert "['ai_voice_profiles.manage', '账号面具管理']" in app_modals_source
    assert "['account_masks.view', '账号面具']" in app_modals_source
    assert "['account_environment.manage', '账号环境管理']" in app_modals_source


def test_ai_voice_profile_write_routes_have_explicit_manage_guard():
    source = (PROJECT_ROOT / "backend/app/api/routers/ai_config.py").read_text()
    guarded_routes = [
        "patch_ai_account_voice_profile",
        "rebuild_ai_account_voice_profile",
        "rollback_ai_account_voice_profile",
        "batch_rebuild_ai_account_voice_profiles",
        "batch_update_ai_account_voice_profile_status",
    ]

    assert "AI_VOICE_PROFILE_MANAGE_PERMISSION = \"ai_voice_profiles.manage\"" in source
    for route_name in guarded_routes:
        route_start = source.index(f"def {route_name}")
        next_route_start = source.find("\n\n@router.", route_start + 1)
        route_source = source[route_start:next_route_start if next_route_start != -1 else len(source)]
        assert "_require_voice_profile_manage(current_user)" in route_source


def test_blob_export_fetches_use_api_error_for_backend_detail():
    client_source = (PROJECT_ROOT / "frontend/src/shared/api/client.ts").read_text()
    audit_source = (PROJECT_ROOT / "frontend/src/app/views/AuditsView.tsx").read_text()
    rules_source = (PROJECT_ROOT / "frontend/src/app/views/RulesCenterView.tsx").read_text()
    task_source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "export async function apiErrorFromResponse(response: Response)" in client_source
    assert "return new AuthExpiredError(response.status, text);" in client_source
    assert "apiErrorFromResponse" in audit_source[audit_source.index("from '../../shared/api/client'") - 80:audit_source.index("from '../../shared/api/client'")]
    assert "apiErrorFromResponse" in rules_source[rules_source.index("from '../../shared/api/client'") - 80:rules_source.index("from '../../shared/api/client'")]
    assert "apiErrorFromResponse" in task_source[task_source.index("from '../../shared/api/client'") - 100:task_source.index("from '../../shared/api/client'")]
    for source in [audit_source, rules_source, task_source]:
        assert "throw await apiErrorFromResponse(response);" in source
        assert "new ApiError(response.status" not in source
        assert "throw new Error(await response.text())" not in source


def test_rules_center_bound_tasks_ignore_stale_rule_set_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/RulesCenterView.tsx").read_text()
    open_bound = source[source.index("async function openBoundTasks"):source.index("\n  const table")]
    close_bound = source[source.index("function closeBoundTasks"):source.index("\n  const table")]
    modal_start = source.index("title={boundTaskTarget ? `绑定任务：${boundTaskTarget.name}` : '绑定任务'}")
    modal_end = source.index("\n      <Modal", modal_start + 1)
    modal = source[modal_start:modal_end]

    assert "const activeBoundTaskRuleSetId = React.useRef<number | null>(null);" in source
    assert "const [boundTaskLoading, setBoundTaskLoading] = React.useState(false);" in source
    assert "function isActiveBoundTaskRuleSet(ruleSetId: number)" in source
    assert "activeBoundTaskRuleSetId.current = ruleSet.id;" in open_bound
    assert "if (!isActiveBoundTaskRuleSet(ruleSet.id)) return;" in open_bound
    assert "if (isActiveBoundTaskRuleSet(ruleSet.id)) setBoundTaskLoading(false);" in open_bound
    catch_block = open_bound[open_bound.index("catch (err)"):]
    assert catch_block.index("if (!isActiveBoundTaskRuleSet(ruleSet.id)) return;") < catch_block.index("setError")
    assert "activeBoundTaskRuleSetId.current = null;" in close_bound
    assert "setBoundTasks([]);" in close_bound
    assert "onCancel={closeBoundTasks}" in modal
    assert "loading={boundTaskLoading}" in modal


def test_audit_export_modal_surfaces_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/AuditsView.tsx").read_text()
    export_body = source[source.index("async function exportCsv"):source.index("\n\n  return (")]

    assert "App as AntdApp" in source
    assert "const { message } = AntdApp.useApp();" in source
    assert "function errorText(error: unknown)" in source
    assert "catch (error)" in export_body
    assert "void message.error(`导出审计记录失败：${errorText(error)}`);" in export_body
    assert "onOk={() => void exportCsv(exportReason)}" not in source
    assert "onOk={() => exportCsv(exportReason)}" in source


def test_topbar_exposes_change_password_entry_separate_from_logout():
    source = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    top_actions = source[source.index('<Space className="top-actions">'):source.index("</Space>", source.index('<Space className="top-actions">'))]

    assert "onClick={() => setModal({ type: 'changePassword' })}" in top_actions
    assert "修改密码" in top_actions
    assert "onClick={logout}" in top_actions
    assert "icon={<RefreshCcw size={16} />} onClick={logout}" in top_actions


def test_frontend_data_loaders_do_not_silently_fake_empty_success():
    checked_files = [
        "frontend/src/app/context/refresh.ts",
        "frontend/src/app/views/OverviewView.tsx",
        "frontend/src/app/hooks/useOverviewOperationData.ts",
        "frontend/src/app/views/RulesCenterView.tsx",
        "frontend/src/app/views/ArchivesView.tsx",
        "frontend/src/app/context/accountActions.ts",
    ]

    combined = "\n".join((PROJECT_ROOT / path).read_text() for path in checked_files)
    assert "Promise.allSettled" not in combined
    assert ".catch(() => [])" not in combined
    assert ".catch(() => ({}" not in combined
    assert ".catch(() => undefined)" not in combined


def test_overview_issue_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx").read_text()
    submit_issue_action = source[source.index("async function submitIssueAction"):source.index("\n  const failedActionColumns")]
    open_issue_detail = source[source.index("async function openIssueDetail"):source.index("\n  async function submitIssueAction")]

    assert "import { api, ApiError }" in source
    assert "function errorText(error: unknown)" in source
    assert "catch (error)" in submit_issue_action
    assert "message.error(`${actionLabel}失败：${errorText(error)}`)" in submit_issue_action
    assert "catch (error)" in open_issue_detail
    assert "message.error(`读取目标异常失败：${errorText(error)}`)" in open_issue_detail


def test_overview_issue_detail_and_actions_ignore_stale_issue_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx").read_text()
    open_issue_detail = source[source.index("async function openIssueDetail"):source.index("\n  async function submitIssueAction")]
    close_issue_drawer = source[source.index("function closeIssueDrawer"):source.index("\n  function openIssueAction")]
    submit_issue_action = source[source.index("async function submitIssueAction"):source.index("\n  const failedActionColumns")]
    drawer_start = source.index("title={issueDetail?.issue ? `目标异常 ${issueDetail.issue.id}` : '目标异常'}")
    drawer = source[drawer_start:source.index("\n      </Drawer>", drawer_start)]

    assert "const activeIssueDetailId = React.useRef<string | null>(null);" in source
    assert "function isActiveIssueDetail(issueId: string)" in source
    assert "activeIssueDetailId.current = issueId;" in open_issue_detail
    assert "if (!isActiveIssueDetail(issueId)) return;" in open_issue_detail
    assert "if (isActiveIssueDetail(issueId)) setIssueLoading(false);" in open_issue_detail
    assert "onClose={closeIssueDrawer}" in drawer
    assert "activeIssueDetailId.current = null;" in close_issue_drawer
    assert "setIssueDetail(null);" in close_issue_drawer
    assert "const issueId = issue.id;" in submit_issue_action
    assert "if (!isActiveIssueDetail(issueId)) return;" in submit_issue_action
    catch_block = submit_issue_action[submit_issue_action.index("catch (error)"):]
    assert catch_block.index("if (!isActiveIssueDetail(issueId)) return;") < catch_block.index("message.error")
    assert "if (requestSeq ? isCurrentIssueActionRequest(requestSeq) : isActiveIssueDetail(issueId)) setIssueBusy('');" in submit_issue_action


def test_group_create_task_target_lookup_surfaces_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    start = source.index("async function openTaskFromGroup")
    open_task = source[start:source.index("\n  const captchaControl", start)]

    assert "import { api, ApiError }" in source
    assert "function errorText(error: unknown)" in source
    assert "if (error instanceof ApiError) return error.message;" in source
    assert "catch (error)" in open_task
    assert "message.warning(`读取运营目标失败：${errorText(error)}。请在任务中心手动选择目标。`)" in open_task


def test_risk_control_proxy_write_actions_require_proxy_manage_permission():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()

    assert "canManageProxies?: boolean" in source
    assert "canManageRisk?: boolean" in source
    assert "canManageProxies={hasPermission(currentUser, 'proxies.manage')}" in app_shell
    assert "canManageRisk={hasPermission(currentUser, 'risk.manage')}" in app_shell

    disposition_start = source.index("function renderDispositionActions")
    disposition_end = source.index("\n  const accountColumns", disposition_start)
    disposition_block = source[disposition_start:disposition_end]
    assert "if ((alertId || proxyId) && !canManageProxies)" in disposition_block
    assert disposition_block.index("!canManageProxies") < disposition_block.index("handleProxyAlert")
    assert disposition_block.index("!canManageProxies") < disposition_block.index("checkProxy(proxyId)")

    assert "render: (_, row) => canManageProxies && row.id" in source
    assert "if (!canManageProxies) return null" in source


def test_risk_control_proxy_disable_surfaces_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    start = source.index("async function disableProxy")
    end = source.index("\n  async function handleProxyAlert", start)
    disable_proxy = source[start:end]

    assert "onOk: async () => {" in disable_proxy
    assert "try {" in disable_proxy
    assert "catch (exc)" in disable_proxy
    assert "setError(exc instanceof Error ? exc.message : '禁用代理失败');" in disable_proxy
    assert "throw exc;" not in disable_proxy
    assert "{canManageProxies && <Button type=\"primary\" onClick={openProxyCreate}>新增代理资源</Button>}" in source
    assert "{canManageRisk && <Button type=\"primary\" onClick={openPolicyEdit}>编辑全局策略</Button>}" in source


def test_risk_control_account_scores_show_score_reasons():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()

    assert "扣分原因" in source
    assert "非扣分失败" in source
    assert "score_reasons" in source
    assert "non_score_reasons" in source
    assert "row.score_reasons" in source
    assert "row.non_score_reasons" in source


def test_risk_control_hit_records_label_failure_reason_not_generic_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    hit_columns = source[source.index("const hitColumns"):source.index("const proxyColumns")]

    assert "title: '失败原因'" in hit_columns
    assert "title: '详情'" not in hit_columns


def test_risk_control_exposes_policy_audit_tab():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    tabs_block = source[source.index("items={["):source.index("\n          ]}", source.index("items={["))]

    assert "type RiskPolicyAudit" in source
    assert "key: 'policy-audit'" in tabs_block
    assert "label: '策略审计'" in tabs_block
    assert "dataSource={policyAuditTable.filteredRows}" in source
    assert "搜索策略动作 / 操作人 / 对象 / 原因" in source


def test_risk_control_proxy_resources_and_alerts_are_separate_tabs():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    tabs_block = source[source.index("items={["):source.index("\n          ]}", source.index("items={["))]
    proxy_tab = source[source.index("key: 'proxy'"):source.index("key: 'proxy-alerts'")]
    proxy_alert_tab = source[source.index("key: 'proxy-alerts'"):source.index("\n      {error &&")]

    assert "key: 'proxy'" in tabs_block
    assert "label: '代理资源'" in tabs_block
    assert "key: 'proxy-alerts'" in tabs_block
    assert "label: '代理告警'" in tabs_block
    assert "setActiveTab('proxy-alerts')" in source
    assert "dataSource={summary?.proxy_alerts ?? []}" not in proxy_tab
    assert "dataSource={summary?.proxy_alerts ?? []}" in proxy_alert_tab


def test_risk_control_proxy_alert_ignore_has_reason_and_expiry_modal():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    proxy_alert_tab = source[source.index("key: 'proxy-alerts'"):source.index("\n      {error &&")]

    assert "type ProxyAlertAction = 'acknowledge' | 'ignore' | 'resolve'" in source
    assert "function openProxyAlertIgnore(alertId: number)" in source
    assert "async function submitProxyAlertIgnore()" in source
    assert "ignored_until: proxyAlertIgnoreUntil" in source
    assert "忽略原因" in source
    assert "忽略到期时间" in source
    assert "onClick={() => openProxyAlertIgnore(row.id!)}" in source
    assert ">忽略</Button>" in source


def test_risk_control_overview_cards_and_account_actions_are_deep_linked():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    context_types = (PROJECT_ROOT / "frontend/src/app/context/types.ts").read_text()
    open_account_detail = app_shell[
        app_shell.index("async function openAccountDetailFromOperation"):
        app_shell.index("\n  async function openTaskFromGroup")
    ]

    assert "goToView: (viewId: string, search?: string) => void" in context_types
    assert "type AccountDeepLinkContext" in app_shell
    assert "function accountDetailTabSlug(tab: string)" in app_shell
    assert "function accountDetailSearch(accountId: number, tab: string, context: AccountDeepLinkContext)" in app_shell
    assert "if (/验证待处理|待处理/.test(tab)) return 'verification';" in app_shell
    assert "if (tab === 'verification') return '验证待处理';" in app_shell
    assert "params.set('account_id', String(accountId))" in app_shell
    assert "params.set('return_to', 'risk-control')" in app_shell
    assert "params.set('tab', accountDetailTabSlug(tab))" in app_shell
    assert "params.set('issue', context.issue)" in app_shell
    assert "params.set('risk_tab', context.riskTab)" in app_shell
    assert "params.set('risk_query', context.riskQuery)" in app_shell
    assert "params.set('risk_page', String(context.riskPage))" in app_shell
    assert "params.set('risk_page_size', String(context.riskPageSize))" in app_shell
    assert "params.set('risk_quick_filter', context.riskQuickFilter)" in app_shell
    assert "onOpenAccountDetail?: (accountId: number, tab?: string, context?: AccountDetailContext) => void" in source
    assert "function handleMetricClick(metric: RiskControlMetric)" in source
    assert "onClick={() => handleMetricClick(metric)}" in source
    assert "function accountDetailContextFor(row: RiskControlAccountScore)" in source
    assert "function riskTableReturnContext()" in source
    assert "function openAccountCenter(row: RiskControlAccountScore)" in source
    assert "onOpenAccountDetail(row.account_id, accountDetailTabFor(row), accountDetailContextFor(row))" in source
    assert "操作" in source[source.index("const accountColumns"):source.index("const queueColumns")]
    assert "width: 190" in source[source.index("title: '操作'"):source.index("const queueColumns")]
    assert "onOpenAccountDetail={openAccountDetailFromOperation}" in app_shell
    assert "goToView('accounts', accountDetailSearch(accountId, tab, context))" in open_account_detail
    assert open_account_detail.index("goToView('accounts'") < open_account_detail.index("await openAccountDetail")


def test_group_create_task_uses_peer_id_when_target_link_is_missing():
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    open_task = app_shell[
        app_shell.index("async function openTaskFromGroup"):
        app_shell.index("\n  const captchaControl", app_shell.index("async function openTaskFromGroup"))
    ]

    assert "const group = groups.find((item) => item.id === groupId);" in open_task
    assert "targets.find((item) => item.linked_group_id === groupId)" in open_task
    assert "targets.find((item) => group?.tg_peer_id && item.tg_peer_id === group.tg_peer_id)" in open_task
    assert open_task.index("linked_group_id === groupId") < open_task.index("item.tg_peer_id === group.tg_peer_id")


def test_account_center_consumes_account_deep_link_query_on_load():
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    app_modals = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    account_modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    deep_link_effect = app_shell[
        app_shell.index("if (activeView !== 'accounts') return;"):
        app_shell.index("\n  const loginReady", app_shell.index("if (activeView !== 'accounts') return;"))
    ]

    assert "import { useLocation } from 'react-router-dom'" in app_shell
    assert "function accountDetailTabLabel(tab: string)" in app_shell
    assert "const accountDeepLinkRef = React.useRef('')" in app_shell
    assert "const location = useLocation()" in app_shell
    assert "new URLSearchParams(location.search)" in app_shell
    assert "params.get('account_id')" in app_shell
    assert "accountDetailTabLabel(params.get('tab') || 'availability')" in app_shell
    assert "if (activeView !== 'accounts')" in app_shell
    assert "void openAccountDetail" in deep_link_effect
    assert ".then((opened) => {" in deep_link_effect
    assert "if (opened) setAccountDetailTab(tab);" in deep_link_effect
    assert ".catch((error) => {" in deep_link_effect
    assert "setNotice(`读取账号 ${accountId} 详情失败：${errorText(error)}`);" in deep_link_effect
    assert "import { useLocation } from 'react-router-dom'" in app_modals
    assert "function riskControlReturnSearch(search: string)" in app_modals
    assert "const returnToRiskControl = new URLSearchParams(location.search).get('return_to') === 'risk-control'" in app_modals
    assert "ctx.goToView('riskControl', riskControlReturnSearch(location.search));" in app_modals
    assert "onReturnToRiskControl?: () => void" in account_modals
    assert "返回风控中心" in account_modals
    assert "<Space className=\"modal-title-actions\"" in account_modals
    assert "{onReturnToRiskControl && <Button onClick={onReturnToRiskControl}>返回风控中心</Button>}" in account_modals


def test_task_center_running_and_paused_states_are_visually_distinct():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    styles = (PROJECT_ROOT / "frontend/src/styles/components.css").read_text()
    start_button = source[source.index("{canManageTasks && canStartTask(task)"):source.index("{canManageTasks && canPauseTask(task)")]
    pause_button = source[source.index("{canManageTasks && canPauseTask(task)"):source.index("{canManageTasks && !isSystemTask(task)")]

    assert "className={`task-status-indicator task-status-${task?.status || status || 'unknown'}`}" in source
    assert 'type="primary"' in start_button
    assert 'danger' in pause_button
    assert ".task-status-indicator.task-status-running" in styles
    assert ".task-status-indicator.task-status-paused" in styles


def test_task_center_ai_chat_account_distribution_controls_are_visible():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    detail = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert 'name="participation_rate"' in source
    assert 'label="参与账号比例"' in source
    assert 'name="allow_account_repeat"' in source
    assert 'label="允许账号重复发言"' in source
    assert 'label="每轮总发言数"' in source
    assert '<InputNumber min={1} max={10}' not in source
    assert "小时上限控制总量" in source
    assert "当天未参与账号会优先补齐" in source
    assert "function markMessagesPerRoundManual" in source
    assert "setFieldValue('messages_per_round_mode', 'manual')" in source
    assert "onChange={markMessagesPerRoundManual}" in source
    assert "准入策略" in source
    assert 'name="account_coverage_mode"' in source
    assert 'label="全账号日覆盖模式"' in source
    assert 'name="per_account_daily_min_messages"' in source
    assert 'name="per_account_daily_max_messages"' in source
    assert "account_coverage_mode: values.account_coverage_mode ?? 'all_accounts_daily'" in view
    assert "'account_coverage_mode'" in view_model
    assert "'per_account_daily_min_messages'" in view_model
    assert "'per_account_daily_max_messages'" in view_model
    assert "target_account_count" in types
    assert "blocked_reasons" in types
    assert "estimated_completion_window" in types
    assert "pending_accounts" in types
    assert "预计补齐窗口" in detail
    assert "阻塞原因" in detail
    assert "近期待补账号" in detail
    assert 'name="auto_join_target"' in source
    assert 'name="auto_follow_required_channel"' in source
    assert 'name="auto_resolve_verification"' in source
    assert 'name="ai_assisted_verification"' in source
    assert 'name="captcha_failure_policy"' in source
    assert 'name="membership_max_concurrent"' in source
    assert "auto_join_target: values.auto_join_target !== false" in view
    assert "'membership_max_concurrent'" in view_model
    assert 'label="上下文历史条数（不是账号数）"' in source
    assert 'label="账号并发上限（账号数）"' in source


def test_frontend_exposes_group_membership_admission_task_type():
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "'group_membership_admission'" in types
    assert "群聊准入任务" in view_model
    assert "group_membership_admission: '/tasks/group-membership-admission'" in view_model
    assert "account_group_ids" in view_model
    assert "admission_pacing" in view
    assert "test_message" in view
    assert "membership_admission_items" in types
    assert "membership_admission_phase" in types
    assert "delete_after_send" in wizard


def test_frontend_tenant_group_rescue_save_is_separate_from_quota_save():
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    quota_start = source.index("async function saveTenantQuota()")
    rescue_start = source.index("async function saveTenantGroupRescueSettings")
    rescue_end = source.index("function openAdminUserEdit", rescue_start)
    quota_body = source[quota_start:rescue_start]
    rescue_body = source[rescue_start:rescue_end]

    assert "/tenant-group-rescue-settings" not in quota_body
    assert "/tenant-group-rescue-settings" in rescue_body
    assert "保存群聊救援配置" in rescue_body
    assert "try {" in rescue_body
    assert "catch (error)" in rescue_body
    assert "params.handleActionError(error)" in rescue_body
    assert "finally" in rescue_body
    assert "params.setBusy('')" in rescue_body


def test_tenant_edit_modal_does_not_embed_group_rescue_form():
    source = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    start = source.index("modal?.type === 'tenantEdit'")
    end = source.index("modal?.type === 'adminUserEdit'", start)
    tenant_modal = source[start:end]

    assert "启用群聊救援" not in tenant_modal
    assert "救援管理员账号" not in tenant_modal
    assert "救援机器人 username" not in tenant_modal


def test_system_config_exposes_group_rescue_as_top_level_tab():
    system_config = (PROJECT_ROOT / "frontend/src/app/views/SystemConfigView.tsx").read_text()
    rescue_view = (PROJECT_ROOT / "frontend/src/app/views/GroupRescueSettingsView.tsx").read_text()
    developer_apps = (PROJECT_ROOT / "frontend/src/app/views/DeveloperAppsView.tsx").read_text()

    assert "showTenants={false}" in system_config
    assert "key: 'group-rescue'" in system_config
    assert "label: '群聊救援配置'" in system_config
    assert "<GroupRescueSettingsView" in system_config
    assert "onSaveGroupRescueSettings={onSaveGroupRescueSettings}" in system_config
    assert "canManageGroupRescue={hasPermission(currentUser, 'system.manage')}" in system_config
    assert "群聊救援配置" not in developer_apps
    assert "保存群聊救援配置" in rescue_view
    assert "救援管理员账号" in rescue_view
    assert "救援机器人 username" not in rescue_view
    assert "api<Account[]>(`/tg-accounts?${params.toString()}`)" in rescue_view
    assert "status: '在线'" in rescue_view


def test_group_rescue_account_search_ignores_stale_responses_and_surfaces_errors():
    source = (PROJECT_ROOT / "frontend/src/app/views/GroupRescueSettingsView.tsx").read_text()
    search_body = source[source.index("async function searchOnlineAccounts"):source.index("\n\n  return (")]

    assert "const searchRequestSeq = React.useRef(0);" in source
    assert "const [searchError, setSearchError] = React.useState('');" in source
    assert "const requestSeq = searchRequestSeq.current + 1;" in search_body
    assert "searchRequestSeq.current = requestSeq;" in search_body
    assert "const nextAccounts = await api<Account[]>(`/tg-accounts?${params.toString()}`);" in search_body
    assert "if (searchRequestSeq.current !== requestSeq) return;" in search_body
    assert "setAccounts(nextAccounts);" in search_body
    catch_block = search_body[search_body.index("catch (error)"):]
    assert catch_block.index("if (searchRequestSeq.current !== requestSeq) return;") < catch_block.index("setSearchError")
    finally_block = search_body[search_body.index("finally"):]
    assert "if (searchRequestSeq.current === requestSeq) setSearching(false);" in finally_block
    assert "type=\"error\"" in source
    assert "message={searchError}" in source


def test_task_center_target_selects_support_searching():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterTargetSection.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "import OperationTargetSelect" in source
    assert "<OperationTargetSelect" in source
    assert "query={{ targetType: 'group', capability }}" in source
    assert "query={{ targetType: 'channel', capability: 'task' }}" in source
    assert 'mode="multiple"' in source
    assert "onTargetsLoaded={onTargetsLoaded}" in source
    assert "mergeLoadedTargets" in view
    assert "const groupTargets = targets.filter((target) => target.target_type === 'group');" in view


def test_task_center_review_uses_task_specific_curve_units():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()

    assert "const profileUnit = taskType === 'group_ai_chat' ? '轮/小时' : '权重'" in source
    assert "${profile.intensity} ${profileUnit}" in source


def test_task_center_ai_group_rows_prefer_target_summary_title():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "function taskListTitle(task: TaskCenterTask): string" in source
    assert "return task.target_summary || task.type_config?.target_group_name || task.name;" in source
    assert "<Typography.Text strong>{taskListTitle(task)}</Typography.Text>" in source


def test_task_center_applies_ai_limit_recommendations_without_overwriting_manual_fields():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    channel_config = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterChannelConfigSections.tsx").read_text()

    assert "recommended_limits" in types
    assert "applyAiLimitRecommendations(result)" in source
    assert "form.isFieldTouched(field)" in source
    assert "max_actions_per_hour" in source
    assert "messages_per_round" in source
    assert "target_comments_per_message" in source
    assert "max_total_comments" in source
    assert "max_total_comments_jitter" in source
    assert "const MAX_TOTAL_COMMENT_JITTER = 0.3;" in channel_config
    assert "max={MAX_TOTAL_COMMENT_JITTER}" in channel_config
    assert "max_comments_per_account_per_hour" in source
    assert "推荐数量" in wizard
    assert "recommended_limits" in wizard


def test_task_center_edit_ai_limits_can_calculate_and_apply_recommendations():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "editRecommendation" in source
    assert "runEditAiLimitRecommendation" in source
    assert "applyEditAiLimitRecommendations" in source
    assert "settingsPayload(editableType, editForm.getFieldsValue(true))" in source
    assert "result.capacity_summary?.recommended_limits" in source
    assert "editForm.setFieldsValue(nextValues)" in source
    assert "计算推荐数量" in source
    assert "一键应用推荐" in source


def test_search_join_group_frontend_exposes_pacing_controls_and_details():
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    detail = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    for field in [
        "per_account_total_action_limit",
        "per_account_daily_action_limit",
        "per_account_cooldown_days",
        "per_keyword_account_daily_limit",
        "max_actions_per_day",
        "hourly_skip_probability",
        "daily_skip_probability",
        "skip_probability_per_action",
        "hourly_jitter_percent",
        "daily_jitter_percent",
    ]:
        assert f'name="{field}"' in wizard
        assert field in view
        assert f"'{field}'" in view_model
    assert "实时 pacing / random decision 不调用 LLM" in wizard
    assert "填 0 表示不设上限" in wizard
    assert "membership_observed" in wizard
    assert "target_not_in_results" in wizard
    assert "pages_exhausted=true" in wizard
    assert "停止整个任务" in wizard
    assert "pacing_limits" in detail
    assert "membership_observed 表示" in detail
    assert "最多翻 70 页" in detail
    assert "账号限制命中" in detail
    assert "Pacing 跳过" in detail


def test_task_center_membership_items_support_server_side_filters():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    modal = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    panel = (PROJECT_ROOT / "frontend/src/app/views/TaskMembershipPanel.tsx").read_text()

    assert "type MembershipFilters" in source
    assert "params.set('phase', filters.phase)" in source
    assert "params.set('manual_required', 'true')" in source
    assert "onMembershipFiltersChange" in modal
    assert "value={membershipFilters.phase}" in panel
    assert "value={membershipFilters.manualRequired}" in panel


def test_task_center_admission_unknown_labels_are_operator_friendly():
    modal = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    assert "if (status === 'unknown_after_send') return '结果未知';" in modal
    assert "const deleteStatusLabel = (status?: string | null) =>" in modal
    assert "unknown_after_send: '结果未知'" in modal
    assert "render: (value) => deleteStatusLabel(value)" in modal
    assert "rescueStatusLabel(item.rescue_status)" in modal


def test_task_detail_opens_before_membership_page_load():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    membership_loader = source[source.index("async function loadMembershipForDetail"):source.index("\n  async function loadDetail(task")]
    load_detail = source[source.index("async function loadDetail(task"):source.index("\n  async function fetchMembershipItems")]

    assert "setDetail(taskDetail)" in load_detail
    set_detail_index = load_detail.index("setDetail(taskDetail)")
    assert set_detail_index < load_detail.index("loadActionPagesForDetail(taskDetail);")
    assert set_detail_index < load_detail.index("loadDetailSectionsForDetail(taskDetail);")
    assert set_detail_index < load_detail.index("loadMembershipForDetail")
    assert "apiWithMeta<TaskCenterAction[]>(`/tasks/${taskId}/actions?${params.toString()}`)" in source
    assert "apiWithMeta<any[]>(`/tasks/${taskDetail.task.id}/${endpoints[kind]}?${params.toString()}`)" in source
    assert "apiWithMeta<TaskMembershipItem[]>(`/tasks/${taskId}/membership-items?${params.toString()}`)" in source
    assert "读取准入前置失败" in membership_loader


def test_risk_control_restores_tab_filter_and_page_from_return_query():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()

    assert "import { useLocation } from 'react-router-dom'" in source
    assert "const location = useLocation()" in source
    assert "new URLSearchParams(location.search)" in source
    assert "params.get('tab')" in source
    assert "params.get('query')" in source
    assert "params.get('quick_filter')" in source
    assert "restoreRiskTableContext(params)" in source
    assert "accountTable.setPage(page, pageSize)" in source
    assert "queueTable.setPage(page, pageSize)" in source
    assert "hitTable.setPage(page, pageSize)" in source


def test_risk_control_account_quick_filter_drives_table_rows_before_pagination():
    source = (PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx").read_text()
    account_table = source[
        source.index("const accountTable = useAntdTableControls<RiskControlAccountScore>"):
        source.index("const queueTable = useAntdTableControls<RiskDispositionItem>")
    ]
    accounts_tab = source[source.index("key: 'accounts'"):source.index("key: 'queue'")]

    assert "rows: quickFilteredAccountRows" in account_table
    assert "dataSource={accountTable.filteredRows}" in accounts_tab
    assert "dataSource={displayedAccountRows}" not in accounts_tab


def test_task_detail_membership_completion_uses_completed_at():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskMembershipPanel.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "加入账号前置任务" in source
    assert "4 小时内排完" in source
    assert "MembershipTaskSummary" in view
    assert "加入账号前置任务" in view
    assert "完成时间" in source
    assert "dataIndex: 'completed_at'" in source
    assert "membershipPhase?.status" in source
    assert "membershipPhase?.progress_percent" in source
    assert "membershipPhase?.current_phase" in source
    assert "ready_account_count" in source
    assert "pending_account_count" in source


def test_task_detail_membership_manual_rows_link_to_account_verification():
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    modal = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    panel = (PROJECT_ROOT / "frontend/src/app/views/TaskMembershipPanel.tsx").read_text()

    assert "onOpenAccountDetail={openAccountDetailFromOperation}" in app_shell
    assert "onOpenAccountDetail?: (accountId: number, tab?: string) => void | Promise<void>" in view
    assert "onOpenAccountDetail={onOpenAccountDetail}" in modal
    assert "打开账号处理" in panel
    assert "onOpenAccountDetail?.(item.account_id, '验证待处理')" in panel
    assert "先在 Telegram 内完成人工动作" in panel


def test_account_verification_tab_supports_challenge_reply_flow():
    account_modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    account_actions = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    groups_schema = (PROJECT_ROOT / "backend/app/schemas/groups.py").read_text()
    groups_router = (PROJECT_ROOT / "backend/app/api/routers/groups.py").read_text()

    assert "查看验证聊天" in account_modals
    assert "验证聊天与回复" in account_modals
    assert "verificationChallengeTask" in account_modals
    assert "输入验证码或验证回复" in account_modals
    assert "提交验证回复" in account_modals
    assert "loadVerificationChallengeContext" in account_actions
    assert "refreshVerificationChallengeContext" in account_actions
    assert "submitVerificationTaskResponse" in account_actions
    assert "VerificationChallengeContextOut" in groups_schema
    assert "submit_account_id: int | None" in groups_schema
    assert "reader_account_id: int | None" in groups_schema
    assert "target_peer_id: str" in groups_schema
    assert "detected_reason: str" in groups_schema
    assert "failure_detail: str" in groups_schema
    assert "suggested_action: str" in groups_schema
    assert "target_peer_id: string" in account_modals
    assert "verificationContextDiagnostic" in account_modals
    assert "加入账号 ID" in account_modals
    assert "读取账号 ID" in account_modals
    assert '"/api/verification-tasks/{task_id}/challenge-context"' in groups_router
    assert '"/api/verification-tasks/{task_id}/refresh-challenge-context"' in groups_router
    assert '"/api/verification-tasks/{task_id}/submit-response"' in groups_router


def test_task_center_uses_runtime_stage_for_paused_and_waiting_states():
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    runtime_stage = (PROJECT_ROOT / "frontend/src/app/views/taskRuntimeStage.ts").read_text()
    task_view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    detail_modal = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    overview = (PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx").read_text()
    operations_types = (PROJECT_ROOT / "frontend/src/app/types/operations.ts").read_text()
    shared = (PROJECT_ROOT / "frontend/src/app/components/shared.tsx").read_text()

    assert "runtimeStage, runtimeStageLabel, statusLabel" in view_model
    assert "export function runtimeStage" in runtime_stage
    assert "task.status === 'paused'" in runtime_stage
    assert "'membership_preparing'" in runtime_stage
    assert "'启动校验中'" in runtime_stage
    assert "currentStage?.stage_code === 'paused'" in detail_modal
    assert "等待 AI" in runtime_stage
    assert "不会继续规划或执行新动作" in runtime_stage
    assert "task_runtime_stage?: Record<string, any> | null;" in operations_types
    assert "issueDetail?.task_runtime_stage || issueDetail?.related_task_summary?.summary?.runtime_stage" in overview
    assert "issueTaskStage.reason" in overview
    assert "runtimeStage(task)" in task_view
    assert "runtimeStage(detail.task" in detail_modal
    assert "任务已暂停，不会继续规划或执行新动作" in detail_modal
    assert "'paused'" not in shared[shared.index("if (['草稿'"):shared.index("].includes(value)) return 'muted';")]


def test_account_center_uses_availability_summary_health_fields():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/accounts.ts").read_text()

    assert "health_score: number;" in types[types.index("export type AccountAvailabilitySummary"):types.index("export type AccountPool")]
    assert "risk_level: string;" in types[types.index("export type AccountAvailabilitySummary"):types.index("export type AccountPool")]
    assert "score_reasons: string[];" in types[types.index("export type AccountAvailabilitySummary"):types.index("export type AccountPool")]
    assert "function accountHealthScore(account: Account," in source
    assert "availabilityByAccountId.get(account.id)?.health_score ?? account.health_score" in source
    assert "dataIndex: 'health_score'" not in source


def test_accounts_view_availability_and_security_actions_surface_backend_error_detail():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()

    assert "const [error, setError] = React.useState('');" in source
    assert "message={error}" in source
    for function_name in [
        "loadAvailability",
        "rebuildAvailability",
        "refreshSelectedSecurity",
    ]:
        start = source.index(f"async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        const_end = source.find("\n  const", start + 1)
        candidates = [index for index in [async_end, const_end] if index != -1]
        function_body = source[start:min(candidates)]
        assert "catch (error)" in function_body
        assert "setError(error instanceof Error ? error.message" in function_body
        assert "throw error;" not in function_body


def test_overview_issue_status_actions_require_permission_and_structured_reason():
    source = (PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx").read_text()
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()

    assert "canManageOperationIssues?: boolean" in source
    assert "canManageOperationIssues={hasPermission(currentUser, 'operation_issues.manage')}" in app_shell
    assert "window.prompt" not in source
    assert "const [issueAction, setIssueAction]" in source
    assert "const [issueActionReason, setIssueActionReason]" in source
    assert "function openIssueAction(action: IssueAction)" in source
    assert "async function submitIssueAction()" in source

    drawer_start = source.index("title={issueDetail?.issue ? `目标异常 ${issueDetail.issue.id}` : '目标异常'}")
    drawer_end = source.index("\n      </Drawer>", drawer_start)
    drawer_block = source[drawer_start:drawer_end]
    assert "extra={canManageOperationIssues && issueDetail?.issue && (" in drawer_block
    assert "onClick={() => openIssueAction('claim')}" in drawer_block
    assert "onClick={() => openIssueAction('acknowledge')}" in drawer_block
    assert "onClick={() => openIssueAction('resolve')}" in drawer_block
    assert "onClick={() => openIssueAction('ignore')}" in drawer_block

    modal_start = source.index("title={issueAction ? `${issueActionLabel(issueAction)}原因` : '处理原因'}")
    modal_block = source[modal_start:]
    assert "value={issueActionReason}" in modal_block
    assert "onOk={() => void submitIssueAction()}" in modal_block


def test_admin_permission_modal_exposes_usage_export_permission():
    source = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()

    assert "['usage.export', '运营数据导出']" in source
    assert "'usage.export'" in source[source.index("'运营管理员'"):source.index("'账号添加专员'")]


def test_usage_reports_show_account_pool_login_drop_rates():
    source = (PROJECT_ROOT / "frontend/src/app/views/UsageReportsView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/operations.ts").read_text()

    assert "account_pool_login_drop_rates: OperationMetricDetail[]" in types
    assert "账号分组登录掉号比例" in source
    assert "metrics?.account_pool_login_drop_rates ?? []" in source
    assert "登录问题账号" in source


def test_app_snapshot_loads_only_current_view_resources():
    refresh = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    assert "const VIEW_RESOURCE_LOADERS" in refresh
    assert "loadAccountsForPool(selectedPoolId)" not in refresh
    assert "api<AuditLog[]>(auditQuery(auditFilters))" not in refresh
    assert "api<ArchiveItem[]>('/archives')" not in refresh
    assert "api<MessageTask[]>(`/message-send-tasks" not in refresh
    assert "api<AiProvider[]>('/ai-providers')" not in refresh
    assert "api<PromptTemplate[]>('/prompt-templates')" not in refresh
    assert "api<TenantAiSetting>('/tenant-ai-settings')" not in refresh
    assert "VIEW_RESOURCE_LOADERS[activeView]" in refresh
    assert "}, [token, activeView, taskStatusFilter, selectedPoolId]);" in context


def test_account_availability_summary_is_account_page_scoped():
    accounts_view = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()
    refresh = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()

    assert "if (!accounts.length) return;" in accounts_view
    assert "const accountIds = accounts.map((account) => account.id).join(',');" in accounts_view
    assert "}, [accountIds]);" in accounts_view
    assert "'/tg-accounts/availability/summary'" not in refresh


def test_task_center_loads_account_support_data_only_for_forms():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    refresh = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()

    assert "async function ensureAccounts()" in source
    assert "async function ensurePromptTemplates()" in source
    assert "loadTaskFormAccounts()" in source
    assert "[ensureAccounts(), ensurePromptTemplates()]" in source
    assert "ensureTargets()" not in source
    assert "accounts={taskAccounts}" in source
    assert "accountPools={taskAccountPools}" in source
    assert "const [taskPromptTemplates, setTaskPromptTemplates]" in source
    assert "taskManagement: async () => ({})" in refresh
    assert "async function loadTaskPage" not in refresh
    assert "loadAccountList(context.selectedPoolId)" not in refresh[refresh.index("const VIEW_RESOURCE_LOADERS"):]
    assert "loadPromptTemplates()" not in refresh[refresh.index("const VIEW_RESOURCE_LOADERS"):]


def test_core_pages_document_query_optimization_contract():
    prd = (PROJECT_ROOT / "docs/01-product/tg-ops-platform-prd.md").read_text()
    design = (PROJECT_ROOT / "docs/01-product/product-design.md").read_text()

    for source in (prd, design):
        assert "页面数据加载契约" in source
        assert "当前页面必要数据" in source
        assert "请求数减少至少 50%" in source
        assert "按需下钻" in source


def test_materials_view_exposes_prd_detail_preview_usage_and_cache_actions():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()

    assert "function openMaterialDetail(material: Material)" in source
    assert "async function refreshMaterialCache(material: Material)" in source
    assert "api<Material>(`/materials/${material.id}`)" in source
    assert "api<MaterialReferences>(`/materials/${material.id}/references`)" in source
    assert "api<MaterialVersionHistory>(`/materials/${material.id}/versions`)" in source
    assert "api<Material>(`/materials/${material.id}/refresh-cache`" in source
    assert "function materialPreviewSrc(material: Material)" in source
    assert "material-tmp" in source
    assert "return `/media/${relative}`" in source
    assert "src={materialPreviewSrc(detailMaterial)}" in source
    assert "素材预览" in source
    assert "引用记录" in source
    assert "版本与缓存记录" in source
    assert "last_cache_error" in source
    assert "usage_count" in source


def test_materials_view_detail_and_cache_refresh_ignore_stale_material_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    open_detail = source[source.index("async function openMaterialDetail"):source.index("\n  async function refreshMaterialCache")]
    refresh_cache = source[source.index("async function refreshMaterialCache"):source.index("\n  function openMaterialGroups")]
    close_detail = source[source.index("function closeMaterialDetail"):source.index("\n  async function refreshMaterialCache")]

    assert "const activeMaterialDetailId = React.useRef<number | null>(null);" in source
    assert "const materialDetailRequestSeq = React.useRef(0);" in source
    assert "function isActiveMaterialDetail(materialId: number, requestSeq: number)" in source
    assert "activeMaterialDetailId.current = material.id;" in open_detail
    assert "const requestSeq = materialDetailRequestSeq.current + 1;" in open_detail
    assert "if (!isActiveMaterialDetail(material.id, requestSeq)) return;" in open_detail
    assert "if (isActiveMaterialDetail(material.id, requestSeq)) setDetailLoading(false);" in open_detail
    assert "activeMaterialDetailId.current = null;" in close_detail
    assert "materialDetailRequestSeq.current += 1;" in close_detail
    assert "const detailRequestSeq = materialDetailRequestSeq.current;" in refresh_cache
    assert "const shouldRefreshDetail = activeMaterialDetailId.current === material.id;" in refresh_cache
    assert "if (shouldRefreshDetail && materialDetailRequestSeq.current !== detailRequestSeq) return;" in refresh_cache
    assert "if (shouldRefreshDetail) {" in refresh_cache
    assert "setDetailMaterial((current) => current?.id === updated.id ? updated : current);" in refresh_cache
    assert "await openMaterialDetail(updated);" in refresh_cache
    catch_block = refresh_cache[refresh_cache.index("catch (error)"):]
    assert catch_block.index("if (shouldRefreshDetail && materialDetailRequestSeq.current !== detailRequestSeq) return;") < catch_block.index("message.error")


def test_materials_view_exposes_prd_material_group_management():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()

    assert "type MaterialGroup" in source
    assert "function openMaterialGroups()" in source
    assert "async function saveMaterialGroup()" in source
    assert "async function toggleMaterialGroup(group: MaterialGroup)" in source
    assert "api<MaterialGroup[]>('/material-groups')" in source
    assert "api<MaterialGroup>('/material-groups'" in source
    assert "api<MaterialGroup>(`/material-groups/${editingGroup.id}`" in source
    assert "素材组管理" in source
    assert "material_count" in source


def test_material_zip_upload_modal_exposes_import_type_and_group_name_defaults():
    source = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    actions = (PROJECT_ROOT / "frontend/src/app/context/contentActions.ts").read_text()

    assert "function isZipMaterialFile(file: File)" in source
    assert "function zipGroupName(fileName: string)" in source
    assert "function handleMaterialFileChange(files: File[] | null)" in source
    assert "setMaterialForm((current) => ({ ...current, title: zipGroupName(files[0].name)" in source
    assert "素材标题 / ZIP 分组名" in source
    assert "素材类型 / ZIP 导入类型" in source
    assert "ZIP 只作为批量导入容器" in source
    assert "单个 ZIP 会按包内图片导入并返回逐文件结果" in source
    assert "ZIP 导入一次只支持选择一个压缩包" in actions
    assert "api<MaterialImportResult>('/materials/upload/zip'" in actions


def test_profile_batch_random_avatar_pool_does_not_require_manual_sources():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "不需要填写 material ID 或路径" in source
    assert "留空则按素材中心头像包顺序分配" in source


def test_profile_batch_confirm_submits_all_preview_rows_to_avoid_regeneration():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "const previewOverrides = React.useMemo(() => (precheck?.items ?? []).map" in source
    assert "preview_overrides: previewOverrides" in source


def test_profile_batch_summary_labels_manual_items_as_auto_skipped():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "const autoSkippedCount = (precheck?.summary.skipped ?? 0) + (precheck?.summary.manual_required ?? 0)" in source
    assert "自动跳过 {autoSkippedCount} 个" in source
    assert "需人工处理 {precheck" not in source


def test_account_security_batch_precheck_and_create_bind_payload_signature_and_request_sequence():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()
    precheck_body = source[source.index("async function runPrecheck"):source.index("\n\n  function openCreateConfirm")]
    create_body = source[source.index("async function createBatch"):source.index("\n\n  const previewColumns")]

    assert "const batchDrawerRequestRef = React.useRef({ kind: '', signature: '', seq: 0 });" in source
    assert "const latestBatchPayloadSignatureRef = React.useRef(payloadSignature);" in source
    assert "function beginBatchDrawerRequest(kind: string, signature: string)" in source
    assert "function isActiveBatchDrawerRequest(kind: string, signature: string, requestSeq: number)" in source
    assert "latestBatchPayloadSignatureRef.current === signature" in source
    assert "function isCurrentBatchDrawerRequest(kind: string, requestSeq: number)" in source

    assert "const requestSignature = payloadSignature;" in precheck_body
    assert "const requestSeq = beginBatchDrawerRequest('precheck', requestSignature);" in precheck_body
    assert "if (!isActiveBatchDrawerRequest('precheck', requestSignature, requestSeq)) return;" in precheck_body
    assert precheck_body.index("if (!isActiveBatchDrawerRequest('precheck', requestSignature, requestSeq)) return;") < precheck_body.index("setPrecheck(result);")
    assert "if (isCurrentBatchDrawerRequest('precheck', requestSeq)) setLoading(false);" in precheck_body

    assert "const requestSignature = payloadSignature;" in create_body
    assert "const requestSeq = beginBatchDrawerRequest('create', requestSignature);" in create_body
    assert "if (!isActiveBatchDrawerRequest('create', requestSignature, requestSeq)) return;" in create_body
    assert create_body.index("if (!isActiveBatchDrawerRequest('create', requestSignature, requestSeq)) return;") < create_body.index("setBatch(result);")
    assert "if (isCurrentBatchDrawerRequest('create', requestSeq)) setLoading(false);" in create_body


def test_telegram_profile_update_can_clear_last_name():
    source = (PROJECT_ROOT / "backend/app/integrations/telegram/gateway.py").read_text()

    assert "last_name=last_name," in source
    assert "last_name=last_name or None" not in source


def test_app_refresh_does_not_replace_accounts_with_empty_fallback_on_account_api_failure():
    source = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()
    accounts_loader = source[source.index("async function loadAccountsPage"):source.index("function messageTaskPath")]

    assert "loadAccountList(context.selectedPoolId)" in accounts_loader
    assert "loadAccountList(context.selectedPoolId).catch(() => [])" not in accounts_loader
    assert "accounts: settledValue(" not in accounts_loader


def test_navigation_does_not_reload_full_app_snapshot_for_self_loading_views():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    refresh = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()
    shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    system_loader = refresh[refresh.index("async function loadSystemPage"):refresh.index("async function loadMessagePage")]

    assert "}, [token, taskStatusFilter, selectedPoolId, activeView]);" not in context
    assert "}, [token, activeView, taskStatusFilter, selectedPoolId]);" in context
    assert "const loader = VIEW_RESOURCE_LOADERS[activeView];" in refresh
    assert "taskManagement: async () => ({})" in refresh
    assert "refreshContentResourcesForActiveView" not in context
    assert "loadAccountList(context.selectedPoolId)" not in system_loader
    assert "loadContentResources()" not in system_loader
    assert "loadSystemConfigTabData(systemConfigTab, requestSeq)" in shell
    assert "page_size=${SYSTEM_CONFIG_ACCOUNT_OPTION_LIMIT}" in shell


def test_auth_expired_api_errors_force_relogin_without_failure_modal():
    api_client = (PROJECT_ROOT / "frontend/src/shared/api/client.ts").read_text()
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    modal_state = (PROJECT_ROOT / "frontend/src/app/context/modalState.ts").read_text()

    assert "AUTH_EXPIRED_EVENT" in api_client
    assert "window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT" in api_client
    assert "window.addEventListener(AUTH_EXPIRED_EVENT" in context
    assert "setModal(null)" in context
    assert "isAuthExpiredError(error)" in modal_state
    assert "return;" in modal_state


def test_task_center_precheck_uses_long_timeout_and_capacity_summary_labels():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "TASK_CREATE_TIMEOUT_MS" in source
    assert "timeoutMs: TASK_CREATE_TIMEOUT_MS" in source
    assert "capacity_summary" in wizard
    assert "目标每条" in wizard
    assert "最大并发" in wizard
    assert "precheckReasonLabel" in view_model
    assert "formatPrecheckReasons" in source
    assert "formatPrecheckReasons" in wizard


def test_group_ai_topic_and_chat_targets_use_plain_line_inputs():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    config_fields = (PROJECT_ROOT / "backend/app/services/task_center/config_fields.py").read_text()

    assert 'name="topic_hint"' not in wizard
    assert 'label="话题方向（每行一个）"' in wizard
    assert 'label="讨论老师（每行一个）"' in wizard
    assert '[{"title":"升学规划"' not in wizard
    assert '[{"name":"王老师"' not in wizard
    assert "formatTopicDirectionLines(config.topic_directions, config.topic_hint)" not in source
    assert "formatChatTargetLines(config.teacher_targets)" in source
    assert "parseTopicDirectionLines(values.topic_directions)" in source
    assert "parseChatTargetLines(values.teacher_targets)" in source
    assert "topic_hint: values.topic_hint" not in source
    group_ai_fields = config_fields.split('"group_ai_chat": {', 1)[1].split("    },", 1)[0]
    assert '"topic_hint"' not in group_ai_fields
    assert "lines.map((title, index) => ({ ...existingTopicDirection(title, existingItems), title, weight: lines.length - index }))" in view_model
    assert "lines.map((name, index) => ({ ...existingChatTarget(name, existingItems), name, priority: lines.length - index }))" in view_model


def test_group_ai_plain_line_edit_preserves_existing_descriptions():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "parseTopicDirectionLines(values.topic_directions, existingTypeConfig.topic_directions)" in source
    assert "parseChatTargetLines(values.teacher_targets, existingTypeConfig.teacher_targets)" in source
    assert "const existingTypeConfig = detail?.task.type_config || {};" in source
    assert "...existingTopicDirection(title, existingItems)" in view_model
    assert "...existingChatTarget(name, existingItems)" in view_model


def test_group_ai_quality_funnel_labels_profile_low_match():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskAIQualityFunnelPanel.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "profile_low_match: '画像低分'" in source
    assert "voice_profile_mismatch: '面具低分'" in source
    assert "detail: string" in types
    assert "title: '细节'" in source
    assert "dataIndex: 'detail'" in source


def test_task_center_runtime_form_exposes_hour_limit_without_generic_task_daily_cap():
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "searchJoinTask ? '每小时最大搜索点击数' : '每小时最大发送量'" in wizard
    assert "min={searchJoinTask ? 0 : 1}" in wizard
    assert 'placeholder="预检后按账号数推荐"' in wizard
    assert 'name="max_actions_per_day" label="每日上限"' not in wizard
    assert "每日上限 ${values.max_actions_per_day" not in wizard
    assert "if (taskType === 'search_join_group')" in view_model
    assert "'max_actions_per_day'" in view_model


def test_target_profile_is_top_level_page_not_target_detail_governance():
    routes = (PROJECT_ROOT / "frontend/src/app/routes.ts").read_text()
    utils = (PROJECT_ROOT / "frontend/src/app/utils.ts").read_text()
    shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    targets = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    modals = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()

    assert "targetProfile: '/target-profile'" in routes
    assert "'/tasks': 'taskManagement'" in routes
    assert "targetProfile: 'target_profile.view'" in utils
    assert "TargetProfileView" in shell
    assert "['targetProfile', '目标画像'" in shell
    assert "目标画像来源状态" in targets
    assert "openLearningAction('rebuild')" not in targets
    assert "learning-versions" not in targets
    assert "目标风控策略" not in targets
    assert "target_learning." not in modals


def test_operation_target_admission_retry_reports_queued_mode():
    targets = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    retry_block = targets[targets.index("async function retryAdmission"):targets.index("\n  function startEdit", targets.index("async function retryAdmission"))]

    assert "retry.mode === 'queued'" in retry_block
    assert "已提交后台重查" in retry_block
    assert "queued_action_count" in retry_block


def test_frontend_static_publish_preserves_previous_hashed_assets():
    source = (PROJECT_ROOT / "deploy/compose-up.sh").read_text()

    assert "preserve_frontend_assets" in source
    assert "find \"$releases_dir\" -mindepth 2 -maxdepth 2 -type d -name assets" in source
    assert "cp -a \"${asset_dir}/.\" \"${tmp_dir}/assets/\"" in source
    assert "preserve_frontend_assets \"$releases_dir\" \"$tmp_dir\"" in source


def test_group_verification_challenge_context_empty_is_rendered():
    modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/accounts.ts").read_text()
    panel = (PROJECT_ROOT / "frontend/src/app/views/TaskMembershipPanel.tsx").read_text()

    assert "context_status" in types
    assert "read_failure_detail" in types
    assert "media_fingerprint" in types
    assert "读取状态" in modals
    assert "读取消息数" in modals
    assert "read_failure_detail" in modals
    assert "verificationContextAllowsReply" in modals
    assert "媒体：" in modals
    assert "challenge_context_empty" in panel
    assert "captcha_solving" in panel


def test_deploy_scripts_timeout_planner_smoke_and_remote_install():
    release = (PROJECT_ROOT / "deploy/release.sh").read_text()
    check_web = (PROJECT_ROOT / "deploy/check-web.sh").read_text()

    assert 'REMOTE_INSTALL_TIMEOUT_SECONDS="${REMOTE_INSTALL_TIMEOUT_SECONDS:-900}"' in release
    assert 'require_positive_integer REMOTE_INSTALL_TIMEOUT_SECONDS "$REMOTE_INSTALL_TIMEOUT_SECONDS"' in release
    assert 'timeout "$REMOTE_INSTALL_TIMEOUT_SECONDS" ssh "${SSH_OPTS[@]}"' in release
    assert 'local timeout_seconds="${TGYUNYING_PLANNER_SMOKE_TIMEOUT_SECONDS:-120}"' in check_web
    assert 'timeout "$timeout_seconds" docker exec tgyunying-worker-planner' in check_web


def test_production_deploy_passes_public_app_base_url_for_tenant_bot_webhook():
    release = (PROJECT_ROOT / "deploy/release.sh").read_text()
    compose = (PROJECT_ROOT / "docker-compose.server.yml").read_text()
    example_env = (PROJECT_ROOT / ".env.production.example").read_text()

    assert "PUBLIC_APP_BASE_URL=https://tgyunying.example.com" in example_env
    assert "PUBLIC_APP_BASE_URL: ${PUBLIC_APP_BASE_URL:?PUBLIC_APP_BASE_URL is required}" in compose
    assert "PUBLIC_APP_BASE_URL=${PUBLIC_APP_BASE_URL:-https://${TGYUNYING_WEB_HOST}}" in release


def test_production_deploy_starts_four_dispatcher_workers():
    compose = (PROJECT_ROOT / "docker-compose.server.yml").read_text()
    compose_up = (PROJECT_ROOT / "deploy/compose-up.sh").read_text()
    check_web = (PROJECT_ROOT / "deploy/check-web.sh").read_text()

    for index in range(1, 5):
        service_name = f"worker-dispatcher-{index}"
        container_name = f"tgyunying-worker-dispatcher-{index}"
        assert f"  {service_name}:" in compose
        assert f"container_name: {container_name}" in compose
        assert f"ACCOUNT_SHARD_INDEX: \"{index - 1}\"" in compose
        assert f"  {service_name}" in compose_up
        assert f"  {container_name}" in check_web
    assert compose.count('ACCOUNT_SHARD_TOTAL: "4"') == 4


def test_production_ai_hourly_probe_reports_membership_failures():
    workflow = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text()
    tianjin_diagnostics = (PROJECT_ROOT / ".github/scripts/tianjin_admission_diagnostics.py").read_text()
    quality_diagnostics = (PROJECT_ROOT / ".github/scripts/ai_group_quality_diagnostics.py").read_text()

    assert "run_production_diagnostics:" in workflow
    assert "run_tianjin_diagnostics:" in workflow
    assert "run_ai_group_quality_diagnostics:" in workflow
    assert "ai_group_quality_release_live_at:" in workflow
    assert "cleanup_tianjin_admission_backlog:" in workflow
    assert "force_cancel_in_progress:" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'workflow_dispatch' && inputs.force_cancel_in_progress }}" in workflow
    assert "Run production planner drain and AI hourly volume diagnostics after deploy" in workflow
    assert "Run lightweight Tianjin admission diagnostics without planner probing" in workflow
    assert "Run production diagnostics for AI group quality, dedupe, voice profiles, and online state" in workflow
    assert "Deduplicate stale Tianjin target-admission retry backlog" in workflow
    assert "default: false" in workflow
    assert workflow.count("if: ${{ github.event_name == 'workflow_dispatch' && inputs.run_production_diagnostics }}") == 2
    assert "if: ${{ github.event_name == 'workflow_dispatch' && inputs.run_tianjin_diagnostics }}" in workflow
    assert "if: ${{ github.event_name == 'workflow_dispatch' && inputs.cleanup_tianjin_admission_backlog }}" in workflow
    assert ".github/scripts/tianjin_admission_diagnostics.py" in workflow
    assert ".github/scripts/tianjin_cleanup_admission_backlog.py" in workflow
    assert "TIANJIN_LIGHT_SUMMARY=" in tianjin_diagnostics
    assert "TIANJIN_FAILED_ACCOUNTS=" in tianjin_diagnostics
    assert "VerificationTask" in workflow
    assert "recent_membership_actions" in workflow
    assert "recent_failed_membership_actions" in workflow
    assert "recent_verification_tasks" in workflow
    assert "membership_status" in workflow
    assert "membership_peer_ref" in workflow
    assert "membership_fallback_ref" in workflow
    assert "target_peer_id" in workflow
    assert "account_policy_reason" in workflow
    assert "AI_HOURLY_INSPECT_START" in workflow
    assert "AI_HOURLY_TASK_VOLUME_TASK=" in workflow
    assert "AI_HOURLY_TASK_VOLUME_SUMMARY=" in workflow
    assert "AI_HOURLY_INSPECT_END" in workflow
    assert ".github/scripts/ai_group_quality_diagnostics.py" in workflow
    assert "AI_GROUP_QUALITY_DIAGNOSTICS_START" in workflow
    assert "AI_GROUP_QUALITY_DIAGNOSTICS_END" in workflow
    assert "inputs.ai_group_quality_release_live_at" in workflow
    assert "AI_GROUP_RELEASE_LIVE_AT=" in workflow
    assert "timeout 1200 docker exec -e AI_GROUP_RELEASE_LIVE_AT=" in workflow
    assert "AI_GROUP_QUALITY_VOICE_PROFILES" in quality_diagnostics
    assert "AI_GROUP_QUALITY_MEMORY" in quality_diagnostics
    assert "AI_GROUP_QUALITY_RECENT_ACTIONS" in quality_diagnostics
    assert "AI_GROUP_QUALITY_RECENT_ACTIONS_AFTER_RELEASE" in quality_diagnostics
    assert "AI_GROUP_REALISM_AUDIT_AFTER_RELEASE" in quality_diagnostics
    assert "AI_GROUP_QUALITY_PAYLOAD_GATE_FAILED" in quality_diagnostics
    assert "AI_GROUP_QUALITY_ONLINE_WAIT" in quality_diagnostics
    assert "AI_GROUP_QUALITY_HARD_HOURLY_DRAIN" in quality_diagnostics
    assert "AI_GROUP_QUALITY_ONLINE_GATE_FAILED" in quality_diagnostics
    assert "AI_GROUP_QUALITY_TASK" in quality_diagnostics


def test_api_error_message_supports_trace_id_in_structured_detail_objects():
    api_client = (PROJECT_ROOT / "frontend/src/shared/api/client.ts").read_text()
    task_center = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    task_error = task_center[task_center.index("export function errorMessage"):task_center.index("\n\nexport function words")]

    assert "if (detail && typeof detail === 'object')" in api_client
    assert "record.message ?? record.failure_detail" in api_client
    assert "record.trace_id" in api_client
    assert "return error.message;" in task_error


def test_profile_batch_submit_message_says_background_execution_not_completed():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "已提交后台执行" in source
    assert "后台 worker 完成后再刷新账号列表" in source
    assert "批次 #${result.id} 已创建：共" not in source


def test_profile_batch_random_avatar_pool_is_default_auto_pick_not_manual_source():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    random_option = "{ label: '随机头像包', value: 'random_from_material_pool' }"
    assert random_option in source
    assert "avatarStrategy.mode === 'random_from_material_pool'" in source
    assert "const shouldShowAvatarSourceInput = avatarStrategy.mode === 'sequential'" in source
    assert "placeholder=\"可选覆盖头像来源：每行一个 avatar:对象key / material:素材ID / 平台媒体文件路径\"" in source


def test_profile_batch_account_picker_supports_top_100_cross_page_and_range_selection():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "const BATCH_SELECTION_LIMIT = 100" in source
    assert "选择当前筛选前 100 个" in source
    assert "filteredAccounts.slice(0, BATCH_SELECTION_LIMIT)" in source
    assert "preserveSelectedRowKeys: true" in source
    assert "const [rangeStart, setRangeStart]" in source
    assert "const [rangeEnd, setRangeEnd]" in source
    assert "function selectFilteredRange()" in source
    assert "filteredAccounts.slice(start - 1, end)" in source
    assert "区间选择" in source


def test_account_center_exposes_standby_session_batch_entry_and_filters():
    accounts_view = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "'standby_session'" in accounts_view
    assert "补齐备用 session" in accounts_view
    assert "setSecurityDrawerMode('standby_session')" in accounts_view
    assert "canManageAuthorizations" in accounts_view
    assert "standby_1 session 缺失" in accounts_view
    assert "standby_2 session 缺失" in accounts_view
    assert "可从备用 session 激活恢复" in accounts_view
    assert "未做过登录设备清理" in accounts_view
    assert "standby_session: {" in drawer
    assert "provision_standby_session" in drawer
    assert "self_heal_session" in drawer
    assert "account_standby_session_provision" in drawer
    assert "自动补齐缺失槽位" in drawer
    assert "仅 standby_1" in drawer
    assert "仅 standby_2" in drawer
    assert "standby_slot_strategy: standbySlotStrategy" in drawer


def test_account_center_quick_searches_login_problem_accounts():
    accounts_view = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()
    account_types = (PROJECT_ROOT / "frontend/src/app/types/accounts.ts").read_text()
    account_auth_types = (PROJECT_ROOT / "frontend/src/app/types/accountAuth.ts").read_text()

    assert "登录有问题" in accounts_view
    assert "没有登录上平台" in accounts_view
    assert "'Session失效'" in accounts_view
    assert "session完全失效" in accounts_view
    assert "登录验证码没收到" in accounts_view
    assert "latestLoginText(account)" in accounts_view
    assert "account.latest_login_flow" in accounts_view
    assert "登录失败：{loginFlow.failure_detail}" in accounts_view
    assert "hasLoginIssue(account)" in accounts_view
    assert "flow?.failure_type || flow?.failure_detail" in accounts_view
    assert "account.authorization_summary.primary_status !== 'active'" in accounts_view
    assert "accountTable.setQuery('登录有问题')" in accounts_view
    assert "latest_login_flow: AccountLatestLoginFlow | null" in account_types
    assert "export type AccountLatestLoginFlow" in account_auth_types
    assert "最近登录流水存在失败类型 / 失败详情" in (PROJECT_ROOT / "docs/01-product/tg-ops-platform-prd.md").read_text()


def test_account_center_prd_documents_login_problem_quick_search_scope():
    prd = (PROJECT_ROOT / "docs/01-product/tg-ops-platform-prd.md").read_text()
    design = (PROJECT_ROOT / "docs/03-feature-designs/account-security-hardening-design.md").read_text()

    for source in (prd, design):
        assert "登录有问题" in source
        assert "没有登录上平台" in source
        assert "等待验证码、等待扫码、等待2FA、需重新登录、异常、Session 失效" in source
        assert "主授权不可用" in source


def test_standby_session_batch_uses_dedicated_confirmation_flow_not_profile_preview():
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "const precheckColumns = isProfileMode ? profilePreviewColumns : actionPrecheckColumns" in drawer
    assert 'columns={precheckColumns}' in drawer
    assert "precheckButtonLabel" in drawer
    assert "'预检备用 session 补齐'" in drawer
    assert "confirmButtonLabel" in drawer
    assert "'确认补齐备用 session'" in drawer
    assert "{isProfileMode && <Button icon={<Activity size={16} />} loading={loading} onClick={runPrecheck}>重抽全部</Button>}" in drawer


def test_standby_session_batch_explains_zero_executable_and_current_profile_names():
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()

    assert "standbyNoExecutable" in drawer
    assert "当前没有可自动补齐的备用 session" in drawer
    assert "账号列展示的是当前 TG 昵称和 username，不是本次生成的新资料。" in drawer
    assert "mode === 'standby_session' ? '账号（当前资料）' : '账号'" in drawer


def test_standby_session_batch_labels_code_and_two_fa_waiting_states():
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()
    runtime = (PROJECT_ROOT / "frontend/src/app/views/taskRuntimeStage.ts").read_text()
    shared = (PROJECT_ROOT / "frontend/src/app/components/shared.tsx").read_text()
    task_detail = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    assert "code_waiting: '等待验证码'" in drawer
    assert "two_fa_waiting: '等待 2FA'" in drawer
    assert "if (value === 'code_waiting') return '等待验证码';" in runtime
    assert "if (value === 'two_fa_waiting') return '等待 2FA';" in runtime
    assert "'code_waiting'" in shared
    assert "'two_fa_waiting'" in shared
    assert "dataIndex: 'developer_app_label'" in task_detail
    assert "dataIndex: 'proxy_label'" in task_detail
    assert "dataIndex: 'two_fa_usage_status'" in task_detail


def test_account_detail_has_authorization_assets_tab_with_slot_cards_and_recovery_action():
    modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    assets_panel = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()

    assert "'授权资产'" in modals
    assert "accountDetailTab === '授权资产'" in modals
    assert "AccountAuthorizationAssetsPanel" in modals
    assert "恢复能力" in assets_panel
    assert "primary session" in assets_panel
    assert "standby_1 session" in assets_panel
    assert "standby_2 session" in assets_panel
    assert "激活恢复" in assets_panel
    assert "故障槽位" in assets_panel
    assert "验证码不可读取" in assets_panel
    assert "2FA 未托管" in assets_panel


def test_code_receiver_managed_2fa_panel_is_reveal_only_and_code_entry_stays_visible():
    managed_2fa = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    accounts_view = (PROJECT_ROOT / "frontend/src/app/views/AccountsView.tsx").read_text()

    assert "accountIdentity" in managed_2fa
    assert "const isCodeReceiver = accountIdentity === 'code_receiver';" in managed_2fa
    assert "{!isCodeReceiver && (" in managed_2fa
    assert "!isCodeReceiver && canManageCredentials" in managed_2fa
    assert "accountIdentity={accountDetail.account.account_identity}" in modals
    assert accounts_view.count("{canViewCodes && <Button size=\"small\" loading={isActionPending(`account:${account.id}:codes`)} onClick={() => onExtractCodes(account)}>提取验证码</Button>}") >= 2


def test_security_drawers_show_cleanup_preservation_and_managed_2fa_policy():
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()
    modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    managed_2fa = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    router = (PROJECT_ROOT / "backend/app/api/routers/account_security.py").read_text()
    auth = (PROJECT_ROOT / "backend/app/auth.py").read_text()

    assert "只保留当前 session、已确认 hash 的 primary / standby_1 / standby_2 和一个官方锚点设备" in drawer
    assert "预计清理外部设备" in drawer
    assert "平台托管 2FA" in drawer
    assert "已设置且平台托管旧密码" in drawer
    assert "旧密码未知" in drawer
    assert "AccountManaged2FaSettingsPanel" in modals
    assert "托管 2FA" in modals
    assert "密码设置 / 轮换不回显旧密码" in modals
    assert "accountId={accountDetail.account.id}" in modals
    assert "const suffix = action === 'save' ? 'managed-2fa' : `managed-2fa/${action}`;" in managed_2fa
    assert "return `/tg-accounts/${accountId}/security/${suffix}`;" in managed_2fa
    assert "post_account_security_managed_2fa" in router
    assert "post_account_security_managed_2fa_rotate" in router
    assert "post_account_security_managed_2fa_reveal" in router
    assert "accounts.security.credential_manage" in auth


def test_task_center_account_security_system_task_detail_switches_by_batch_type():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "account_security_batch" in types
    assert "system_task_type" in types
    assert "account_device_cleanup" in source
    assert "account_2fa_setup" in source
    assert "account_standby_session_provision" in source
    assert "当前 session / 已确认 hash 的主备授权 / 1 个官方锚点" in source
    assert "目标槽位" in source
    assert "验证码读取" in source
    assert "params.set('type', nextTaskTypeFilter)" in view


def test_task_center_exposes_task_type_filter_request_parameter():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "taskTypeFilter" in source
    assert "account_profile_init" in source
    assert "params.set('type', nextTaskTypeFilter)" in source
    assert "api<TaskCenterTask[]>(`/tasks${query ? `?${query}` : ''}`)" in source


def test_task_center_list_groups_by_target_group_and_channel():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    grouping = (PROJECT_ROOT / "frontend/src/app/views/taskCenterListGrouping.ts").read_text()

    assert "buildTaskQuickGroups" in grouping
    assert "filterTasksByQuickGroup" in grouping
    assert "targetGroupLabel" in grouping
    assert "associatedChannelLabel" in grouping
    assert "const [selectedTaskGroupId, setSelectedTaskGroupId] = React.useState('all');" in source
    assert "const taskQuickGroups = buildTaskQuickGroups(table.filteredRows);" in source
    assert "const visibleTaskRows = filterTasksByQuickGroup(table.filteredRows, selectedTaskGroupId);" in source
    assert "全部任务分组" in source
    assert "<Select<string>" in source
    assert 'aria-label="任务分组"' in source
    assert "TASK_GROUP_SELECT_WIDTH" in source
    assert "TASK_GROUP_DROPDOWN_WIDTH" in source
    assert "popupMatchSelectWidth={TASK_GROUP_DROPDOWN_WIDTH}" in source
    assert "<Segmented" not in source[source.index("const taskQuickGroups"):source.index("dataSource={visibleTaskRows}")]
    assert "dataSource={visibleTaskRows}" in source
    assert "Table<TaskCenterTask>" in source
    assert "目标群聊" in source
    assert "关联频道" in source


def test_task_center_shows_account_coverage_for_active_tasks():
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    modal = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "accountCoverageLabel" in view
    assert "账号覆盖" in view
    assert "今日账号参与覆盖" in modal
    assert "export type TaskAccountCoverage" in types
    assert "account_coverage?: TaskAccountCoverage" in types
    assert "covered_count" in view_model


def test_task_center_hides_delete_for_account_security_system_tasks():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "function canDeleteTask(task: TaskCenterTask)" in source
    assert "canDeleteTask(task)" in source
    assert "openDangerTaskAction(task, 'delete')" in source
    assert "return canManageTasks && Boolean(task.id) && !isSystemTask(task);" in source


def test_task_center_lifecycle_buttons_match_task_status():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "function canStartTask(task: TaskCenterTask)" in source
    assert "return !isSystemTask(task) && task.status !== 'running';" in source
    assert "function canPauseTask(task: TaskCenterTask)" in source
    assert "return !isSystemTask(task) && task.status === 'running';" in source
    assert "canManageTasks && canStartTask(task) &&" in source
    assert "canManageTasks && canPauseTask(task) &&" in source


def test_task_center_failure_diagnosis_is_visible_before_attempt_table():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "failure_diagnosis" in types
    assert "failureDiagnosis(action)" in source
    assert "处理建议" in source
    assert "账号/目标原因" in source
    assert "task.status === 'paused' ? 'resume' : 'start'" in source
    assert ">{task.status === 'paused' ? '恢复' : '启动'}</Button>" not in source
    assert "disabled={task.status !== 'running'}" not in source


def test_task_center_ai_generation_records_show_generation_source():
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "generation_source?: string" in types
    assert "function aiGenerationSourceLabel" in view
    assert "生成来源" in view
    assert "human_context: '真人上下文'" in view
    assert "idle_continuation: '无人续聊'" in view
    assert "bootstrap: '冷启动'" in view


def test_task_center_ai_turns_show_voice_profile_and_memory_fields():
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()

    assert "account_voice_profile_version: number" in types
    assert "account_voice_profile_summary: string" in types
    assert "account_mask_version: number" in types
    assert "account_mask_summary: string" in types
    assert "turn.account_mask_summary || turn.account_voice_profile_summary" in view
    assert "stance_summary: string" in types
    assert "ai_message_memory_id: string" in types
    assert "material_intent: string" in types
    assert "material_matched_tags: string[]" in types
    assert "material_candidate_count: number" in types
    assert "面具" in view
    assert "立场/记忆" in view
    assert "素材意图" in view
    assert "act_type" in view


def test_task_center_create_refreshes_after_long_timeout_and_capacity_summary_types():
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/taskCenter.ts").read_text()
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()

    assert "const TASK_CREATE_TIMEOUT_MS = 120_000" in view
    assert "timeoutMs: TASK_CREATE_TIMEOUT_MS" in view
    assert "await load();" in view
    assert "capacity_summary" in types
    assert "容量口径" in wizard
    assert "最大并发" in wizard


def test_task_center_create_reuses_review_precheck_before_submit():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    create_task = source[source.index("async function createTask"):source.index("\n\n  async function saveTaskSettings")]
    assert "const precheckSignature = taskPrecheckPayloadSignature(taskType, payload);" in create_task
    assert "const requiresFreshPrecheck = taskType !== 'group_membership_admission' && taskType !== 'search_rank_deboost' && !options.skipCapacityCheck;" in create_task
    assert "precheck && precheckPayloadSignature === precheckSignature" in create_task
    assert "await runTaskPrecheck(values)" in create_task
    assert "if (!result && requiresFreshPrecheck) return;" in create_task


def test_search_rank_deboost_create_submits_draft_until_real_exempt_group_exists():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    create_task = source[source.index("async function createTask"):source.index("\n\n  async function saveTaskSettings")]

    assert "const shouldStartNow = start && taskType !== 'search_rank_deboost';" in create_task
    assert "(shouldStartNow ? CREATE_AND_START_ENDPOINT : CREATE_ENDPOINT)[taskType]" in create_task
    assert "搜索排名观察任务已创建为草稿" in create_task
    assert "refreshTaskListAfterAction(shouldStartNow ? '任务创建并启动' : '任务创建')" in create_task


def test_api_error_message_supports_timeout_copy_and_trace_id():
    api_client = (PROJECT_ROOT / "frontend/src/shared/api/client.ts").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "error.status === 408" in source
    assert "请求超时，服务可能仍在处理" in source
    assert "if (detail && typeof detail === 'object')" in api_client
    assert "record.trace_id" in api_client
    assert "return error.message;" in source


def test_search_rank_deboost_routes_are_covered_by_permission_gates():
    assert required_permission("POST", "/api/tasks/search_rank_deboost") == (
        "tasks.manage",
        "tasks.create.search_rank_deboost",
    )
    assert required_permission("POST", "/api/tasks/search_rank_deboost/create_and_start") == (
        "tasks.manage",
        "tasks.create.search_rank_deboost",
    )
    assert required_permission("PATCH", "/api/tasks/123/search_rank_deboost_config") == (
        "tasks.manage.search_rank_deboost",
    )
    assert required_permission("POST", "/api/tasks/123/search_rank_deboost_reroll_exempt_group") == (
        "tasks.manage.search_rank_deboost",
    )
