from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_material_cache_refresh_surfaces_list_refresh_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    refresh_block = source[source.index("async function refreshMaterialCache"):source.index("\n  function openMaterialGroups")]

    assert "onRefresh: () => void | Promise<void>;" in source
    assert "try {\n        await onRefresh();" in refresh_block
    assert "刷新素材列表失败" in refresh_block
    assert "\n      onRefresh();" not in refresh_block

    list_refresh_block = refresh_block[refresh_block.index("await onRefresh()"):]
    assert "刷新素材缓存失败" not in list_refresh_block[:list_refresh_block.index("void message.success")]


def test_material_cache_refresh_only_refreshes_open_detail_for_same_material():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    refresh_block = source[source.index("async function refreshMaterialCache"):source.index("\n  function openMaterialGroups")]

    assert "const shouldRefreshDetail = activeMaterialDetailId.current === material.id;" in refresh_block
    assert "activeMaterialDetailId.current === null || activeMaterialDetailId.current === material.id" not in refresh_block
    assert "if (shouldRefreshDetail) {" in refresh_block
    guarded_detail = refresh_block[refresh_block.index("if (shouldRefreshDetail) {"):refresh_block.index("try {\n        await onRefresh();")]
    assert "setDetailMaterial((current) => current?.id === updated.id ? updated : current);" in guarded_detail
    assert "await openMaterialDetail(updated);" in guarded_detail


def test_material_group_writes_distinguish_refresh_failure_from_write_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    save_block = source[source.index("async function saveMaterialGroup"):source.index("\n  async function toggleMaterialGroup")]
    toggle_block = source[source.index("async function toggleMaterialGroup"):source.index("\n\n  const columns")]
    helper_block = source[source.index("async function refreshMaterialGroupsAfterAction"):source.index("\n\n  function editMaterialGroup")]

    assert "async function fetchMaterialGroups(requestSeq: number)" in source
    assert "async function refreshMaterialGroupsAfterAction(actionLabel: string)" in source
    assert "素材中心数据刷新失败" in helper_block
    assert "操作已完成" in helper_block
    assert "await fetchMaterialGroups(requestSeq);" in helper_block

    assert "await refreshMaterialGroupsAfterAction('素材组保存');" in save_block
    assert "await refreshMaterialGroupsAfterAction('素材组启停');" in toggle_block

    assert "保存素材组失败" not in helper_block
    assert "更新素材组失败" not in helper_block


def test_material_group_refreshes_ignore_stale_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    fetch_block = source[source.index("async function fetchMaterialGroups"):source.index("\n\n  async function refreshMaterialGroupsAfterAction")]
    load_block = source[source.index("async function loadMaterialGroups"):source.index("\n\n  async function fetchMaterialGroups")]
    refresh_block = source[source.index("async function refreshMaterialGroupsAfterAction"):source.index("\n\n  function editMaterialGroup")]

    assert "const materialGroupRequestSeq = React.useRef(0);" in source
    assert "function beginMaterialGroupRequest()" in source
    assert "materialGroupRequestSeq.current += 1;" in source
    assert "function isActiveMaterialGroupRequest(requestSeq: number)" in source
    assert "async function fetchMaterialGroups(requestSeq: number)" in source

    stale_guard = "if (!isActiveMaterialGroupRequest(requestSeq)) return false;"
    assert stale_guard in fetch_block
    assert fetch_block.index(stale_guard) < fetch_block.index("setMaterialGroups(rows);")
    assert "return true;" in fetch_block

    assert "const requestSeq = beginMaterialGroupRequest();" in load_block
    assert "await fetchMaterialGroups(requestSeq);" in load_block
    load_error_guard = "if (!isActiveMaterialGroupRequest(requestSeq)) return;"
    assert load_error_guard in load_block
    assert load_block.index(load_error_guard) < load_block.index("void message.error(error instanceof Error ? error.message : '读取素材组失败');")
    assert "if (isActiveMaterialGroupRequest(requestSeq)) setGroupLoading(false);" in load_block

    assert "const requestSeq = beginMaterialGroupRequest();" in refresh_block
    assert "await fetchMaterialGroups(requestSeq);" in refresh_block
    refresh_error_guard = "if (!isActiveMaterialGroupRequest(requestSeq)) return;"
    assert refresh_error_guard in refresh_block
    assert refresh_block.index(refresh_error_guard) < refresh_block.index("void message.error(`素材中心数据刷新失败：")


