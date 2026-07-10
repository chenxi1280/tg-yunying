from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_CENTER_VIEW = PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx"
TASK_CENTER_DETAIL_MODAL = PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx"


def _source() -> str:
    return TASK_CENTER_VIEW.read_text()


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  function", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_task_center_actions_distinguish_refresh_failure_from_write_failure():
    source = _source()

    assert "async function fetchTaskListData(requestSeq: number, nextTaskTypeFilter: TaskTypeFilter = taskTypeFilter): Promise<boolean>" in source
    assert "async function refreshTaskListAfterAction(actionLabel: string)" in source
    assert "async function refreshVisibleTaskAfterAction(actionLabel: string, task: TaskCenterTask)" in source
    assert "任务中心数据刷新失败" in source
    assert "操作已完成" in source

    list_helper = source[source.index("async function refreshTaskListAfterAction"):source.index("\n\n  async function refreshVisibleTaskAfterAction")]
    assert "await fetchTaskListData(requestSeq);" in list_helper
    assert "setActionError(`任务中心数据刷新失败：" in list_helper

    detail_helper = source[source.index("async function refreshVisibleTaskAfterAction"):source.index("\n\n  async function ensureTargets")]
    assert "await refreshTaskListAfterAction(actionLabel);" in detail_helper
    assert "const taskDetail = await fetchTaskDetail(task.id);" in detail_helper
    assert "setActionError(`任务中心数据刷新失败：" in detail_helper

    for function_name in [
        "createTask",
        "saveTaskSettings",
        "taskAction",
        "membershipAdmissionAction",
        "deleteTask",
        "addSourceIdentityToBlocklist",
    ]:
        body = _function_body(source, function_name)
        if function_name != "createTask":
            assert "await load();" not in body
        assert "await loadDetail(" not in body
        assert (
            "await refreshTaskListAfterAction(" in body
            or "await refreshVisibleTaskAfterAction(" in body
        )


def test_task_center_task_list_refreshes_ignore_stale_responses():
    source = _source()
    fetch_list = source[source.index("async function fetchTaskListData"):source.index("\n\n  async function load")]
    load_list = source[source.index("async function load("):source.index("\n\n  async function refreshTaskListAfterAction")]
    refresh_list = source[source.index("async function refreshTaskListAfterAction"):source.index("\n\n  async function refreshVisibleTaskAfterAction")]

    assert "const activeTaskListRequestSeq = React.useRef(0);" in source
    assert "function beginTaskListRequest()" in source
    assert "activeTaskListRequestSeq.current += 1;" in source
    assert "function isActiveTaskListRequest(requestSeq: number)" in source

    assert "async function fetchTaskListData(requestSeq: number, nextTaskTypeFilter: TaskTypeFilter = taskTypeFilter): Promise<boolean>" in fetch_list
    assert "const [taskData, schedulingData] = await Promise.all([" in fetch_list
    assert "if (!isActiveTaskListRequest(requestSeq)) return false;" in fetch_list
    assert fetch_list.index("if (!isActiveTaskListRequest(requestSeq)) return false;") < fetch_list.index("setTasks(taskData);")
    assert fetch_list.index("setTasks(taskData);") < fetch_list.index("setSchedulingSetting(schedulingData);")

    assert "const requestSeq = beginTaskListRequest();" in load_list
    assert "await fetchTaskListData(requestSeq, nextTaskTypeFilter);" in load_list
    assert "if (!isActiveTaskListRequest(requestSeq)) return;" in load_list
    assert "if (isActiveTaskListRequest(requestSeq)) setLoading(false);" in load_list

    assert "const requestSeq = beginTaskListRequest();" in refresh_list
    assert "await fetchTaskListData(requestSeq);" in refresh_list
    assert "if (!isActiveTaskListRequest(requestSeq)) return;" in refresh_list


