from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_system_actions_distinguish_refresh_failure_from_write_failure():
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()

    assert "async function refreshSystemSettingsAfterAction(actionLabel: string)" in source
    assert "params.showResult('系统设置数据刷新失败'" in source
    assert "操作已完成，但刷新系统设置数据失败" in source

    helper = source[source.index("async function refreshSystemSettingsAfterAction"):source.index("\n\n  async function createDeveloperApp")]
    assert "await params.refresh();" in helper
    assert "params.handleActionError(" not in helper

    for function_name in [
        "createDeveloperApp",
        "saveTenantQuota",
        "saveTenantGroupRescueSettings",
        "saveAdminUser",
        "adjustAdminUserTokens",
        "toggleDeveloperApp",
        "checkDeveloperApp",
        "createAiProvider",
        "toggleAiProvider",
        "checkAiProvider",
        "saveTenantAiSetting",
        "createPromptTemplate",
        "savePromptTemplate",
    ]:
        start = source.index(f"\n  async function {function_name}")
        async_end = source.find("\n  async function", start + 1)
        function_end = source.find("\n  function", start + 1)
        return_end = source.find("\n  return", start + 1)
        end = min(index for index in [async_end, function_end, return_end] if index != -1)
        body = source[start:end]
        assert "await refreshSystemSettingsAfterAction(" in body
        refresh_block = body[body.index("await refreshSystemSettingsAfterAction("):]
        assert "params.handleActionError(" not in refresh_block[:refresh_block.index("} catch")]


def test_system_token_ledger_reads_ignore_stale_users_and_requests():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()

    ledger_body_start = source.index("async function loadUserTokenLedgers")
    ledger_body = source[ledger_body_start:source.index("\n\n  async function toggleDeveloperApp", ledger_body_start)]
    create_body = source[source.index("function openAdminUserCreate"):source.index("\n\n  async function saveAdminUser")]

    assert "const userTokenLedgerRequestRef = React.useRef({ userId: null as number | null, seq: 0 });" in context
    assert "userTokenLedgerRequestRef," in context
    assert "userTokenLedgerRequestRef: React.MutableRefObject<{ userId: number | null; seq: number }>;" in source

    assert "const requestSeq = params.userTokenLedgerRequestRef.current.seq + 1;" in ledger_body
    assert "params.userTokenLedgerRequestRef.current = { userId, seq: requestSeq };" in ledger_body
    assert "params.setSelectedAdminUserId(userId);" in ledger_body
    assert "params.setSelectedUserTokenLedgers([]);" in ledger_body
    assert "params.userTokenLedgerRequestRef.current.userId !== userId || params.userTokenLedgerRequestRef.current.seq !== requestSeq" in ledger_body
    assert "params.setSelectedUserTokenLedgers(ledgers);" in ledger_body
    assert ledger_body.index("params.userTokenLedgerRequestRef.current.userId !== userId || params.userTokenLedgerRequestRef.current.seq !== requestSeq") < ledger_body.index("params.setSelectedUserTokenLedgers(ledgers);")

    catch_block = ledger_body[ledger_body.index("catch (error)"):]
    assert "params.userTokenLedgerRequestRef.current.userId !== userId || params.userTokenLedgerRequestRef.current.seq !== requestSeq" in catch_block
    assert catch_block.index("return;") < catch_block.index("params.handleActionError(error);")

    finally_block = ledger_body[ledger_body.index("finally"):]
    assert "params.userTokenLedgerRequestRef.current.userId === userId && params.userTokenLedgerRequestRef.current.seq === requestSeq" in finally_block
    assert "params.setBusy('');" in finally_block

    assert "const requestSeq = params.userTokenLedgerRequestRef.current.seq + 1;" in create_body
    assert "params.userTokenLedgerRequestRef.current = { userId: null, seq: requestSeq };" in create_body
    assert create_body.index("params.userTokenLedgerRequestRef.current = { userId: null, seq: requestSeq };") < create_body.index("params.setSelectedAdminUserId(null);")
    assert "params.setSelectedUserTokenLedgers([]);" in create_body
    assert create_body.index("params.userTokenLedgerRequestRef.current = { userId: null, seq: requestSeq };") < create_body.index("params.setSelectedUserTokenLedgers([]);")
    assert create_body.index("params.setSelectedUserTokenLedgers([]);") < create_body.index("params.setSelectedAdminUserId(null);")


