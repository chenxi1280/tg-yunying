from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERVIEW_VIEW = PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx"

pytestmark = pytest.mark.no_postgres


def _source() -> str:
    return OVERVIEW_VIEW.read_text()


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  function", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_overview_plan_actions_surface_write_failures_and_refresh_failures():
    source = _source()

    assert "async function fetchOperationData(request: OperationDataRequest)" in source
    assert "async function refreshOperationDataAfterAction(actionLabel: string)" in source
    assert "运营中心数据刷新失败" in source
    assert "操作已完成" in source

    helper_start = source.index("async function refreshOperationDataAfterAction")
    helper_end = source.index("\n\n  async function createDefaultPlan", helper_start)
    helper = source[helper_start:helper_end]
    assert "await fetchOperationData(request);" in helper
    assert "setOperationError(`运营中心数据刷新失败：" in helper

    for function_name in [
        "createDefaultPlan",
        "previewPlan",
        "generatePlanTasks",
        "changePlanLifecycle",
        "savePlanEditor",
        "openImpactPreview",
        "confirmImpactApply",
        "submitIssueAction",
    ]:
        body = _function_body(source, function_name)
        assert "await refreshOperationDataAfterAction(" in body
        assert "catch (error)" in body
        assert "errorText(error)" in body
        assert "await loadOperationData();" not in body


def test_overview_operation_data_refreshes_ignore_stale_responses():
    source = _source()

    fetch_data = _function_body(source, "fetchOperationData")
    load_data = _function_body(source, "loadOperationData")
    refresh_data = _function_body(source, "refreshOperationDataAfterAction")

    assert "const operationDataRequestRef = React.useRef<OperationDataRequestIdentity>" in source
    assert "const operationDataAbortController = React.useRef<AbortController | null>(null);" in source
    assert "function beginOperationDataRequest(query: TargetPageQuery)" in source
    assert "operationDataAbortController.current?.abort();" in source
    assert "queryKey: targetPageQueryKey(query)" in source
    assert "function isActiveOperationDataRequest(request: OperationDataRequest)" in source
    assert "async function fetchOperationData(request: OperationDataRequest)" in source

    stale_guard = "if (!isActiveOperationDataRequest(request)) return false;"
    assert stale_guard in fetch_data
    assert fetch_data.index(stale_guard) < fetch_data.index("setPlans(planRows);")
    assert "return true;" in fetch_data

    assert "const request = beginOperationDataRequest(targetPageQueryRef.current);" in load_data
    assert "await fetchOperationData(request);" in load_data
    load_error_guard = "if (!isActiveOperationDataRequest(request)) return;"
    assert load_error_guard in load_data
    assert load_data.index(load_error_guard) < load_data.index("setOperationError(err instanceof Error ? err.message : String(err));")
    assert "if (isActiveOperationDataRequest(request)) setOperationLoading(false);" in load_data

    assert "const request = beginOperationDataRequest(targetPageQueryRef.current);" in refresh_data
    assert "await fetchOperationData(request);" in refresh_data
    refresh_error_guard = "if (!isActiveOperationDataRequest(request)) return;"
    assert refresh_error_guard in refresh_data
    assert refresh_data.index(refresh_error_guard) < refresh_data.index("setOperationError(`运营中心数据刷新失败：")