def test_task_center_row_actions_bind_busy_state_to_action_key():
    source = _source()
    task_action = _function_body(source, "taskAction")
    admission_action = _function_body(source, "membershipAdmissionAction")
    export_failures = _function_body(source, "downloadMembershipAdmissionFailures")
    delete_task = _function_body(source, "deleteTask")

    assert "const activeTaskActionKey = React.useRef('');" in source
    assert "function beginTaskAction(actionKey: string)" in source
    assert "function isActiveTaskAction(actionKey: string)" in source
    assert "function clearTaskAction(actionKey: string)" in source

    expected = [
        (task_action, "const actionKey = `${task.id}:${name}`;"),
        (admission_action, "const actionKey = `admission:${loadingKey}`;"),
        (export_failures, "const actionKey = `admission:export:${task.id}`;"),
        (delete_task, "const actionKey = `${task.id}:delete`;"),
    ]
    for body, action_key in expected:
        assert action_key in body
        assert "beginTaskAction(actionKey);" in body
        assert "if (!isActiveTaskAction(actionKey)) return false;" in body
        catch_block = body[body.index("catch (error)"):]
        assert catch_block.index("if (!isActiveTaskAction(actionKey)) return false;") < catch_block.index("setActionError(errorMessage(error));")
        assert "clearTaskAction(actionKey);" in body[body.index("finally"):]
        assert "setBusyId('');" not in body


def test_task_center_precheck_and_recommendation_bind_payload_signature():
    source = _source()
    run_precheck = _function_body(source, "runTaskPrecheck")
    run_recommendation = _function_body(source, "runEditAiLimitRecommendation")
    create_task = _function_body(source, "createTask")

    assert "const [precheckPayloadSignature, setPrecheckPayloadSignature] = React.useState('');" in source
    assert "const activeTaskPrecheckRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "const activeEditRecommendationRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "function taskPrecheckPayloadSignature(type: TaskCenterTaskType, payload: Record<string, any>)" in source
    assert "function beginTaskPrecheckRequest(signature: string)" in source
    assert "function currentTaskPrecheckPayloadSignature()" in source
    assert "function isActiveTaskPrecheckRequest(requestSeq: number, signature: string)" in source
    assert "function isCurrentTaskPrecheckRequest(requestSeq: number)" in source
    assert "function beginEditRecommendationRequest(signature: string)" in source
    assert "function currentEditRecommendationPayloadSignature()" in source
    assert "function isActiveEditRecommendationRequest(requestSeq: number, signature: string)" in source
    assert "function isCurrentEditRecommendationRequest(requestSeq: number)" in source
    assert "currentTaskPrecheckPayloadSignature() === signature" in source
    assert "currentEditRecommendationPayloadSignature() === signature" in source

    assert "const payload = createPayload(values);" in run_precheck
    assert "const payloadSignature = taskPrecheckPayloadSignature(taskType, payload);" in run_precheck
    assert "const requestSeq = beginTaskPrecheckRequest(payloadSignature);" in run_precheck
    assert "body: JSON.stringify({ task_type: taskType, payload })," in run_precheck
    stale_precheck_guard = "if (!isActiveTaskPrecheckRequest(requestSeq, payloadSignature)) return null;"
    assert stale_precheck_guard in run_precheck
    assert run_precheck.index(stale_precheck_guard) < run_precheck.index("setPrecheck(result);")
    assert "setPrecheckPayloadSignature(payloadSignature);" in run_precheck
    assert "if (isCurrentTaskPrecheckRequest(requestSeq)) setPrecheckLoading(false);" in run_precheck

    assert "const payload = createPayload(values);" in create_task
    assert "const precheckSignature = taskPrecheckPayloadSignature(taskType, payload);" in create_task
    assert "const requiresFreshPrecheck = taskType !== 'group_membership_admission' && taskType !== 'search_rank_deboost' && !options.skipCapacityCheck;" in create_task
    assert "precheck && precheckPayloadSignature === precheckSignature" in create_task
    assert "await runTaskPrecheck(values)" in create_task
    assert "if (!result && requiresFreshPrecheck) return;" in create_task

    assert "const payload = settingsPayload(editableType, editForm.getFieldsValue(true));" in run_recommendation
    assert "payloadSignature = taskPrecheckPayloadSignature(editableType, payload);" in run_recommendation
    assert "requestSeq = beginEditRecommendationRequest(payloadSignature);" in run_recommendation
    assert "body: JSON.stringify({ task_type: editableType, payload })," in run_recommendation
    stale_recommendation_guard = "if (!isActiveEditRecommendationRequest(requestSeq, payloadSignature)) return;"
    assert stale_recommendation_guard in run_recommendation
    assert run_recommendation.index(stale_recommendation_guard) < run_recommendation.index("setEditRecommendation(result.capacity_summary?.recommended_limits ?? null);")
    catch_block = run_recommendation[run_recommendation.index("catch (error)"):]
    catch_guard = "if (requestSeq && !isActiveEditRecommendationRequest(requestSeq, payloadSignature)) return;"
    assert catch_guard in catch_block
    assert catch_block.index(catch_guard) < catch_block.index("setActionError(errorMessage(error));")
    assert "if (!requestSeq || isCurrentEditRecommendationRequest(requestSeq)) setEditRecommendationLoading(false);" in run_recommendation


