from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_operation_target_detail_does_not_auto_sync_for_read_only_users():
    source = (PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx").read_text()
    start = source.index("function openDetail")
    end = source.index("\n  function openCreate", start)
    open_detail = source[start:end]

    assert "syncTargetMessages(target)" in open_detail
    assert "if (canManageTargets)" in open_detail
    assert open_detail.index("if (canManageTargets)") < open_detail.index("syncTargetMessages(target)")


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


def test_account_center_consumes_account_deep_link_query_on_load():
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    app_modals = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    account_modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()

    assert "import { useLocation } from 'react-router-dom'" in app_shell
    assert "function accountDetailTabLabel(tab: string)" in app_shell
    assert "const accountDeepLinkRef = React.useRef('')" in app_shell
    assert "const location = useLocation()" in app_shell
    assert "new URLSearchParams(location.search)" in app_shell
    assert "params.get('account_id')" in app_shell
    assert "accountDetailTabLabel(params.get('tab') || 'availability')" in app_shell
    assert "if (activeView !== 'accounts')" in app_shell
    assert "void openAccountDetail" in app_shell
    assert "setAccountDetailTab(tab)" in app_shell
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

    assert 'name="participation_rate"' in source
    assert 'label="参与账号比例"' in source
    assert 'name="allow_account_repeat"' in source
    assert 'label="允许账号重复发言"' in source
    assert 'label="每轮总发言数"' in source
    assert 'label="上下文历史条数（不是账号数）"' in source
    assert 'label="账号并发上限（账号数）"' in source


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
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    assert "完成时间" in source
    assert "dataIndex: 'completed_at'" in source
    assert "detail.membership_phase?.status" in source
    assert "detail.membership_phase?.progress_percent" in source
    assert "detail.membership_phase?.current_phase" in source
    assert "ready_account_count" in source
    assert "pending_account_count" in source


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


def test_app_refresh_does_not_replace_accounts_with_empty_fallback_on_account_api_failure():
    source = (PROJECT_ROOT / "frontend/src/app/context/refresh.ts").read_text()

    assert "if (results[3].status === 'rejected') throw results[3].reason" in source
    assert "accounts: settledValue(results[3], [] as Account[])" not in source


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

    assert "TASK_CREATE_TIMEOUT_MS" in source
    assert "timeoutMs: TASK_CREATE_TIMEOUT_MS" in source
    assert "capacity_summary" in wizard
    assert "目标每条" in wizard
    assert "最大并发" in wizard


def test_api_error_message_supports_trace_id_in_structured_detail_objects():
    source = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "typeof parsed.detail === 'object'" in source
    assert "detail.message" in source
    assert "detail.trace_id" in source


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


def test_task_center_exposes_task_type_filter_request_parameter():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "taskTypeFilter" in source
    assert "account_profile_init" in source
    assert "params.set('type', nextTaskTypeFilter)" in source
    assert "api<TaskCenterTask[]>(`/tasks${query ? `?${query}` : ''}`)" in source


def test_task_center_allows_profile_batch_delete_without_lifecycle_controls():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "canDeleteTask(task)" in source
    assert "openDangerTaskAction(task, 'delete')" in source
    assert "canManageTasks && !isSystemTask(task) && <Button size=\"small\" danger loading={busyId === `${task.id}:delete`}" not in source


def test_task_center_lifecycle_buttons_match_task_status():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "function canStartTask(task: TaskCenterTask)" in source
    assert "return !isSystemTask(task) && task.status !== 'running';" in source
    assert "function canPauseTask(task: TaskCenterTask)" in source
    assert "return !isSystemTask(task) && task.status === 'running';" in source
    assert "canManageTasks && canStartTask(task) &&" in source
    assert "canManageTasks && canPauseTask(task) &&" in source
    assert "task.status === 'paused' ? 'resume' : 'start'" in source
    assert ">{task.status === 'paused' ? '恢复' : '启动'}</Button>" not in source
    assert "disabled={task.status !== 'running'}" not in source


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


def test_api_error_message_supports_timeout_copy_and_trace_id():
    source = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert "error.status === 408" in source
    assert "请求超时，服务可能仍在处理" in source
    assert "parsed.detail && typeof parsed.detail === 'object'" in source
    assert "detail.trace_id" in source