def test_material_group_actions_ignore_stale_action_responses():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    save_block = source[source.index("async function saveMaterialGroup"):source.index("\n  async function toggleMaterialGroup")]
    toggle_block = source[source.index("async function toggleMaterialGroup"):source.index("\n\n  const columns")]

    assert "const activeMaterialGroupActionKey = React.useRef('');" in source
    assert "const activeMaterialGroupSaveRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "function beginMaterialGroupAction(actionKey: string)" in source
    assert "activeMaterialGroupActionKey.current = actionKey;" in source
    assert "function isActiveMaterialGroupAction(actionKey: string)" in source
    assert "function materialGroupSavePayloadSignature(groupId: number | null, payload: MaterialGroupForm)" in source
    assert "function beginMaterialGroupSaveRequest(signature: string)" in source
    assert "function currentMaterialGroupSavePayloadSignature()" in source
    assert "function isCurrentMaterialGroupSaveRequest(request: { seq: number; signature: string })" in source

    assert "const actionKey = beginMaterialGroupAction(editingGroup ? `group-save:${editingGroup.id}` : 'group-create');" in save_block
    assert "const saveRequest = beginMaterialGroupSaveRequest(currentMaterialGroupSavePayloadSignature());" in save_block
    assert "if (!isActiveMaterialGroupAction(actionKey)) return;" in save_block
    assert "if (!isCurrentMaterialGroupSaveRequest(saveRequest)) return;" in save_block
    assert save_block.index("if (!isActiveMaterialGroupAction(actionKey)) return;") > save_block.index("await api")
    assert save_block.index("if (!isActiveMaterialGroupAction(actionKey)) return;") < save_block.index("setEditingGroup(null);")
    assert save_block.index("if (!isCurrentMaterialGroupSaveRequest(saveRequest)) return;") < save_block.index("setEditingGroup(null);")
    assert "if (!isActiveMaterialGroupAction(actionKey)) return;" in save_block[save_block.index("} catch (error) {"):]
    assert "if (!isCurrentMaterialGroupSaveRequest(saveRequest)) return;" in save_block[save_block.index("} catch (error) {"):]
    assert "if (isActiveMaterialGroupAction(actionKey)) setGroupSaving(false);" in save_block

    assert "const actionKey = beginMaterialGroupAction(`group-toggle:${group.id}`);" in toggle_block
    assert "if (!isActiveMaterialGroupAction(actionKey)) return;" in toggle_block
    assert toggle_block.index("if (!isActiveMaterialGroupAction(actionKey)) return;") > toggle_block.index("await api")
    assert toggle_block.index("if (!isActiveMaterialGroupAction(actionKey)) return;") < toggle_block.index("await refreshMaterialGroupsAfterAction('素材组启停');")
    assert "if (!isActiveMaterialGroupAction(actionKey)) return;" in toggle_block[toggle_block.index("} catch (error) {"):]
    assert "if (isActiveMaterialGroupAction(actionKey)) setGroupSaving(false);" in toggle_block


def test_material_detail_loads_preserve_partial_success():
    source = (PROJECT_ROOT / "frontend/src/app/views/MaterialsView.tsx").read_text()
    open_block = source[source.index("async function openMaterialDetail"):source.index("\n  async function refreshMaterialCache")]

    assert "Promise.allSettled" in open_block
    assert "detailResult.status === 'fulfilled'" in open_block
    assert "referencesResult.status === 'fulfilled'" in open_block
    assert "versionsResult.status === 'fulfilled'" in open_block
    assert "setDetailMaterial(detailResult.value)" in open_block
    assert "setMaterialReferences(referencesResult.value)" in open_block
    assert "setMaterialVersions(versionsResult.value)" in open_block
    assert "读取素材详情失败" in open_block
    assert "api<Material>(`/materials/${material.id}`)" in open_block
    assert "api<MaterialReferences>(`/materials/${material.id}/references`)" in open_block
    assert "api<MaterialVersionHistory>(`/materials/${material.id}/versions`)" in open_block
    assert "const [detail, references, versions] = await Promise.all([" not in open_block