def test_task_center_save_settings_binds_payload_signature_and_edit_session():
    source = _source()
    save_settings = _function_body(source, "saveTaskSettings")
    close_edit = source[source.index("function closeEditTaskModal"):source.index("\n\n  function accountConfig")]

    assert "const activeEditSaveRequestRef = React.useRef({ seq: 0, taskId: '', signature: '' });" in source
    assert "function taskSettingsSavePayloadSignature(taskId: string, type: TaskCenterTaskType, payload: Record<string, any>)" in source
    assert "function invalidateTaskSettingsSaveRequest()" in source
    assert "function beginTaskSettingsSaveRequest(taskId: string, signature: string)" in source
    assert "function currentTaskSettingsSavePayloadSignature()" in source
    assert "function isCurrentTaskSettingsSaveRequest(requestSeq: number)" in source
    assert "function isActiveTaskSettingsSaveRequest(taskId: string, requestSeq: number, signature: string)" in source
    assert "currentTaskSettingsSavePayloadSignature() === signature" in source

    assert "invalidateTaskSettingsSaveRequest();" in close_edit
    assert "setEditOpen(false);" in close_edit
    assert "onCancel={closeEditTaskModal}" in source

    assert "const taskId = detail.task.id;" in save_settings
    assert "const payload = settingsPayload(editableType, values);" in save_settings
    assert "payloadSignature = taskSettingsSavePayloadSignature(taskId, editableType, payload);" in save_settings
    assert "requestSeq = beginTaskSettingsSaveRequest(taskId, payloadSignature);" in save_settings
    assert "body: JSON.stringify(payload)" in save_settings
    stale_save_guard = "if (!isActiveTaskSettingsSaveRequest(taskId, requestSeq, payloadSignature)) return;"
    assert stale_save_guard in save_settings
    assert save_settings.index(stale_save_guard) < save_settings.index("setEditOpen(false);")
    catch_block = save_settings[save_settings.index("catch (error)"):]
    catch_guard = "if (requestSeq && !isActiveTaskSettingsSaveRequest(taskId, requestSeq, payloadSignature)) return;"
    assert catch_guard in catch_block
    assert catch_block.index(catch_guard) < catch_block.index("setActionError(errorMessage(error));")
    assert "if (!requestSeq || isCurrentTaskSettingsSaveRequest(requestSeq)) setEditSaving(false);" in save_settings


def test_task_center_action_attempt_failures_stay_visible_in_modal():
    source = _source()
    attempts_body = _function_body(source, "openActionAttempts")
    modal_start = source.index("title={attemptDetail ? `执行尝试 ${attemptDetail.action.id}`")
    attempts_modal = source[modal_start:source.index("\n      <Modal", modal_start + 1)]

    assert "const [attemptError, setAttemptError] = React.useState('');" in source
    assert "setAttemptError('');" in attempts_body
    assert "setAttemptDetail({ action, attempts: [], loading: false });" in attempts_body
    assert "setAttemptError(`读取执行尝试失败：${errorMessage(error)}`);" in attempts_body
    assert "throw error;" not in attempts_body
    assert "message={attemptError}" in attempts_modal


def test_task_center_action_attempts_ignore_stale_action_responses():
    source = _source()
    attempts_body = _function_body(source, "openActionAttempts")
    modal_start = source.index("title={attemptDetail ? `执行尝试 ${attemptDetail.action.id}`")
    attempts_modal = source[modal_start:source.index("\n        destroyOnHidden", modal_start)]

    assert "const activeAttemptActionId = React.useRef<string | null>(null);" in source
    assert "activeAttemptActionId.current = action.id;" in attempts_body
    assert "if (activeAttemptActionId.current !== action.id) return;" in attempts_body
    assert attempts_body.index("if (activeAttemptActionId.current !== action.id) return;") < attempts_body.index("setAttemptDetail({ action, attempts, loading: false });")
    assert "activeAttemptActionId.current = null;" in attempts_modal


