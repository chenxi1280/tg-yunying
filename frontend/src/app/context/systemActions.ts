import type React from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api } from '../../shared/api/client';
import type {
  AdminUser,
  AdminUserForm,
  AiProvider,
  CurrentUser,
  DeveloperApp,
  ModalState,
  PromptTemplate,
  Tenant,
  TenantAiSetting,
  TokenLedger,
} from '../types';
import type { TenantForm } from './types';

type GroupRescueSettingsPayload = {
  group_rescue_enabled: boolean;
  group_rescue_admin_account_id: number | null;
};

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

interface SystemActionParams {
  adminUserForm: AdminUserForm;
  aiProviderForm: { id: number | null; provider_name: string; base_url: string; model_name: string; api_key: string; api_key_header: string; notes: string; is_active: boolean };
  currentUser: CurrentUser | null;
  developerAppForm: { id: number | null; app_name: string; api_id: string; api_hash: string; max_accounts: number; notes: string; is_active: boolean };
  promptTemplateForm: { id: number | null; name: string; template_type: string; content: string; is_active: boolean };
  selectedAiProviderId: number | '';
  tenantAiSetting: TenantAiSetting | null;
  tenantForm: TenantForm;
  tokenAdjustmentForm: { delta_tokens: number; reason: string };
  tokenAdjustmentRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;
  developerAppSaveRequestRef: React.MutableRefObject<{ seq: number; appId: number | null; signature: string }>;
  developerAppActionRequestRef: React.MutableRefObject<{ seq: number; appId: number | null; action: string }>;
  tenantQuotaSaveRequestRef: React.MutableRefObject<{ seq: number; tenantId: number | null; signature: string }>;
  tenantGroupRescueSaveRequestRef: React.MutableRefObject<{ seq: number; tenantId: number | null; signature: string }>;
  adminUserSaveRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;
  adminUserPasswordResetRequestRef: React.MutableRefObject<{ seq: number; userId: number | null; signature: string }>;
  aiProviderSaveRequestRef: React.MutableRefObject<{ seq: number; providerId: number | null; signature: string }>;
  aiProviderActionRequestRef: React.MutableRefObject<{ seq: number; providerId: number | null; action: string }>;
  tenantAiSettingSaveRequestRef: React.MutableRefObject<{ seq: number; signature: string }>;
  promptTemplateSaveRequestRef: React.MutableRefObject<{ seq: number; templateId: number | null; signature: string }>;
  userTokenLedgerRequestRef: React.MutableRefObject<{ userId: number | null; seq: number }>;
  closeModal: () => void;
  handleActionError: (error: unknown) => void;
  refresh: () => Promise<void>;
  setAdminUserForm: Dispatch<SetStateAction<AdminUserForm>>;
  setAiProviderForm: (form: SystemActionParams['aiProviderForm']) => void;
  setBusy: (busy: string) => void;
  setDeveloperAppForm: (form: SystemActionParams['developerAppForm']) => void;
  setModal: (modal: ModalState) => void;
  setNotice: (notice: string) => void;
  setPromptTemplateForm: (form: SystemActionParams['promptTemplateForm']) => void;
  setPromptTemplates: Dispatch<SetStateAction<PromptTemplate[]>>;
  setSelectedAdminUserId: (id: number | null) => void;
  setSelectedUserTokenLedgers: (ledgers: TokenLedger[]) => void;
  setTenantForm: (form: SystemActionParams['tenantForm']) => void;
  showResult: (title: string, detail: string) => void;
}

