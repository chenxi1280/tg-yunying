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