def test_system_developer_app_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    create_body = source[source.index("async function createDeveloperApp"):source.index("\n\n  function openDeveloperAppEdit")]

    assert "const developerAppSaveRequestRef = React.useRef({ seq: 0, appId: null as number | null, signature: '' });" in context
    assert "developerAppSaveRequestRef," in context
    assert "developerAppSaveRequestRef: React.MutableRefObject<{ seq: number; appId: number | null; signature: string }>;" in source

    assert "function developerAppPayload()" in source
    assert "function developerAppPayloadSignature(payload: Record<string, unknown>)" in source
    assert "function beginDeveloperAppSaveRequest(appId: number | null, signature: string)" in source
    assert "function isCurrentDeveloperAppSaveRequest(requestSeq: number)" in source
    assert "function isActiveDeveloperAppSaveRequest(appId: number | null, requestSeq: number, signature: string)" in source

    assert "const appId = params.developerAppForm.id;" in create_body
    assert "const payload = developerAppPayload();" in create_body
    assert "const signature = developerAppPayloadSignature(payload);" in create_body
    assert "const requestSeq = beginDeveloperAppSaveRequest(appId, signature);" in create_body
    assert "if (!isActiveDeveloperAppSaveRequest(appId, requestSeq, signature)) return;" in create_body
    assert create_body.index("if (!isActiveDeveloperAppSaveRequest(appId, requestSeq, signature)) return;") < create_body.index("params.closeModal();")
    assert create_body.index("if (!isActiveDeveloperAppSaveRequest(appId, requestSeq, signature)) return;", create_body.index("catch")) < create_body.index("params.handleActionError(error);")
    assert "if (isCurrentDeveloperAppSaveRequest(requestSeq)) params.setBusy('');" in create_body


def test_system_developer_app_actions_bind_app_action_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    toggle_body = source[source.index("async function toggleDeveloperApp"):source.index("\n\n  async function checkDeveloperApp")]
    check_body = source[source.index("async function checkDeveloperApp"):source.index("\n\n  async function createAiProvider")]

    assert "const developerAppActionRequestRef = React.useRef({ seq: 0, appId: null as number | null, action: '' });" in context
    assert "developerAppActionRequestRef," in context
    assert "developerAppActionRequestRef: React.MutableRefObject<{ seq: number; appId: number | null; action: string }>;" in source

    assert "function beginDeveloperAppActionRequest(appId: number, action: string)" in source
    assert "function isCurrentDeveloperAppActionRequest(requestSeq: number)" in source
    assert "function isActiveDeveloperAppActionRequest(appId: number, action: string, requestSeq: number)" in source

    assert "const action = app.is_active ? 'disable' : 'enable';" in toggle_body
    assert "const requestSeq = beginDeveloperAppActionRequest(app.id, action);" in toggle_body
    assert "`/developer-apps/${app.id}/${action}`" in toggle_body
    assert "if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;" in toggle_body
    assert toggle_body.index("if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;") < toggle_body.index("params.showResult(")
    assert toggle_body.index("if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;", toggle_body.index("catch")) < toggle_body.index("params.handleActionError(error);")
    assert "if (isCurrentDeveloperAppActionRequest(requestSeq)) params.setBusy('');" in toggle_body

    assert "const action = 'check';" in check_body
    assert "const requestSeq = beginDeveloperAppActionRequest(app.id, action);" in check_body
    assert "if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;" in check_body
    assert check_body.index("if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;") < check_body.index("params.showResult(")
    assert check_body.index("if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;", check_body.index("catch")) < check_body.index("params.handleActionError(error);")
    assert "if (isCurrentDeveloperAppActionRequest(requestSeq)) params.setBusy('');" in check_body


