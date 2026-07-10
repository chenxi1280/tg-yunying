from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERVIEW_VIEW = PROJECT_ROOT / "frontend/src/app/views/OverviewView.tsx"
OVERVIEW_HOOK = PROJECT_ROOT / "frontend/src/app/hooks/useOverviewOperationData.ts"
OVERVIEW_WORKBENCH = PROJECT_ROOT / "frontend/src/app/components/OverviewTargetWorkbench.tsx"

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
    hook = OVERVIEW_HOOK.read_text()

    assert "async function fetchOperationBase(request: BaseRequest)" in hook
    assert "async function fetchTargetPage(request: TargetRequest)" in hook
    assert "const refreshOperationDataAfterAction = React.useCallback(" in hook
    assert "运营中心数据刷新失败" in hook
    assert "操作已完成" in hook

    base_refresh = hook[hook.index("async function refreshOperationBase"):hook.index("\n\nfunction useTargetPage")]
    target_refresh = hook[hook.index("async function refreshTargetPage"):hook.index("\n\nexport function useOverviewOperationData")]
    assert "await fetchOperationBase(request);" in base_refresh
    assert "state.setError(`运营中心数据刷新失败：" in base_refresh
    assert "await fetchTargetPage(request);" in target_refresh
    assert "state.setError(`运营中心数据刷新失败：" in target_refresh
    assert "await Promise.all([refreshOperationBase(base, actionLabel), refreshTargetPage(targetPage, actionLabel)]);" in hook

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
    hook = OVERVIEW_HOOK.read_text()
    base_owner = hook[hook.index("function useOperationBase"):hook.index("\n\nasync function refreshOperationBase")]
    target_owner = hook[hook.index("function useTargetPage"):hook.index("\n\nasync function refreshTargetPage")]

    assert "function beginBaseRequest(" in hook
    assert "controllerRef.current?.abort();" in hook
    assert "function isActiveBaseRequest(" in hook
    assert "function beginTargetRequest(" in hook
    assert "queryKey: targetPageQueryKey(query)" in hook
    assert "function isActiveTargetRequest(" in hook
    assert "const request = beginBaseRequest(identityRef, controllerRef);" in base_owner
    assert "if (!isActiveBaseRequest(identityRef, request)) return;" in base_owner
    assert "if (isActiveBaseRequest(identityRef, request)) setLoading(false);" in base_owner
    assert "const request = beginTargetRequest(identityRef, controllerRef, queryRef.current);" in target_owner
    assert "if (!isActiveTargetRequest(identityRef, request)) return;" in target_owner
    assert "if (isActiveTargetRequest(identityRef, request)) setLoading(false);" in target_owner


def test_overview_target_workbench_uses_bounded_target_page_then_scoped_runtime_summary():
    source = OVERVIEW_HOOK.read_text()
    workbench = OVERVIEW_WORKBENCH.read_text()
    fetch_data = source[source.index("async function fetchTargetPage"):source.index("\n\nfunction useOperationBase")]

    assert "const [query, setQuery] = React.useState<TargetPageQuery>" in source
    assert "const [total, setTotal] = React.useState(0);" in source
    assert "apiWithMeta<OperationTarget[]>(operationTargetPagePath(request.query)" in fetch_data
    assert "const targetResponse = await apiWithMeta<OperationTarget[]>" in fetch_data
    assert "const runtimePath = targetRuntimeSummaryPath(targetResponse.data.map((target) => target.id));" in fetch_data
    assert "const summaries = await api<TargetRuntimeSummary[]>(runtimePath, options);" in fetch_data
    assert "params.append('target_ids', String(targetId));" in source
    assert "params.append('target_ids', '');" in source
    assert "total: responseTotal(targetResponse.headers)" in fetch_data
    assert "current: props.targetPageQuery.page" in workbench
    assert "pageSize: props.targetPageQuery.pageSize" in workbench
    assert "total: props.targetTotal" in workbench
    assert "onChange={props.onTargetPageChange}" in workbench
    assert "api<TargetRuntimeSummary[]>('/operation-targets/runtime-summary')" not in source


def test_overview_base_reads_run_in_parallel_and_target_runtime_stays_ordered():
    source = OVERVIEW_HOOK.read_text()
    base_fetch = source[source.index("async function fetchOperationBase"):source.index("\n\nasync function fetchTargetPage")]
    target_fetch = source[source.index("async function fetchTargetPage"):source.index("\n\nfunction useOperationBase")]

    promise_all = base_fetch.index("await Promise.all([")
    promise_end = base_fetch.index("]);", promise_all)
    parallel_block = base_fetch[promise_all:promise_end]
    assert "api<OperationPlan[]>('/operation-plans'" in parallel_block
    assert "api<OperationCenterSummary>('/operation-center/overview'" in parallel_block
    assert "api<OperationIssue[]>('/operation-issues'" in parallel_block
    assert target_fetch.index("const targetResponse = await apiWithMeta") < target_fetch.index("const runtimePath = targetRuntimeSummaryPath")
    assert target_fetch.index("const runtimePath = targetRuntimeSummaryPath") < target_fetch.index("const summaries = await api<TargetRuntimeSummary[]>")


def test_overview_isolates_base_data_from_target_page_refresh_lifecycle():
    source = OVERVIEW_HOOK.read_text()
    workbench = OVERVIEW_WORKBENCH.read_text()
    base_owner = source[source.index("function useOperationBase"):source.index("\n\nasync function refreshOperationBase")]
    target_owner = source[source.index("function useTargetPage"):source.index("\n\nasync function refreshTargetPage")]
    base_fetch = source[source.index("async function fetchOperationBase"):source.index("\n\nasync function fetchTargetPage")]
    target_fetch = source[source.index("async function fetchTargetPage"):source.index("\n\nfunction useOperationBase")]

    assert "const identityRef = React.useRef<BaseRequestIdentity>" in base_owner
    assert "const controllerRef = React.useRef<AbortController | null>(null);" in base_owner
    assert "const identityRef = React.useRef<TargetRequestIdentity>" in target_owner
    assert "const controllerRef = React.useRef<AbortController | null>(null);" in target_owner
    assert "const [loading, setLoading] = React.useState(false);" in base_owner
    assert "const [loading, setLoading] = React.useState(false);" in target_owner
    assert "operationLoading: base.loading || targetPage.loading" in source
    assert "operationError: [base.error, targetPage.error].filter(Boolean).join('；')" in source
    assert "/operation-plans" in base_fetch
    assert "/operation-center/overview" in base_fetch
    assert "/operation-issues" in base_fetch
    assert "/operation-targets" not in base_fetch
    assert "operationTargetPagePath(request.query)" in target_fetch
    assert "targetRuntimeSummaryPath" in target_fetch
    assert "/operation-plans" not in target_fetch
    assert "await Promise.all([base.load(), targetPage.load()]);" in source
    assert "await Promise.all([refreshOperationBase(base, actionLabel), refreshTargetPage(targetPage, actionLabel)]);" in source
    assert "void load();\n    return () => controllerRef.current?.abort();" in base_owner
    assert "}, [load, query]);" in target_owner
    assert "loading={props.operationLoading} onClick={props.onRefresh}" in workbench


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
    close_modal = source[source.index("function closeIssueActionModal"):source.index("\n\n  async function submitIssueAction")]

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
