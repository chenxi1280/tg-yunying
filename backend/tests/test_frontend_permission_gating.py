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
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert 'name="participation_rate"' in source
    assert 'label="参与账号比例"' in source
    assert 'name="allow_account_repeat"' in source
    assert 'label="允许账号重复发言"' in source
    assert 'label="每轮总发言数"' in source
    assert '<InputNumber min={1} max={10}' not in source
    assert "小时上限控制总量" in source
    assert "参与比例按多轮统计" in source
    assert "function markMessagesPerRoundManual" in source
    assert "setFieldValue('messages_per_round_mode', 'manual')" in source
    assert "onChange={markMessagesPerRoundManual}" in source
    assert "准入策略" in source
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


def test_task_center_target_selects_support_searching():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "targetSelectProps" in source
    assert "showSearch: true" in source
    assert 'optionFilterProp: "label"' in source
    assert "<Select allowClear options={groupTargetOptions} {...targetSelectProps}" in source
    assert '<Select mode="multiple" allowClear options={groupTargetOptions} {...targetSelectProps}' in source
    assert "<Select allowClear options={channelTargetOptions} onChange={onTargetChannelChange} {...targetSelectProps}" in source
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

    assert "recommended_limits" in types
    assert "applyAiLimitRecommendations(result)" in source
    assert "form.isFieldTouched(field)" in source
    assert "max_actions_per_hour" in source
    assert "messages_per_round" in source
    assert "target_comments_per_message" in source
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


def test_task_detail_opens_before_membership_page_load():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    membership_loader = source[source.index("async function loadMembershipForDetail"):source.index("\n  async function loadDetail")]
    load_detail = source[source.index("async function loadDetail"):source.index("\n  async function fetchMembershipItems")]

    assert "setDetail(taskDetail)" in load_detail
    assert load_detail.index("setDetail(taskDetail)") < load_detail.index("loadMembershipForDetail")
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


def test_navigation_does_not_reload_full_app_snapshot_for_self_loading_views():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    assert "}, [token, taskStatusFilter, selectedPoolId, activeView]);" not in context
    assert "}, [token, taskStatusFilter, selectedPoolId]);" in context
    assert "refreshContentResourcesForActiveView" in context


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


def test_task_center_runtime_form_exposes_hour_limit_without_task_daily_cap():
    wizard = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx").read_text()
    view_model = (PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts").read_text()

    assert 'name="max_actions_per_hour" label="每小时最大发送量"' in wizard
    assert 'placeholder="预检后按账号数推荐"' in wizard
    assert 'name="max_actions_per_day" label="每日上限"' not in wizard
    assert "每日上限 ${values.max_actions_per_day" not in wizard
    assert "'max_actions_per_day'" not in view_model


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


def test_security_drawers_show_cleanup_preservation_and_managed_2fa_policy():
    drawer = (PROJECT_ROOT / "frontend/src/app/views/AccountSecurityBatchDrawer.tsx").read_text()
    modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    managed_2fa = (PROJECT_ROOT / "frontend/src/app/views/AccountManaged2FaSettingsPanel.tsx").read_text()
    router = (PROJECT_ROOT / "backend/app/api/routers/account_security.py").read_text()
    auth = (PROJECT_ROOT / "backend/app/auth.py").read_text()

    assert "不会清理 primary / standby_1 / standby_2 / 官方锚点设备" in drawer
    assert "预计清理外部设备" in drawer
    assert "平台托管 2FA" in drawer
    assert "已设置且平台托管旧密码" in drawer
    assert "旧密码未知" in drawer
    assert "AccountManaged2FaSettingsPanel" in modals
    assert "托管 2FA" in modals
    assert "密码设置 / 轮换不回显旧密码" in modals
    assert "accountId={accountDetail.account.id}" in modals
    assert "`/tg-accounts/${accountId}/security/managed-2fa`" in managed_2fa
    assert "`/tg-accounts/${accountId}/security/managed-2fa/rotate`" in managed_2fa
    assert "post_account_security_managed_2fa" in router
    assert "post_account_security_managed_2fa_rotate" in router
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
    assert "primary / standby_1 / standby_2 / 官方锚点设备" in source
    assert "目标槽位" in source
    assert "验证码读取" in source
    assert "params.set('type', nextTaskTypeFilter)" in view


def test_task_center_exposes_task_type_filter_request_parameter():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()

    assert "taskTypeFilter" in source
    assert "account_profile_init" in source
    assert "params.set('type', nextTaskTypeFilter)" in source
    assert "api<TaskCenterTask[]>(`/tasks${query ? `?${query}` : ''}`)" in source


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