def test_system_tenant_quota_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    save_body = source[source.index("async function saveTenantQuota"):source.index("\n\n  async function saveTenantGroupRescueSettings")]

    assert "const tenantQuotaSaveRequestRef = React.useRef({ seq: 0, tenantId: null as number | null, signature: '' });" in context
    assert "tenantQuotaSaveRequestRef," in context
    assert "tenantQuotaSaveRequestRef: React.MutableRefObject<{ seq: number; tenantId: number | null; signature: string }>;" in source

    assert "function tenantQuotaPayload()" in source
    assert "function tenantQuotaPayloadSignature(payload: Record<string, unknown>)" in source
    assert "function beginTenantQuotaSaveRequest(tenantId: number, signature: string)" in source
    assert "function isCurrentTenantQuotaSaveRequest(requestSeq: number)" in source
    assert "function isActiveTenantQuotaSaveRequest(tenantId: number, requestSeq: number, signature: string)" in source

    assert "const tenantId = params.tenantForm.id;" in save_body
    assert "const payload = tenantQuotaPayload();" in save_body
    assert "const signature = tenantQuotaPayloadSignature(payload);" in save_body
    assert "const requestSeq = beginTenantQuotaSaveRequest(tenantId, signature);" in save_body
    assert "if (!isActiveTenantQuotaSaveRequest(tenantId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveTenantQuotaSaveRequest(tenantId, requestSeq, signature)) return;") < save_body.index("params.closeModal();")
    assert save_body.index("if (!isActiveTenantQuotaSaveRequest(tenantId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentTenantQuotaSaveRequest(requestSeq)) params.setBusy('');" in save_body


def test_system_group_rescue_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    save_body = source[source.index("async function saveTenantGroupRescueSettings"):source.index("\n\n  function openAdminUserEdit")]

    assert "const tenantGroupRescueSaveRequestRef = React.useRef({ seq: 0, tenantId: null as number | null, signature: '' });" in context
    assert "tenantGroupRescueSaveRequestRef," in context
    assert "tenantGroupRescueSaveRequestRef: React.MutableRefObject<{ seq: number; tenantId: number | null; signature: string }>;" in source

    assert "function tenantGroupRescuePayloadSignature(tenantId: number, payload: GroupRescueSettingsPayload)" in source
    assert "function beginTenantGroupRescueSaveRequest(tenantId: number, signature: string)" in source
    assert "function isCurrentTenantGroupRescueSaveRequest(requestSeq: number)" in source
    assert "function isActiveTenantGroupRescueSaveRequest(tenantId: number, requestSeq: number, signature: string)" in source

    assert "const signature = tenantGroupRescuePayloadSignature(tenantId, payload);" in save_body
    assert "const requestSeq = beginTenantGroupRescueSaveRequest(tenantId, signature);" in save_body
    assert "if (!isActiveTenantGroupRescueSaveRequest(tenantId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveTenantGroupRescueSaveRequest(tenantId, requestSeq, signature)) return;") < save_body.index("params.showResult(")
    assert save_body.index("if (!isActiveTenantGroupRescueSaveRequest(tenantId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentTenantGroupRescueSaveRequest(requestSeq)) params.setBusy('');" in save_body


def test_system_admin_user_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    save_body = source[source.index("async function saveAdminUser"):source.index("\n\n  async function resetAdminUserPassword")]

    assert "const adminUserSaveRequestRef = React.useRef({ seq: 0, userId: null as number | null, signature: '' });" in context
    assert "adminUserSaveRequestRef," in context
    assert "adminUserSaveRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;" in source

    assert "function adminUserPayload()" in source
    assert "function adminUserPayloadSignature(payload: Record<string, unknown>)" in source
    assert "function beginAdminUserSaveRequest(userId: number | null, signature: string)" in source
    assert "function isCurrentAdminUserSaveRequest(requestSeq: number)" in source
    assert "function isActiveAdminUserSaveRequest(userId: number | null, requestSeq: number, signature: string)" in source

    assert "const userId = params.adminUserForm.id;" in save_body
    assert "const payload = adminUserPayload();" in save_body
    assert "const signature = adminUserPayloadSignature(payload);" in save_body
    assert "const requestSeq = beginAdminUserSaveRequest(userId, signature);" in save_body
    assert "if (!isActiveAdminUserSaveRequest(userId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveAdminUserSaveRequest(userId, requestSeq, signature)) return;") < save_body.index("params.setNotice(")
    assert save_body.index("if (!isActiveAdminUserSaveRequest(userId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentAdminUserSaveRequest(requestSeq)) params.setBusy('');" in save_body


def test_system_admin_user_password_reset_binds_user_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    reset_body = source[source.index("async function resetAdminUserPassword"):source.index("\n\n  async function adjustAdminUserTokens")]

    assert "const adminUserPasswordResetRequestRef = React.useRef({ seq: 0, userId: null as number | null, signature: '' });" in context
    assert "adminUserPasswordResetRequestRef," in context
    assert "adminUserPasswordResetRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;" in source

    assert "function adminUserPasswordResetSignature(userId: number, newPassword: string)" in source
    assert "function beginAdminUserPasswordResetRequest(userId: number, signature: string)" in source
    assert "function isCurrentAdminUserPasswordResetRequest(requestSeq: number)" in source
    assert "function isActiveAdminUserPasswordResetRequest(userId: number, requestSeq: number, signature: string)" in source

    assert "const userId = user.id;" in reset_body
    assert "const signature = adminUserPasswordResetSignature(userId, newPassword);" in reset_body
    assert "const requestSeq = beginAdminUserPasswordResetRequest(userId, signature);" in reset_body
    assert "if (!isActiveAdminUserPasswordResetRequest(userId, requestSeq, signature)) return;" in reset_body
    assert reset_body.index("if (!isActiveAdminUserPasswordResetRequest(userId, requestSeq, signature)) return;") < reset_body.index("params.setNotice(")
    assert reset_body.index("if (!isActiveAdminUserPasswordResetRequest(userId, requestSeq, signature)) return;", reset_body.index("catch")) < reset_body.index("params.handleActionError(error);")
    assert "if (isCurrentAdminUserPasswordResetRequest(requestSeq)) params.setBusy('');" in reset_body


def test_system_token_adjustment_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    adjust_body = source[source.index("async function adjustAdminUserTokens"):source.index("\n\n  async function loadUserTokenLedgers")]

    assert "const tokenAdjustmentRequestRef = React.useRef({ seq: 0, userId: null as number | null, signature: '' });" in context
    assert "tokenAdjustmentRequestRef," in context
    assert "tokenAdjustmentRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;" in source

    assert "function tokenAdjustmentPayload()" in source
    assert "function tokenAdjustmentPayloadSignature(userId: number, payload: Record<string, unknown>)" in source
    assert "function beginTokenAdjustmentRequest(userId: number, signature: string)" in source
    assert "function isCurrentTokenAdjustmentRequest(requestSeq: number)" in source
    assert "function isActiveTokenAdjustmentRequest(userId: number, requestSeq: number, signature: string)" in source

    assert "const userId = user.id;" in adjust_body
    assert "const payload = tokenAdjustmentPayload();" in adjust_body
    assert "const signature = tokenAdjustmentPayloadSignature(userId, payload);" in adjust_body
    assert "const requestSeq = beginTokenAdjustmentRequest(userId, signature);" in adjust_body
    assert "if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;" in adjust_body
    assert adjust_body.index("if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;") < adjust_body.index("params.setNotice(")
    assert adjust_body.index("if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;") < adjust_body.index("await loadUserTokenLedgers(userId);")
    assert adjust_body.index("if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;", adjust_body.index("catch")) < adjust_body.index("params.handleActionError(error);")
    assert "if (isCurrentTokenAdjustmentRequest(requestSeq)) params.setBusy('');" in adjust_body


def test_system_ai_provider_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    create_body = source[source.index("async function createAiProvider"):source.index("\n\n  function openAiProviderEdit")]

    assert "const aiProviderSaveRequestRef = React.useRef({ seq: 0, providerId: null as number | null, signature: '' });" in context
    assert "aiProviderSaveRequestRef," in context
    assert "aiProviderSaveRequestRef: React.MutableRefObject<{ seq: number; providerId: number | null; signature: string }>;" in source

    assert "function aiProviderPayloadSignature(payload: Record<string, unknown>)" in source
    assert "function beginAiProviderSaveRequest(providerId: number | null, signature: string)" in source
    assert "function isCurrentAiProviderSaveRequest(requestSeq: number)" in source
    assert "function isActiveAiProviderSaveRequest(providerId: number | null, requestSeq: number, signature: string)" in source

    assert "const payload = aiProviderPayload();" in create_body
    assert "const signature = aiProviderPayloadSignature(payload);" in create_body
    assert "const requestSeq = beginAiProviderSaveRequest(providerId, signature);" in create_body
    assert "if (!isActiveAiProviderSaveRequest(providerId, requestSeq, signature)) return;" in create_body
    assert create_body.index("if (!isActiveAiProviderSaveRequest(providerId, requestSeq, signature)) return;") < create_body.index("params.closeModal();")
    assert create_body.index("if (!isActiveAiProviderSaveRequest(providerId, requestSeq, signature)) return;", create_body.index("catch")) < create_body.index("params.handleActionError(error);")
    assert "if (isCurrentAiProviderSaveRequest(requestSeq)) params.setBusy('');" in create_body


def test_system_ai_provider_actions_bind_provider_action_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    toggle_body = source[source.index("async function toggleAiProvider"):source.index("\n\n  async function checkAiProvider")]
    check_body = source[source.index("async function checkAiProvider"):source.index("\n\n  async function saveTenantAiSetting")]

    assert "const aiProviderActionRequestRef = React.useRef({ seq: 0, providerId: null as number | null, action: '' });" in context
    assert "aiProviderActionRequestRef," in context
    assert "aiProviderActionRequestRef: React.MutableRefObject<{ seq: number; providerId: number | null; action: string }>;" in source

    assert "function beginAiProviderActionRequest(providerId: number, action: string)" in source
    assert "function isCurrentAiProviderActionRequest(requestSeq: number)" in source
    assert "function isActiveAiProviderActionRequest(providerId: number, action: string, requestSeq: number)" in source

    assert "const action = provider.is_active ? 'disable' : 'enable';" in toggle_body
    assert "const requestSeq = beginAiProviderActionRequest(provider.id, action);" in toggle_body
    assert "body: JSON.stringify({ is_active: action === 'enable' })," in toggle_body
    assert "if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;" in toggle_body
    assert toggle_body.index("if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;") < toggle_body.index("params.showResult(")
    assert toggle_body.index("if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;", toggle_body.index("catch")) < toggle_body.index("params.handleActionError(error);")
    assert "if (isCurrentAiProviderActionRequest(requestSeq)) params.setBusy('');" in toggle_body

    assert "const action = 'check';" in check_body
    assert "const requestSeq = beginAiProviderActionRequest(provider.id, action);" in check_body
    assert "if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;" in check_body
    assert check_body.index("if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;") < check_body.index("params.showResult(")
    assert check_body.index("if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;", check_body.index("catch")) < check_body.index("params.handleActionError(error);")
    assert "if (isCurrentAiProviderActionRequest(requestSeq)) params.setBusy('');" in check_body


def test_system_tenant_ai_setting_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    save_body = source[source.index("async function saveTenantAiSetting"):source.index("\n\n  async function createPromptTemplate")]

    assert "const tenantAiSettingSaveRequestRef = React.useRef({ seq: 0, signature: '' });" in context
    assert "tenantAiSettingSaveRequestRef," in context
    assert "tenantAiSettingSaveRequestRef: React.MutableRefObject<{ seq: number; signature: string }>;" in source

    assert "function tenantAiSettingPayload()" in source
    assert "function tenantAiSettingPayloadSignature(payload: Record<string, unknown>)" in source
    assert "function beginTenantAiSettingSaveRequest(signature: string)" in source
    assert "function isCurrentTenantAiSettingSaveRequest(requestSeq: number)" in source
    assert "function isActiveTenantAiSettingSaveRequest(requestSeq: number, signature: string)" in source

    assert "const payload = tenantAiSettingPayload();" in save_body
    assert "const signature = tenantAiSettingPayloadSignature(payload);" in save_body
    assert "const requestSeq = beginTenantAiSettingSaveRequest(signature);" in save_body
    assert "if (!isActiveTenantAiSettingSaveRequest(requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveTenantAiSettingSaveRequest(requestSeq, signature)) return;") < save_body.index("params.closeModal();")
    assert save_body.index("if (!isActiveTenantAiSettingSaveRequest(requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentTenantAiSettingSaveRequest(requestSeq)) params.setBusy('');" in save_body


def test_system_prompt_template_save_binds_payload_signature_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/systemActions.ts").read_text()
    create_body = source[source.index("async function createPromptTemplate"):source.index("\n\n  function openPromptTemplateEdit")]
    save_start = source.index("async function savePromptTemplate")
    save_body = source[save_start:source.index("\n\n  return", save_start)]

    assert "const promptTemplateSaveRequestRef = React.useRef({ seq: 0, templateId: null as number | null, signature: '' });" in context
    assert "promptTemplateSaveRequestRef," in context
    assert "promptTemplateSaveRequestRef: React.MutableRefObject<{ seq: number; templateId: number | null; signature: string }>;" in source

    assert "function promptTemplatePayload(templateId: number | null)" in source
    assert "function promptTemplatePayloadSignature(templateId: number | null, payload: Record<string, unknown>)" in source
    assert "function beginPromptTemplateSaveRequest(templateId: number | null, signature: string)" in source
    assert "function isCurrentPromptTemplateSaveRequest(requestSeq: number)" in source
    assert "function isActivePromptTemplateSaveRequest(templateId: number | null, requestSeq: number, signature: string)" in source

    for body in [create_body, save_body]:
        assert "const payload = promptTemplatePayload(templateId);" in body
        assert "const signature = promptTemplatePayloadSignature(templateId, payload);" in body
        assert "const requestSeq = beginPromptTemplateSaveRequest(templateId, signature);" in body
        assert "if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;" in body
        assert body.index("if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;") < body.index("params.closeModal();")
        assert body.index("if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;", body.index("catch")) < body.index("params.handleActionError(error);")
        assert "if (isCurrentPromptTemplateSaveRequest(requestSeq)) params.setBusy('');" in body

    assert "const templateId = null;" in create_body
    assert "const templateId = params.promptTemplateForm.id;" in save_body