def test_overview_target_workbench_uses_bounded_target_page_then_scoped_runtime_summary():
    source = _source()
    fetch_data = _function_body(source, "fetchOperationData")

    assert "const [targetPageQuery, setTargetPageQuery] = React.useState<TargetPageQuery>" in source
    assert "const [targetTotal, setTargetTotal] = React.useState(0);" in source
    assert "apiWithMeta<OperationTarget[]>(operationTargetPagePath(request.query)" in fetch_data
    assert "const targetResponse = await targetRequest;" in fetch_data
    assert "const runtimePath = targetRuntimeSummaryPath(targetResponse.data.map((target) => target.id));" in fetch_data
    assert "api<TargetRuntimeSummary[]>(runtimePath, { signal: request.controller.signal })" in fetch_data
    assert "params.append('target_ids', String(targetId));" in source
    assert "params.append('target_ids', '');" in source
    assert "const responseTotal = operationTargetResponseTotal(targetResponse.headers);" in fetch_data
    assert "setTargetTotal(responseTotal);" in fetch_data
    assert "current: targetPageQuery.page" in source
    assert "pageSize: targetPageQuery.pageSize" in source
    assert "total: targetTotal" in source
    assert "onChange={handleTargetWorkbenchTableChange}" in source
    workbench_start = source.index('<Table<TargetWorkbenchRow>')
    workbench_end = source.index('</Table>', workbench_start) if '</Table>' in source[workbench_start:] else source.index('/>', workbench_start)
    assert "pagination={{ pageSize: 8 }}" not in source[workbench_start:workbench_end]
    assert "api<TargetRuntimeSummary[]>('/operation-targets/runtime-summary')" not in source


def test_overview_plan_target_selection_is_remote_and_not_bound_to_workbench_page():
    source = _source()
    create_default = _function_body(source, "createDefaultPlan")
    editor_start = source.index("<span>绑定目标</span>")
    editor_end = source.index("</label>", editor_start)
    editor = source[editor_start:editor_end]

    assert "import OperationTargetSelect from '../components/OperationTargetSelect';" in source
    assert "<OperationTargetSelect" in editor
    assert 'mode="multiple"' in editor
    assert "query={{ targetType: planEditForm.target_type as OperationTarget['target_type'] }}" in editor
    assert "value={planEditForm.target_ids}" in editor
    assert "options={targets.filter" not in editor
    assert "targetTotal === 1 ? targets[0] : undefined" in create_default
    assert "targets.length === 1" not in create_default
    assert "targetTotal > 1" in create_default


def test_overview_plan_actions_ignore_stale_action_responses():
    source = _source()

    assert "const activePlanActionKey = React.useRef('');" in source
    assert "function beginPlanAction(actionKey: string)" in source
    assert "activePlanActionKey.current = actionKey;" in source
    assert "function isActivePlanAction(actionKey: string)" in source

    for function_name, action_key in [
        ("createDefaultPlan", "'create'"),
        ("previewPlan", "`${plan.id}:preview`"),
        ("generatePlanTasks", "busyKey"),
        ("changePlanLifecycle", "`${plan.id}:${action}`"),
        ("openImpactPreview", "`${plan.id}:impact`"),
    ]:
        body = _function_body(source, function_name)
        assert f"const actionKey = beginPlanAction({action_key});" in body
        assert "if (!isActivePlanAction(actionKey)) return;" in body
        assert "if (!isActivePlanAction(actionKey)) return;" in body[body.index("} catch (error) {"):]
        assert "if (isActivePlanAction(actionKey)) setPlanBusy('');" in body

    for function_name, action_key, current_request_guard in [
        ("savePlanEditor", "`${editingPlan.id}:edit`", "isCurrentPlanEditSaveRequest(requestSeq)"),
        ("confirmImpactApply", "`${impactPlan.id}:apply`", "isCurrentImpactApplyRequest(requestSeq)"),
    ]:
        body = _function_body(source, function_name)
        assert f"const actionKey = beginPlanAction({action_key});" in body
        assert "if (!isActivePlanAction(actionKey)) return;" in body
        assert "if (!isActivePlanAction(actionKey)) return;" in body[body.index("} catch (error) {"):]
        assert f"if (requestSeq ? {current_request_guard} : isActivePlanAction(actionKey)) setPlanBusy('');" in body

    preview_body = _function_body(source, "previewPlan")
    assert preview_body.index("if (!isActivePlanAction(actionKey)) return;") > preview_body.index("const result = await api<OperationPlanPreview>")
    assert preview_body.index("if (!isActivePlanAction(actionKey)) return;") < preview_body.index("setPlanPreview(result);")

    impact_body = _function_body(source, "openImpactPreview")
    assert impact_body.index("if (!isActivePlanAction(actionKey)) return;") > impact_body.index("const result = await api<OperationPlanApplyResult>")
    assert impact_body.index("if (!isActivePlanAction(actionKey)) return;") < impact_body.index("setImpactResult(result);")