def test_task_center_form_support_data_ignores_stale_requests():
    source = _source()
    ensure_form = source[source.index("async function ensureTaskFormData"):source.index("\n\n  React.useEffect", source.index("async function ensureTaskFormData"))]
    lazy_effect = source[source.index("if (!modalOpen && !editOpen) return;"):source.index("}, [editOpen, modalOpen, messageScope, taskType]")]
    create_task = _function_body(source, "openCreateTask")
    edit_task = _function_body(source, "openEditTask")
    next_step = _function_body(source, "nextStep")
    reset_type = source[source.index("function resetTypeFields"):source.index("\n\n  const table")]

    assert "const activeTaskFormSupportRequestSeq = React.useRef(0);" in source
    assert "function beginTaskFormSupportRequest()" in source
    assert "activeTaskFormSupportRequestSeq.current += 1;" in source
    assert "function isActiveTaskFormSupportRequest(requestSeq: number)" in source
    assert "async function ensureTaskFormData(type: TaskCenterTaskType, requestSeq: number): Promise<boolean>" in ensure_form
    assert "if (!isActiveTaskFormSupportRequest(requestSeq)) return false;" in ensure_form
    assert ensure_form.index("if (!isActiveTaskFormSupportRequest(requestSeq)) return false;") > ensure_form.index("await Promise.all(requests);")
    assert "if (isActiveTaskFormSupportRequest(requestSeq)) setSupportLoading(false);" in ensure_form
    assert "async function loadTaskTypeSupportData(type: TaskCenterTaskType, requestSeq: number): Promise<boolean>" in source

    assert "const requestSeq = beginTaskFormSupportRequest();" in lazy_effect
    assert "void loadTaskTypeSupportData(taskType, requestSeq).catch((error) => {" in lazy_effect
    assert "if (!isActiveTaskFormSupportRequest(requestSeq)) return;" in lazy_effect
    assert lazy_effect.index("if (!isActiveTaskFormSupportRequest(requestSeq)) return;") < lazy_effect.index("setActionError(`读取任务表单支撑数据失败：")

    for block in [create_task, edit_task, next_step]:
        assert "const requestSeq = beginTaskFormSupportRequest();" in block
        assert "await ensureTaskFormData(" in block
        assert "if (!isActiveTaskFormSupportRequest(requestSeq)) return;" in block

    assert "const requestSeq = beginTaskFormSupportRequest();" in reset_type
    assert "void ensureTaskFormData(nextType, requestSeq)" in reset_type
    assert "if (!loaded || !isActiveTaskFormSupportRequest(requestSeq)) return;" in reset_type
    assert "if (!isActiveTaskFormSupportRequest(requestSeq)) return;" in reset_type[reset_type.index(".catch((error) => {"):]
    assert reset_type.index("if (!loaded || !isActiveTaskFormSupportRequest(requestSeq)) return;") < reset_type.index("applyDefaultRuleSet(loadedRuleSets, nextType);")


def test_task_detail_failure_errors_are_bound_to_active_task():
    source = _source()
    load_detail_start = source.index("async function loadDetail(task")
    load_detail = source[load_detail_start:source.index("\n\n  async function fetchMembershipItems", load_detail_start)]
    load_detail_catch = load_detail[load_detail.index("} catch (error) {"):]
    focus_effect = source[
        source.index("if (!focusTask || appliedFocusNonce.current === focusTask.nonce) return;"):
        source.index("}, [focusTask, onFocusTaskConsumed]")
    ]

    assert "if (!isActiveDetailRequest(task.id, requestSeq)) return;" in load_detail_catch
    assert load_detail_catch.index("if (!isActiveDetailRequest(task.id, requestSeq)) return;") < load_detail_catch.index("setActionError(`读取任务 ${task.id} 详情失败：")

    assert ".catch((error) => {" in focus_effect
    assert "if (!isActiveDetailRequest(focusTask.taskId, requestSeq)) return;" in focus_effect
    assert focus_effect.index("if (!isActiveDetailRequest(focusTask.taskId, requestSeq)) return;") < focus_effect.index("setActionError(`读取任务 ${focusTask.taskId} 详情失败：")


