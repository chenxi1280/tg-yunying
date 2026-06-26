from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_material_save_binds_payload_files_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/contentActions.ts").read_text()
    create_body = source[source.index("async function createMaterial"):source.index("\n\n  function openMaterialEdit")]
    save_body = source[source.index("async function saveMaterial"):source.index("\n\n  async function disableMaterial")]

    assert "const materialSaveRequestRef = React.useRef({ seq: 0, materialId: null as number | null, signature: '' });" in context
    assert "materialSaveRequestRef," in context
    assert "materialSaveRequestRef: React.MutableRefObject<{ seq: number; materialId: number | null; signature: string }>;" in source

    assert "function materialPayload()" in source
    assert "function materialFilesSignature(files: File[])" in source
    assert "function materialSavePayloadSignature(materialId: number | null, tenantId: number, payload: MaterialPayload, files: File[])" in source
    assert "function beginMaterialSaveRequest(materialId: number | null, signature: string)" in source
    assert "function isCurrentMaterialSaveRequest(requestSeq: number)" in source
    assert "function isActiveMaterialSaveRequest(materialId: number | null, requestSeq: number, signature: string)" in source

    assert "const materialId = null;" in create_body
    assert "const payload = materialPayload();" in create_body
    assert "const tenantId = params.currentUser?.tenant_id ?? 1;" in create_body
    assert "const files = params.materialFile ?? [];" in create_body
    assert "const signature = materialSavePayloadSignature(materialId, tenantId, payload, files);" in create_body
    assert "const requestSeq = beginMaterialSaveRequest(materialId, signature);" in create_body
    assert "if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;" in create_body
    assert create_body.index("if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;") < create_body.index("params.setMaterialFile(null);")
    assert create_body.index("if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;", create_body.index("catch")) < create_body.index("params.handleActionError(error);")
    assert "if (isCurrentMaterialSaveRequest(requestSeq)) params.setBusy('');" in create_body

    assert "const materialId = params.materialForm.id;" in save_body
    assert "const payload = materialPayload();" in save_body
    assert "const tenantId = params.currentUser?.tenant_id ?? 1;" in save_body
    assert "const files = params.materialFile ?? [];" in save_body
    assert "const signature = materialSavePayloadSignature(materialId, tenantId, payload, files);" in save_body
    assert "const requestSeq = beginMaterialSaveRequest(materialId, signature);" in save_body
    assert "if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;") < save_body.index("params.setMaterials(")
    assert save_body.index("if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentMaterialSaveRequest(requestSeq)) params.setBusy('');" in save_body


def test_material_status_actions_bind_material_action_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/contentActions.ts").read_text()
    disable_body = source[source.index("async function disableMaterial"):source.index("\n\n  async function restoreMaterial")]
    restore_body = source[source.index("async function restoreMaterial"):source.index("\n\n  function openContentKeywordRuleEdit")]

    assert "const materialActionRequestRef = React.useRef({ seq: 0, materialId: null as number | null, action: '' });" in context
    assert "materialActionRequestRef," in context
    assert "materialActionRequestRef: React.MutableRefObject<{ seq: number; materialId: number | null; action: string }>;" in source

    assert "function beginMaterialActionRequest(materialId: number, action: string)" in source
    assert "function isCurrentMaterialActionRequest(requestSeq: number)" in source
    assert "function isActiveMaterialActionRequest(materialId: number, action: string, requestSeq: number)" in source

    assert "const action = 'disable';" in disable_body
    assert "const requestSeq = beginMaterialActionRequest(material.id, action);" in disable_body
    assert "if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;" in disable_body
    assert disable_body.index("if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;") < disable_body.index("params.setMaterials(")
    assert disable_body.index("if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;", disable_body.index("catch")) < disable_body.index("params.handleActionError(error);")
    assert "if (isCurrentMaterialActionRequest(requestSeq)) params.setBusy('');" in disable_body

    assert "const action = 'restore';" in restore_body
    assert "const requestSeq = beginMaterialActionRequest(material.id, action);" in restore_body
    assert "if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;" in restore_body
    assert restore_body.index("if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;") < restore_body.index("params.setMaterials(")
    assert restore_body.index("if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;", restore_body.index("catch")) < restore_body.index("params.handleActionError(error);")
    assert "if (isCurrentMaterialActionRequest(requestSeq)) params.setBusy('');" in restore_body


def test_keyword_rule_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/contentActions.ts").read_text()
    create_body = source[source.index("async function createContentKeywordRule"):source.index("\n\n  async function saveContentKeywordRule")]
    save_body = source[source.index("async function saveContentKeywordRule"):source.index("\n\n  return {")]

    assert "const keywordRuleSaveRequestRef = React.useRef({ seq: 0, ruleId: null as number | null, signature: '' });" in context
    assert "keywordRuleSaveRequestRef," in context
    assert "keywordRuleSaveRequestRef: React.MutableRefObject<{ seq: number; ruleId: number | null; signature: string }>;" in source

    assert "function keywordRulePayload()" in source
    assert "function keywordRulePayloadSignature(ruleId: number | null, tenantId: number, payload: ReturnType<typeof keywordRulePayload>)" in source
    assert "function beginKeywordRuleSaveRequest(ruleId: number | null, signature: string)" in source
    assert "function isCurrentKeywordRuleSaveRequest(requestSeq: number)" in source
    assert "function isActiveKeywordRuleSaveRequest(ruleId: number | null, requestSeq: number, signature: string)" in source

    assert "const ruleId = null;" in create_body
    assert "const tenantId = params.currentUser?.tenant_id ?? 1;" in create_body
    assert "const payload = keywordRulePayload();" in create_body
    assert "const signature = keywordRulePayloadSignature(ruleId, tenantId, payload);" in create_body
    assert "const requestSeq = beginKeywordRuleSaveRequest(ruleId, signature);" in create_body
    assert "if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;" in create_body
    assert create_body.index("if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;") < create_body.index("params.closeModal();")
    assert create_body.index("if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;", create_body.index("catch")) < create_body.index("params.handleActionError(error);")
    assert "if (isCurrentKeywordRuleSaveRequest(requestSeq)) params.setBusy('');" in create_body

    assert "const ruleId = params.keywordRuleForm.id;" in save_body
    assert "const tenantId = params.currentUser?.tenant_id ?? 1;" in save_body
    assert "const payload = keywordRulePayload();" in save_body
    assert "const signature = keywordRulePayloadSignature(ruleId, tenantId, payload);" in save_body
    assert "const requestSeq = beginKeywordRuleSaveRequest(ruleId, signature);" in save_body
    assert "if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;") < save_body.index("params.closeModal();")
    assert save_body.index("if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentKeywordRuleSaveRequest(requestSeq)) params.setBusy('');" in save_body