def test_overview_plan_editor_save_binds_payload_signature_and_session():
    source = _source()
    open_editor = source[source.index("function openPlanEditor"):source.index("\n\n  async function savePlanEditor")]
    save_editor = _function_body(source, "savePlanEditor")
    close_editor = source[source.index("function closePlanEditor"):source.index("\n\n  async function openImpactPreview")]

    assert "const activePlanEditSaveRequestRef = React.useRef<{ seq: number; planId: number | null; signature: string }>({ seq: 0, planId: null, signature: '' });" in source
    assert "function planEditSavePayloadSignature(planId: number, payload: Record<string, any>)" in source
    assert "function invalidatePlanEditSaveRequest()" in source
    assert "function beginPlanEditSaveRequest(planId: number, signature: string)" in source
    assert "function currentPlanEditSavePayloadSignature()" in source
    assert "function isCurrentPlanEditSaveRequest(requestSeq: number)" in source
    assert "function isActivePlanEditSaveRequest(planId: number, requestSeq: number, signature: string)" in source
    assert "currentPlanEditSavePayloadSignature() === signature" in source

    assert "invalidatePlanEditSaveRequest();" in open_editor
    assert "invalidatePlanEditSaveRequest();" in close_editor
    assert "setPlanEditOpen(false);" in close_editor
    assert "onClose={closePlanEditor}" in source

    assert "const planId = editingPlan.id;" in save_editor
    assert "const payload = planEditPayloadFromForm(planEditForm);" in save_editor
    assert "payloadSignature = planEditSavePayloadSignature(planId, payload);" in save_editor
    assert "requestSeq = beginPlanEditSaveRequest(planId, payloadSignature);" in save_editor
    assert "body: JSON.stringify(payload)" in save_editor
    stale_guard = "if (!isActivePlanEditSaveRequest(planId, requestSeq, payloadSignature)) return;"
    assert stale_guard in save_editor
    assert save_editor.index(stale_guard) < save_editor.index("setPlanEditOpen(false);")
    catch_block = save_editor[save_editor.index("catch (error)"):]
    catch_guard = "if (requestSeq && !isActivePlanEditSaveRequest(planId, requestSeq, payloadSignature)) return;"
    assert catch_guard in catch_block
    assert catch_block.index(catch_guard) < catch_block.index("void message.error(`保存运营方案失败：${errorText(error)}`);")
    assert "if (requestSeq ? isCurrentPlanEditSaveRequest(requestSeq) : isActivePlanAction(actionKey)) setPlanBusy('');" in save_editor