def test_task_center_write_refreshes_are_bound_to_active_task():
    source = _source()
    refresh_detail = source[
        source.index("async function refreshVisibleTaskAfterAction"):
        source.index("\n\n  async function ensureTargets")
    ]
    membership_action = _function_body(source, "membershipAdmissionAction")

    assert "if (activeDetailTaskId.current !== task.id) return;" in refresh_detail
    assert "if (detail?.task.id !== task.id) return;" not in refresh_detail
    assert refresh_detail.index("if (activeDetailTaskId.current !== task.id) return;") < refresh_detail.index("const requestSeq = beginDetailRequest();")
    assert "if (!isActiveDetailRequest(task.id, requestSeq)) return;" in refresh_detail
    assert "if (!isActiveDetailRequest(task.id, requestSeq)) return;" in refresh_detail[refresh_detail.index("} catch (error) {"):]

    assert "async function membershipAdmissionAction(path: string, loadingKey: string, taskId: string)" in source
    assert "if (activeDetailTaskId.current !== taskId) return false;" in membership_action
    assert membership_action.index("if (activeDetailTaskId.current !== taskId) return false;") < membership_action.index("setDetail(updated);")
    assert "if (activeDetailTaskId.current !== taskId) return false;" in membership_action[membership_action.index("} catch (error) {"):]


def test_task_center_detail_requests_ignore_stale_same_task_responses():
    source = _source()
    refresh_detail = source[
        source.index("async function refreshVisibleTaskAfterAction"):
        source.index("\n\n  async function ensureTargets")
    ]
    focus_effect = source[
        source.index("if (!focusTask || appliedFocusNonce.current === focusTask.nonce) return;"):
        source.index("}, [focusTask, onFocusTaskConsumed]")
    ]
    load_detail = source[source.index("async function loadDetail(task"):source.index("\n\n  async function fetchMembershipItems")]

    assert "const activeDetailRequestSeq = React.useRef(0);" in source
    assert "function beginDetailRequest()" in source
    assert "activeDetailRequestSeq.current += 1;" in source
    assert "function isActiveDetailRequest(taskId: string, requestSeq: number)" in source

    for block, task_id in [
        (refresh_detail, "task.id"),
        (focus_effect, "focusTask.taskId"),
        (load_detail, "task.id"),
    ]:
        assert "const requestSeq = beginDetailRequest();" in block
        assert f"if (!isActiveDetailRequest({task_id}, requestSeq)) return;" in block
        fetch_index = block.index("fetchTaskDetail(")
        guard_index = block.index(f"if (!isActiveDetailRequest({task_id}, requestSeq)) return;", fetch_index)
        set_detail_index = block.index("setDetail(taskDetail);")
        catch_index = block.index("catch")
        catch_guard_index = block.index(f"if (!isActiveDetailRequest({task_id}, requestSeq)) return;", catch_index)

        assert guard_index < set_detail_index
        assert catch_guard_index < block.index("setActionError(", catch_index)


def test_task_center_action_pages_ignore_stale_page_requests_for_same_task():
    source = _source()
    action_page = source[source.index("async function loadActionPage"):source.index("\n\n  function loadActionPagesForDetail")]

    assert "const activeActionPageRequestSeq = React.useRef<Record<ActionPageKind, number>>({ planned: 0, executed: 0 });" in source
    assert "function beginActionPageRequest(kind: ActionPageKind)" in source
    assert "activeActionPageRequestSeq.current[kind] += 1;" in source
    assert "function isActiveActionPageRequest(taskId: string, kind: ActionPageKind, requestSeq: number)" in source

    assert "const requestSeq = beginActionPageRequest(kind);" in action_page
    assert "if (!isActiveActionPageRequest(taskId, kind, requestSeq)) return;" in action_page
    response_index = action_page.index("const response = await apiWithMeta<TaskCenterAction[]>")
    guard_index = action_page.index("if (!isActiveActionPageRequest(taskId, kind, requestSeq)) return;", response_index)
    rows_index = action_page.index("setRows(response.data);")
    page_index = action_page.index("setPage({ current: page, pageSize, total, loading: false });")
    catch_index = action_page.index("} catch (error) {")
    catch_guard_index = action_page.index("if (!isActiveActionPageRequest(taskId, kind, requestSeq)) return;", catch_index)
    catch_page_index = action_page.index("setPage((current) => ({ ...current, loading: false }));", catch_index)

    assert guard_index < rows_index < page_index
    assert catch_guard_index < catch_page_index