export function createSystemActions(params: SystemActionParams) {
  async function refreshSystemSettingsAfterAction(actionLabel: string) {
    try {
      await params.refresh();
    } catch (error) {
      params.showResult('系统设置数据刷新失败', `${actionLabel}操作已完成，但刷新系统设置数据失败：${errorText(error)}`);
    }
  }

  function developerAppPayload() {
    return {
      app_name: params.developerAppForm.app_name,
      api_id: Number(params.developerAppForm.api_id),
      api_hash: params.developerAppForm.api_hash || undefined,
      max_accounts: Number(params.developerAppForm.max_accounts),
      notes: params.developerAppForm.notes,
      is_active: params.developerAppForm.is_active,
    };
  }

  function developerAppPayloadSignature(payload: Record<string, unknown>) {
    return JSON.stringify(payload);
  }

  function beginDeveloperAppSaveRequest(appId: number | null, signature: string) {
    const requestSeq = params.developerAppSaveRequestRef.current.seq + 1;
    params.developerAppSaveRequestRef.current = { seq: requestSeq, appId, signature };
    return requestSeq;
  }

  function isCurrentDeveloperAppSaveRequest(requestSeq: number) {
    return params.developerAppSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveDeveloperAppSaveRequest(appId: number | null, requestSeq: number, signature: string) {
    return isCurrentDeveloperAppSaveRequest(requestSeq)
      && params.developerAppSaveRequestRef.current.appId === appId
      && params.developerAppSaveRequestRef.current.signature === signature
      && params.developerAppForm.id === appId
      && developerAppPayloadSignature(developerAppPayload()) === signature;
  }

  function beginDeveloperAppActionRequest(appId: number, action: string) {
    const requestSeq = params.developerAppActionRequestRef.current.seq + 1;
    params.developerAppActionRequestRef.current = { seq: requestSeq, appId, action };
    return requestSeq;
  }

  function isCurrentDeveloperAppActionRequest(requestSeq: number) {
    return params.developerAppActionRequestRef.current.seq === requestSeq;
  }

  function isActiveDeveloperAppActionRequest(appId: number, action: string, requestSeq: number) {
    return isCurrentDeveloperAppActionRequest(requestSeq)
      && params.developerAppActionRequestRef.current.appId === appId
      && params.developerAppActionRequestRef.current.action === action;
  }

  function tenantQuotaPayload() {
    return {
      name: params.tenantForm.name,
      plan_name: params.tenantForm.plan_name,
      account_quota: params.tenantForm.account_quota,
      task_quota: params.tenantForm.task_quota,
    };
  }

  function tenantQuotaPayloadSignature(payload: Record<string, unknown>) {
    return JSON.stringify(payload);
  }

  function beginTenantQuotaSaveRequest(tenantId: number, signature: string) {
    const requestSeq = params.tenantQuotaSaveRequestRef.current.seq + 1;
    params.tenantQuotaSaveRequestRef.current = { seq: requestSeq, tenantId, signature };
    return requestSeq;
  }

  function isCurrentTenantQuotaSaveRequest(requestSeq: number) {
    return params.tenantQuotaSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveTenantQuotaSaveRequest(tenantId: number, requestSeq: number, signature: string) {
    return isCurrentTenantQuotaSaveRequest(requestSeq)
      && params.tenantQuotaSaveRequestRef.current.tenantId === tenantId
      && params.tenantQuotaSaveRequestRef.current.signature === signature
      && params.tenantForm.id === tenantId
      && tenantQuotaPayloadSignature(tenantQuotaPayload()) === signature;
  }

  function tenantGroupRescuePayloadSignature(tenantId: number, payload: GroupRescueSettingsPayload) {
    return JSON.stringify({ tenantId, payload });
  }

  function beginTenantGroupRescueSaveRequest(tenantId: number, signature: string) {
    const requestSeq = params.tenantGroupRescueSaveRequestRef.current.seq + 1;
    params.tenantGroupRescueSaveRequestRef.current = { seq: requestSeq, tenantId, signature };
    return requestSeq;
  }

  function isCurrentTenantGroupRescueSaveRequest(requestSeq: number) {
    return params.tenantGroupRescueSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveTenantGroupRescueSaveRequest(tenantId: number, requestSeq: number, signature: string) {
    return isCurrentTenantGroupRescueSaveRequest(requestSeq)
      && params.tenantGroupRescueSaveRequestRef.current.tenantId === tenantId
      && params.tenantGroupRescueSaveRequestRef.current.signature === signature;
  }

  function adminUserPayload() {
    return {
      name: params.adminUserForm.name,
      role: params.adminUserForm.role,
      role_template: params.adminUserForm.role_template,
      subscription_status: params.adminUserForm.subscription_status,
      menu_permissions: params.adminUserForm.permissions,
      permissions: params.adminUserForm.permissions,
      is_active: params.adminUserForm.is_active,
      ...(!params.adminUserForm.id ? { password: params.adminUserForm.password } : {}),
    };
  }

  function adminUserPayloadSignature(payload: Record<string, unknown>) {
    return JSON.stringify(payload);
  }

  function beginAdminUserSaveRequest(userId: number | null, signature: string) {
    const requestSeq = params.adminUserSaveRequestRef.current.seq + 1;
    params.adminUserSaveRequestRef.current = { seq: requestSeq, userId, signature };
    return requestSeq;
  }

  function isCurrentAdminUserSaveRequest(requestSeq: number) {
    return params.adminUserSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveAdminUserSaveRequest(userId: number | null, requestSeq: number, signature: string) {
    return isCurrentAdminUserSaveRequest(requestSeq)
      && params.adminUserSaveRequestRef.current.userId === userId
      && params.adminUserSaveRequestRef.current.signature === signature
      && params.adminUserForm.id === userId
      && adminUserPayloadSignature(adminUserPayload()) === signature;
  }

  function tokenAdjustmentPayload() {
    return {
      delta_tokens: params.tokenAdjustmentForm.delta_tokens,
      reason: params.tokenAdjustmentForm.reason,
    };
  }

  function tokenAdjustmentPayloadSignature(userId: number, payload: Record<string, unknown>) {
    return JSON.stringify({ userId, payload });
  }

  function beginTokenAdjustmentRequest(userId: number, signature: string) {
    const requestSeq = params.tokenAdjustmentRequestRef.current.seq + 1;
    params.tokenAdjustmentRequestRef.current = { seq: requestSeq, userId, signature };
    return requestSeq;
  }

  function isCurrentTokenAdjustmentRequest(requestSeq: number) {
    return params.tokenAdjustmentRequestRef.current.seq === requestSeq;
  }

  function isActiveTokenAdjustmentRequest(userId: number, requestSeq: number, signature: string) {
    return isCurrentTokenAdjustmentRequest(requestSeq)
      && params.tokenAdjustmentRequestRef.current.userId === userId
      && params.tokenAdjustmentRequestRef.current.signature === signature
      && params.userTokenLedgerRequestRef.current.userId === userId
      && tokenAdjustmentPayloadSignature(userId, tokenAdjustmentPayload()) === signature;
  }

  function adminUserPasswordResetSignature(userId: number, newPassword: string) {
    return JSON.stringify({ userId, newPassword });
  }

  function beginAdminUserPasswordResetRequest(userId: number, signature: string) {
    const requestSeq = params.adminUserPasswordResetRequestRef.current.seq + 1;
    params.adminUserPasswordResetRequestRef.current = { seq: requestSeq, userId, signature };
    return requestSeq;
  }

  function isCurrentAdminUserPasswordResetRequest(requestSeq: number) {
    return params.adminUserPasswordResetRequestRef.current.seq === requestSeq;
  }

  function isActiveAdminUserPasswordResetRequest(userId: number, requestSeq: number, signature: string) {
    return isCurrentAdminUserPasswordResetRequest(requestSeq)
      && params.adminUserPasswordResetRequestRef.current.userId === userId
      && params.adminUserPasswordResetRequestRef.current.signature === signature
      && params.adminUserForm.id === userId;
  }

  function aiProviderPayload() {
    return {
      ...params.aiProviderForm,
      provider_type: 'openai_compatible',
      api_key: params.aiProviderForm.api_key || undefined,
    };
  }

  function aiProviderPayloadSignature(payload: Record<string, unknown>) {
    return JSON.stringify(payload);
  }

  function beginAiProviderSaveRequest(providerId: number | null, signature: string) {
    const requestSeq = params.aiProviderSaveRequestRef.current.seq + 1;
    params.aiProviderSaveRequestRef.current = { seq: requestSeq, providerId, signature };
    return requestSeq;
  }

  function isCurrentAiProviderSaveRequest(requestSeq: number) {
    return params.aiProviderSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveAiProviderSaveRequest(providerId: number | null, requestSeq: number, signature: string) {
    return isCurrentAiProviderSaveRequest(requestSeq)
      && params.aiProviderSaveRequestRef.current.providerId === providerId
      && params.aiProviderSaveRequestRef.current.signature === signature
      && aiProviderPayloadSignature(aiProviderPayload()) === signature;
  }

  function beginAiProviderActionRequest(providerId: number, action: string) {
    const requestSeq = params.aiProviderActionRequestRef.current.seq + 1;
    params.aiProviderActionRequestRef.current = { seq: requestSeq, providerId, action };
    return requestSeq;
  }

  function isCurrentAiProviderActionRequest(requestSeq: number) {
    return params.aiProviderActionRequestRef.current.seq === requestSeq;
  }

  function isActiveAiProviderActionRequest(providerId: number, action: string, requestSeq: number) {
    return isCurrentAiProviderActionRequest(requestSeq)
      && params.aiProviderActionRequestRef.current.providerId === providerId
      && params.aiProviderActionRequestRef.current.action === action;
  }

  function tenantAiSettingPayload() {
    return {
      default_provider_id: params.selectedAiProviderId || null,
      ai_enabled: params.tenantAiSetting?.ai_enabled,
      fallback_to_mock: params.tenantAiSetting?.fallback_to_mock,
      ai_group_model_fallback_enabled: params.tenantAiSetting?.ai_group_model_fallback_enabled,
      ai_group_grok_fallback_enabled: params.tenantAiSetting?.ai_group_grok_fallback_enabled,
      ai_group_static_fallback_enabled: params.tenantAiSetting?.ai_group_static_fallback_enabled,
      temperature: params.tenantAiSetting?.temperature,
      max_tokens: params.tenantAiSetting?.max_tokens,
    };
  }

  function tenantAiSettingPayloadSignature(payload: Record<string, unknown>) {
    return JSON.stringify(payload);
  }

  function beginTenantAiSettingSaveRequest(signature: string) {
    const requestSeq = params.tenantAiSettingSaveRequestRef.current.seq + 1;
    params.tenantAiSettingSaveRequestRef.current = { seq: requestSeq, signature };
    return requestSeq;
  }

  function isCurrentTenantAiSettingSaveRequest(requestSeq: number) {
    return params.tenantAiSettingSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveTenantAiSettingSaveRequest(requestSeq: number, signature: string) {
    return isCurrentTenantAiSettingSaveRequest(requestSeq)
      && params.tenantAiSettingSaveRequestRef.current.signature === signature
      && tenantAiSettingPayloadSignature(tenantAiSettingPayload()) === signature;
  }

  function promptTemplatePayload(templateId: number | null) {
    const { id: _id, ...payload } = params.promptTemplateForm;
    if (templateId !== null) return payload;
    return { ...payload, tenant_id: params.currentUser?.tenant_id ?? 1 };
  }

  function promptTemplatePayloadSignature(templateId: number | null, payload: Record<string, unknown>) {
    return JSON.stringify({ templateId, payload });
  }

  function beginPromptTemplateSaveRequest(templateId: number | null, signature: string) {
    const requestSeq = params.promptTemplateSaveRequestRef.current.seq + 1;
    params.promptTemplateSaveRequestRef.current = { seq: requestSeq, templateId, signature };
    return requestSeq;
  }

  function isCurrentPromptTemplateSaveRequest(requestSeq: number) {
    return params.promptTemplateSaveRequestRef.current.seq === requestSeq;
  }

  function isActivePromptTemplateSaveRequest(templateId: number | null, requestSeq: number, signature: string) {
    return isCurrentPromptTemplateSaveRequest(requestSeq)
      && params.promptTemplateSaveRequestRef.current.templateId === templateId
      && params.promptTemplateSaveRequestRef.current.signature === signature
      && params.promptTemplateForm.id === templateId
      && promptTemplatePayloadSignature(templateId, promptTemplatePayload(templateId)) === signature;
  }

  async function createDeveloperApp() {
    const editing = params.developerAppForm.id !== null;
    const appId = params.developerAppForm.id;
    const payload = developerAppPayload();
    const signature = developerAppPayloadSignature(payload);
    const requestSeq = beginDeveloperAppSaveRequest(appId, signature);
    params.setBusy(editing ? '保存开发者应用' : '新增开发者应用');
    try {
      const saved = editing
        ? await api<DeveloperApp>(`/developer-apps/${appId}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<DeveloperApp>('/developer-apps', { method: 'POST', body: JSON.stringify(payload) });
      if (!isActiveDeveloperAppSaveRequest(appId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult(editing ? '开发者应用已保存' : '开发者应用已新增', `${saved.app_name} 当前状态：${saved.health_status}`);
      params.setDeveloperAppForm({ id: null, app_name: 'Telegram 开发者应用', api_id: '', api_hash: '', max_accounts: 0, notes: '', is_active: true });
      await refreshSystemSettingsAfterAction(editing ? '开发者应用保存' : '开发者应用新增');
    } catch (error) {
      if (!isActiveDeveloperAppSaveRequest(appId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentDeveloperAppSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  function openDeveloperAppEdit(app: DeveloperApp) {
    params.setDeveloperAppForm({
      id: app.id,
      app_name: app.app_name,
      api_id: String(app.api_id),
      api_hash: '',
      max_accounts: app.max_accounts,
      notes: app.notes,
      is_active: app.is_active,
    });
    params.setModal({ type: 'developerAppEdit' });
  }

  function openTenantEdit(tenant: Tenant) {
    params.setTenantForm({
      id: tenant.id,
      name: tenant.name,
      plan_name: tenant.plan_name,
      account_quota: tenant.account_quota,
      task_quota: tenant.task_quota,
    });
    params.setModal({ type: 'tenantEdit' });
  }

  async function saveTenantQuota() {
    if (!params.tenantForm.id) return;
    const tenantId = params.tenantForm.id;
    const payload = tenantQuotaPayload();
    const signature = tenantQuotaPayloadSignature(payload);
    const requestSeq = beginTenantQuotaSaveRequest(tenantId, signature);
    params.setBusy('保存租户配额');
    try {
      await api(`/tenants/${tenantId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActiveTenantQuotaSaveRequest(tenantId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult('运营空间配置已更新', `${params.tenantForm.name} 的任务配额已保存。`);
      await refreshSystemSettingsAfterAction('运营空间配置保存');
    } catch (error) {
      if (!isActiveTenantQuotaSaveRequest(tenantId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentTenantQuotaSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function saveTenantGroupRescueSettings(tenantId: number, payload: GroupRescueSettingsPayload) {
    const signature = tenantGroupRescuePayloadSignature(tenantId, payload);
    const requestSeq = beginTenantGroupRescueSaveRequest(tenantId, signature);
    params.setBusy('保存群聊救援配置');
    try {
      await api(`/tenant-group-rescue-settings?tenant_id=${tenantId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActiveTenantGroupRescueSaveRequest(tenantId, requestSeq, signature)) return;
      params.showResult('群聊救援配置已保存', payload.group_rescue_enabled ? '救援配置已启用，专职处置账号不会参与普通任务。' : '群聊救援已关闭。');
      await refreshSystemSettingsAfterAction('群聊救援配置保存');
    } catch (error) {
      if (!isActiveTenantGroupRescueSaveRequest(tenantId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentTenantGroupRescueSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  function openAdminUserEdit(user: AdminUser) {
    params.setAdminUserForm({
      id: user.id,
      name: user.name,
      password: '',
      role: user.role,
      role_template: user.role_template,
      subscription_status: user.subscription_status,
      menu_permissions: user.permissions?.includes('*') ? ['*'] : (user.permissions ?? user.menu_permissions),
      permissions: user.permissions?.includes('*') ? ['*'] : (user.permissions ?? user.menu_permissions),
      is_active: user.is_active,
    });
    params.setSelectedAdminUserId(user.id);
    void loadUserTokenLedgers(user.id);
    params.setModal({ type: 'adminUserEdit' });
  }

  function openAdminUserCreate() {
    const requestSeq = params.userTokenLedgerRequestRef.current.seq + 1;
    params.userTokenLedgerRequestRef.current = { userId: null, seq: requestSeq };
    params.setSelectedUserTokenLedgers([]);
    const permissions = ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'];
    params.setAdminUserForm({
      id: null,
      name: '',
      password: '',
      role: '后台用户',
      role_template: '账号添加专员',
      subscription_status: 'active',
      menu_permissions: permissions,
      permissions,
      is_active: true,
    });
    params.setSelectedAdminUserId(null);
    params.setModal({ type: 'adminUserEdit' });
  }

  async function saveAdminUser() {
    const userId = params.adminUserForm.id;
    const payload = adminUserPayload();
    const signature = adminUserPayloadSignature(payload);
    const requestSeq = beginAdminUserSaveRequest(userId, signature);
    params.setBusy('保存用户');
    try {
      const saved = await api<AdminUser>(userId ? `/admin/users/${userId}` : '/admin/users', {
        method: userId ? 'PATCH' : 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActiveAdminUserSaveRequest(userId, requestSeq, signature)) return;
      params.setNotice(`用户已保存：${saved.name}`);
      params.closeModal();
      await refreshSystemSettingsAfterAction('后台用户保存');
    } catch (error) {
      if (!isActiveAdminUserSaveRequest(userId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAdminUserSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function resetAdminUserPassword(user: AdminUser, newPassword: string) {
    const userId = user.id;
    const signature = adminUserPasswordResetSignature(userId, newPassword);
    const requestSeq = beginAdminUserPasswordResetRequest(userId, signature);
    params.setBusy('重置密码');
    try {
      await api<AdminUser>(`/admin/users/${userId}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ new_password: newPassword }),
      });
      if (!isActiveAdminUserPasswordResetRequest(userId, requestSeq, signature)) return;
      params.setNotice(`${user.name} 的密码已重置。`);
    } catch (error) {
      if (!isActiveAdminUserPasswordResetRequest(userId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAdminUserPasswordResetRequest(requestSeq)) params.setBusy('');
    }
  }

  async function adjustAdminUserTokens(user: AdminUser) {
    const userId = user.id;
    const payload = tokenAdjustmentPayload();
    const signature = tokenAdjustmentPayloadSignature(userId, payload);
    const requestSeq = beginTokenAdjustmentRequest(userId, signature);
    params.setBusy('调整 Token');
    try {
      const saved = await api<AdminUser>(`/admin/users/${userId}/token-adjustments`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;
      params.setNotice(`${saved.name} 当前 Token 余额：${saved.token_balance}`);
      await loadUserTokenLedgers(userId);
      await refreshSystemSettingsAfterAction('Token 调整');
    } catch (error) {
      if (!isActiveTokenAdjustmentRequest(userId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentTokenAdjustmentRequest(requestSeq)) params.setBusy('');
    }
  }

  async function loadUserTokenLedgers(userId: number) {
    const requestSeq = params.userTokenLedgerRequestRef.current.seq + 1;
    params.userTokenLedgerRequestRef.current = { userId, seq: requestSeq };
    params.setBusy('读取 Token 流水');
    params.setSelectedAdminUserId(userId);
    params.setSelectedUserTokenLedgers([]);
    try {
      const ledgers = await api<TokenLedger[]>(`/admin/users/${userId}/token-ledgers`);
      if (params.userTokenLedgerRequestRef.current.userId !== userId || params.userTokenLedgerRequestRef.current.seq !== requestSeq) return;
      params.setSelectedUserTokenLedgers(ledgers);
    } catch (error) {
      if (params.userTokenLedgerRequestRef.current.userId !== userId || params.userTokenLedgerRequestRef.current.seq !== requestSeq) return;
      params.handleActionError(error);
    } finally {
      if (params.userTokenLedgerRequestRef.current.userId === userId && params.userTokenLedgerRequestRef.current.seq === requestSeq) params.setBusy('');
    }
  }

  async function toggleDeveloperApp(app: DeveloperApp) {
    const action = app.is_active ? 'disable' : 'enable';
    const requestSeq = beginDeveloperAppActionRequest(app.id, action);
    params.setBusy(app.is_active ? '禁用开发者应用' : '启用开发者应用');
    try {
      const updated = await api<DeveloperApp>(`/developer-apps/${app.id}/${action}`, { method: 'POST' });
      if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;
      params.showResult('开发者应用状态已更新', `${updated.app_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await refreshSystemSettingsAfterAction('开发者应用状态更新');
    } catch (error) {
      if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentDeveloperAppActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function checkDeveloperApp(app: DeveloperApp) {
    const action = 'check';
    const requestSeq = beginDeveloperAppActionRequest(app.id, action);
    params.setBusy('检查开发者应用');
    try {
      const checked = await api<DeveloperApp>(`/developer-apps/${app.id}/check`, { method: 'POST' });
      if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;
      params.showResult('检查完成', `${checked.app_name}：${checked.health_status}`);
      await refreshSystemSettingsAfterAction('开发者应用检查');
    } catch (error) {
      if (!isActiveDeveloperAppActionRequest(app.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentDeveloperAppActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function createAiProvider() {
    const editing = params.aiProviderForm.id !== null;
    const providerId = params.aiProviderForm.id;
    const payload = aiProviderPayload();
    const signature = aiProviderPayloadSignature(payload);
    const requestSeq = beginAiProviderSaveRequest(providerId, signature);
    params.setBusy(editing ? '保存 AI 供应商' : '新增 AI 供应商');
    try {
      const saved = editing
        ? await api<AiProvider>(`/ai-providers/${providerId}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<AiProvider>('/ai-providers', { method: 'POST', body: JSON.stringify(payload) });
      if (!isActiveAiProviderSaveRequest(providerId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult(editing ? 'AI 供应商已保存' : 'AI 供应商已新增', `${saved.provider_name} 当前状态：${saved.health_status}`);
      params.setAiProviderForm({ id: null, provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com', model_name: 'deepseek-v4-flash', api_key: '', api_key_header: 'Authorization', notes: '', is_active: true });
      await refreshSystemSettingsAfterAction(editing ? 'AI 供应商保存' : 'AI 供应商新增');
    } catch (error) {
      if (!isActiveAiProviderSaveRequest(providerId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAiProviderSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  function openAiProviderEdit(provider: AiProvider) {
    params.setAiProviderForm({
      id: provider.id,
      provider_name: provider.provider_name,
      base_url: provider.base_url,
      model_name: provider.model_name,
      api_key: '',
      api_key_header: provider.api_key_header,
      notes: provider.notes,
      is_active: provider.is_active,
    });
    params.setModal({ type: 'aiProviderEdit' });
  }

  async function toggleAiProvider(provider: AiProvider) {
    const action = provider.is_active ? 'disable' : 'enable';
    const requestSeq = beginAiProviderActionRequest(provider.id, action);
    params.setBusy(provider.is_active ? '禁用 AI 供应商' : '启用 AI 供应商');
    try {
      const updated = await api<AiProvider>(`/ai-providers/${provider.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: action === 'enable' }),
      });
      if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;
      params.showResult('AI 供应商状态已更新', `${updated.provider_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await refreshSystemSettingsAfterAction('AI 供应商状态更新');
    } catch (error) {
      if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAiProviderActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function checkAiProvider(provider: AiProvider) {
    const action = 'check';
    const requestSeq = beginAiProviderActionRequest(provider.id, action);
    params.setBusy('检查 AI 供应商');
    try {
      const checked = await api<AiProvider>(`/ai-providers/${provider.id}/check`, { method: 'POST' });
      const detailLabel = checked.health_status === '健康' ? '警告' : '错误';
      const errorSummary = checked.last_error ? `，${detailLabel}：${checked.last_error.slice(0, 220)}${checked.last_error.length > 220 ? '...' : ''}` : '';
      if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;
      params.showResult('AI 供应商检查完成', `${checked.provider_name}：${checked.health_status}${errorSummary}`);
      await refreshSystemSettingsAfterAction('AI 供应商检查');
    } catch (error) {
      if (!isActiveAiProviderActionRequest(provider.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAiProviderActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function saveTenantAiSetting() {
    if (!params.tenantAiSetting) return;
    const payload = tenantAiSettingPayload();
    const signature = tenantAiSettingPayloadSignature(payload);
    const requestSeq = beginTenantAiSettingSaveRequest(signature);
    params.setBusy('保存 AI 配置');
    try {
      await api('/tenant-ai-settings', {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActiveTenantAiSettingSaveRequest(requestSeq, signature)) return;
      params.closeModal();
      params.showResult('AI 配置已保存', '运营默认模型、温度、Token 和回退策略已更新。');
      await refreshSystemSettingsAfterAction('AI 配置保存');
    } catch (error) {
      if (!isActiveTenantAiSettingSaveRequest(requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentTenantAiSettingSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function createPromptTemplate() {
    const templateId = null;
    const payload = promptTemplatePayload(templateId);
    const signature = promptTemplatePayloadSignature(templateId, payload);
    const requestSeq = beginPromptTemplateSaveRequest(templateId, signature);
    params.setBusy('新增提示词');
    try {
      const template = await api<PromptTemplate>('/prompt-templates', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult('提示词已新增', `已新增提示词模板：${template.name}`);
      params.setPromptTemplateForm({ ...params.promptTemplateForm, id: null, name: '运营群活跃模板', is_active: true });
      await refreshSystemSettingsAfterAction('提示词新增');
    } catch (error) {
      if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentPromptTemplateSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  function openPromptTemplateEdit(template: PromptTemplate) {
    params.setPromptTemplateForm({
      id: template.id,
      name: template.name,
      template_type: template.template_type,
      content: template.content,
      is_active: template.is_active,
    });
    params.setModal({ type: 'promptTemplateEdit' });
  }

  async function savePromptTemplate() {
    if (!params.promptTemplateForm.id) return createPromptTemplate();
    const templateId = params.promptTemplateForm.id;
    const payload = promptTemplatePayload(templateId);
    const signature = promptTemplatePayloadSignature(templateId, payload);
    const requestSeq = beginPromptTemplateSaveRequest(templateId, signature);
    params.setBusy('保存提示词');
    try {
      const template = await api<PromptTemplate>(`/prompt-templates/${templateId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;
      params.setPromptTemplates((current) => current.map((item) => item.id === template.id ? template : item));
      params.closeModal();
      params.showResult('提示词已保存', `已更新提示词模板：${template.name}`);
      await refreshSystemSettingsAfterAction('提示词保存');
    } catch (error) {
      if (!isActivePromptTemplateSaveRequest(templateId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentPromptTemplateSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  return {
    createDeveloperApp,
    openDeveloperAppEdit,
    toggleDeveloperApp,
    checkDeveloperApp,
    openTenantEdit,
    saveTenantQuota,
    saveTenantGroupRescueSettings,
    openAdminUserCreate,
    openAdminUserEdit,
    saveAdminUser,
    resetAdminUserPassword,
    adjustAdminUserTokens,
    loadUserTokenLedgers,
    createAiProvider,
    openAiProviderEdit,
    toggleAiProvider,
    checkAiProvider,
    saveTenantAiSetting,
    createPromptTemplate,
    openPromptTemplateEdit,
    savePromptTemplate,
  };
}