def test_overview_impact_apply_binds_reason_signature_and_session():
    source = _source()
    open_impact = _function_body(source, "openImpactPreview")
    confirm_apply = _function_body(source, "confirmImpactApply")
    close_impact = source[source.index("function closeImpactPreview"):source.index("\n\n  async function confirmImpactApply")]

    assert "const activeImpactApplyRequestRef = React.useRef<{ seq: number; planId: number | null; signature: string }>({ seq: 0, planId: null, signature: '' });" in source
    assert "function impactApplyPayloadSignature(planId: number, payload: Record<string, any>)" in source
    assert "function invalidateImpactApplyRequest()" in source
    assert "function beginImpactApplyRequest(planId: number, signature: string)" in source
    assert "function currentImpactApplyPayloadSignature()" in source
    assert "function isCurrentImpactApplyRequest(requestSeq: number)" in source
    assert "function isActiveImpactApplyRequest(planId: number, requestSeq: number, signature: string)" in source
    assert "currentImpactApplyPayloadSignature() === signature" in source

    assert "invalidateImpactApplyRequest();" in open_impact
    assert "invalidateImpactApplyRequest();" in close_impact
    assert "setImpactOpen(false);" in close_impact
    assert "onClose={closeImpactPreview}" in source

    assert "const planId = impactPlan.id;" in confirm_apply
    assert "const payload = { reason, confirm_apply: true };" in confirm_apply
    assert "payloadSignature = impactApplyPayloadSignature(planId, payload);" in confirm_apply
    assert "requestSeq = beginImpactApplyRequest(planId, payloadSignature);" in confirm_apply
    assert "body: JSON.stringify(payload)" in confirm_apply
    stale_guard = "if (!isActiveImpactApplyRequest(planId, requestSeq, payloadSignature)) return;"
    assert stale_guard in confirm_apply
    assert confirm_apply.index(stale_guard) < confirm_apply.index("setImpactResult(result);")
    catch_block = confirm_apply[confirm_apply.index("catch (error)"):]
    catch_guard = "if (requestSeq && !isActiveImpactApplyRequest(planId, requestSeq, payloadSignature)) return;"
    assert catch_guard in catch_block
    assert catch_block.index(catch_guard) < catch_block.index("void message.error(`应用关联任务失败：${errorText(error)}`);")
    assert "if (requestSeq ? isCurrentImpactApplyRequest(requestSeq) : isActivePlanAction(actionKey)) setPlanBusy('');" in confirm_apply


def test_overview_issue_action_binds_payload_signature_and_modal_session():
    source = _source()
    open_action = source[source.index("function openIssueAction"):source.index("\n\n  async function submitIssueAction")]
    submit_action = _function_body(source, "submitIssueAction")
    close_modal = source[source.index("function closeIssueActionModal"):source.index("\n\n  const targetRows")]

    assert "const activeIssueActionRequestRef = React.useRef({ seq: 0, issueId: '', signature: '' });" in source
    assert "function issueActionPayloadSignature(issueId: string, action: IssueAction, reason: string)" in source
    assert "function invalidateIssueActionRequest()" in source
    assert "function beginIssueActionRequest(issueId: string, signature: string)" in source
    assert "function currentIssueActionPayloadSignature()" in source
    assert "function isCurrentIssueActionRequest(requestSeq: number)" in source
    assert "function isActiveIssueActionRequest(issueId: string, requestSeq: number, signature: string)" in source
    assert "currentIssueActionPayloadSignature() === signature" in source

    assert "invalidateIssueActionRequest();" in open_action
    assert "invalidateIssueActionRequest();" in close_modal
    assert "setIssueAction(null);" in close_modal
    assert "onCancel={closeIssueActionModal}" in source

    assert "payloadSignature = issueActionPayloadSignature(issueId, action, reason);" in submit_action
    assert "requestSeq = beginIssueActionRequest(issueId, payloadSignature);" in submit_action
    assert "body: JSON.stringify({ reason })" in submit_action
    stale_guard = "if (!isActiveIssueActionRequest(issueId, requestSeq, payloadSignature)) return;"
    assert stale_guard in submit_action
    assert submit_action.index(stale_guard) < submit_action.index("setIssueDetail((current) => current ? { ...current, issue: updated } : current);")
    catch_block = submit_action[submit_action.index("catch (error)"):]
    catch_guard = "if (requestSeq && !isActiveIssueActionRequest(issueId, requestSeq, payloadSignature)) return;"
    assert catch_guard in catch_block
    assert catch_block.index(catch_guard) < catch_block.index("void message.error(`${actionLabel}失败：${errorText(error)}`);")
    assert "if (requestSeq ? isCurrentIssueActionRequest(requestSeq) : isActiveIssueDetail(issueId)) setIssueBusy('');" in submit_action