def _required_remote_target_source(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path
    assert path.exists(), f"missing frontend source: {relative_path}"
    return path.read_text()


def test_remote_operation_target_select_supports_search_errors_and_stable_selected_values():
    source = _required_remote_target_source("frontend/src/app/components/OperationTargetSelect.tsx")

    assert "useOperationTargetOptions" in source
    assert "mode?: 'multiple';" in source
    assert "showSearch" in source
    assert "filterOption={false}" in source
    assert "onSearch={search}" in source
    assert "loading={loading}" in source
    assert "status={error ? 'error' : status}" in source
    assert "notFoundContent={notFoundContent}" in source
    assert "ids: selectedIds" in source
    assert "value={value}" in source
    assert "onTargetsLoaded?.(targets);" in source
    assert "onChange" not in source[source.index("React.useEffect"):source.index("return (")]


@pytest.mark.xfail(strict=True, reason="Task 5 migrates TaskCenter to the shared remote target loader")
def test_target_support_task_center_unbounded_call_is_pending_task_5_migration():
    assert "api<OperationTarget[]>('/operation-targets')" not in _source()


def test_task_center_detail_section_pages_ignore_stale_page_requests_for_same_task():
    source = _source()
    section_page = source[source.index("async function loadDetailSectionPage"):source.index("\n\n  function loadDetailSectionsForDetail")]

    assert "const activeDetailSectionPageRequestSeq = React.useRef<Record<DetailSectionKind, number>>({" in source
    assert "function beginDetailSectionPageRequest(kind: DetailSectionKind)" in source
    assert "activeDetailSectionPageRequestSeq.current[kind] += 1;" in source
    assert "function isActiveDetailSectionPageRequest(taskId: string, kind: DetailSectionKind, requestSeq: number)" in source

    assert "const requestSeq = beginDetailSectionPageRequest(kind);" in section_page
    assert "if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;" in section_page
    response_index = section_page.index("const response = await apiWithMeta<any[]>")
    guard_index = section_page.index("if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;", response_index)
    detail_index = section_page.index("setDetail((current) => current && current.task.id === taskDetail.task.id")
    page_index = section_page.index("setDetailSectionPage(kind, { current: page, pageSize, total, loading: false });")
    catch_index = section_page.index("} catch (error) {")
    catch_guard_index = section_page.index("if (!isActiveDetailSectionPageRequest(taskDetail.task.id, kind, requestSeq)) return;", catch_index)
    catch_page_index = section_page.index("setDetailSectionPage(kind, (current) => ({ ...current, loading: false }));", catch_index)

    assert guard_index < detail_index < page_index
    assert catch_guard_index < catch_page_index


def test_task_center_membership_pages_ignore_stale_page_requests_for_same_task():
    source = _source()
    fetch_membership = source[source.index("async function fetchMembershipItems"):source.index("\n\n  async function loadMembershipPage")]

    assert "const activeMembershipPageRequestSeq = React.useRef(0);" in source
    assert "function beginMembershipPageRequest()" in source
    assert "activeMembershipPageRequestSeq.current += 1;" in source
    assert "function isActiveMembershipPageRequest(taskId: string, requestSeq: number)" in source

    assert "const requestSeq = beginMembershipPageRequest();" in fetch_membership
    assert "if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;" in fetch_membership
    response_index = fetch_membership.index("const response = await apiWithMeta<TaskMembershipItem[]>")
    guard_index = fetch_membership.index("if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;", response_index)
    page_index = fetch_membership.index("setMembershipPage({ current: page, pageSize, total, loading: false });")
    catch_index = fetch_membership.index("} catch (error) {")
    catch_guard_index = fetch_membership.index("if (!isActiveMembershipPageRequest(taskId, requestSeq)) return null;", catch_index)
    catch_page_index = fetch_membership.index("setMembershipPage((current) => ({ ...current, loading: false }));", catch_index)

    assert guard_index < page_index
    assert catch_guard_index < catch_page_index


def test_task_center_only_displays_target_profile_status_without_profile_selection():
    source = _source()
    detail_source = TASK_CENTER_DETAIL_MODAL.read_text()
    combined = f"{source}\n{detail_source}"

    assert "learning_profile_preview" in detail_source
    assert "目标画像" in combined
    assert "api('/target-profile" not in combined
    assert "api(`/target-profile" not in combined
    assert "name=\"profile_scene\"" not in combined
    assert "name=\"profile_version\"" not in combined
    assert "profile_scene:" not in combined
    assert "profile_version:" not in combined
