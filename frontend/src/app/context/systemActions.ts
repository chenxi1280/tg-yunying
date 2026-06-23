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
  group_rescue_bot_username: string;
};

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
  async function createDeveloperApp() {
    const editing = params.developerAppForm.id !== null;
    params.setBusy(editing ? '保存开发者应用' : '新增开发者应用');
    try {
      const payload = {
        app_name: params.developerAppForm.app_name,
        api_id: Number(params.developerAppForm.api_id),
        api_hash: params.developerAppForm.api_hash || undefined,
        max_accounts: Number(params.developerAppForm.max_accounts),
        notes: params.developerAppForm.notes,
        is_active: params.developerAppForm.is_active,
      };
      const saved = editing
        ? await api<DeveloperApp>(`/developer-apps/${params.developerAppForm.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<DeveloperApp>('/developer-apps', { method: 'POST', body: JSON.stringify(payload) });
      params.closeModal();
      params.showResult(editing ? '开发者应用已保存' : '开发者应用已新增', `${saved.app_name} 当前状态：${saved.health_status}`);
      params.setDeveloperAppForm({ id: null, app_name: 'Telegram 开发者应用', api_id: '', api_hash: '', max_accounts: 0, notes: '', is_active: true });
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
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
    params.setBusy('保存租户配额');
    try {
      await api(`/tenants/${params.tenantForm.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: params.tenantForm.name,
          plan_name: params.tenantForm.plan_name,
          account_quota: params.tenantForm.account_quota,
          task_quota: params.tenantForm.task_quota,
        }),
      });
      params.closeModal();
      params.showResult('运营空间配置已更新', `${params.tenantForm.name} 的任务配额已保存。`);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function saveTenantGroupRescueSettings(tenantId: number, payload: GroupRescueSettingsPayload) {
    params.setBusy('保存群聊救援配置');
    try {
      await api(`/tenant-group-rescue-settings?tenant_id=${tenantId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      params.showResult('群聊救援配置已保存', payload.group_rescue_enabled ? '救援配置已启用，专职处置账号不会参与普通任务。' : '群聊救援已关闭。');
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
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
    params.setBusy('保存用户');
    try {
      const payload = {
        name: params.adminUserForm.name,
        role: params.adminUserForm.role,
        role_template: params.adminUserForm.role_template,
        subscription_status: params.adminUserForm.subscription_status,
        menu_permissions: params.adminUserForm.permissions,
        permissions: params.adminUserForm.permissions,
        is_active: params.adminUserForm.is_active,
        ...(!params.adminUserForm.id ? { password: params.adminUserForm.password } : {}),
      };
      const saved = await api<AdminUser>(params.adminUserForm.id ? `/admin/users/${params.adminUserForm.id}` : '/admin/users', {
        method: params.adminUserForm.id ? 'PATCH' : 'POST',
        body: JSON.stringify(payload),
      });
      params.setNotice(`用户已保存：${saved.name}`);
      params.closeModal();
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function resetAdminUserPassword(user: AdminUser, newPassword: string) {
    params.setBusy('重置密码');
    try {
      await api<AdminUser>(`/admin/users/${user.id}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ new_password: newPassword }),
      });
      params.setNotice(`${user.name} 的密码已重置。`);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function adjustAdminUserTokens(user: AdminUser) {
    params.setBusy('调整 Token');
    try {
      const saved = await api<AdminUser>(`/admin/users/${user.id}/token-adjustments`, {
        method: 'POST',
        body: JSON.stringify(params.tokenAdjustmentForm),
      });
      params.setNotice(`${saved.name} 当前 Token 余额：${saved.token_balance}`);
      await loadUserTokenLedgers(user.id);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function loadUserTokenLedgers(userId: number) {
    const ledgers = await api<TokenLedger[]>(`/admin/users/${userId}/token-ledgers`);
    params.setSelectedAdminUserId(userId);
    params.setSelectedUserTokenLedgers(ledgers);
  }

  async function toggleDeveloperApp(app: DeveloperApp) {
    params.setBusy(app.is_active ? '禁用开发者应用' : '启用开发者应用');
    try {
      const updated = await api<DeveloperApp>(`/developer-apps/${app.id}/${app.is_active ? 'disable' : 'enable'}`, { method: 'POST' });
      params.showResult('开发者应用状态已更新', `${updated.app_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function checkDeveloperApp(app: DeveloperApp) {
    params.setBusy('检查开发者应用');
    try {
      const checked = await api<DeveloperApp>(`/developer-apps/${app.id}/check`, { method: 'POST' });
      params.showResult('检查完成', `${checked.app_name}：${checked.health_status}`);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function createAiProvider() {
    const editing = params.aiProviderForm.id !== null;
    params.setBusy(editing ? '保存 AI 供应商' : '新增 AI 供应商');
    try {
      const payload = {
        ...params.aiProviderForm,
        provider_type: 'openai_compatible',
        api_key: params.aiProviderForm.api_key || undefined,
      };
      const saved = editing
        ? await api<AiProvider>(`/ai-providers/${params.aiProviderForm.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<AiProvider>('/ai-providers', { method: 'POST', body: JSON.stringify(payload) });
      params.closeModal();
      params.showResult(editing ? 'AI 供应商已保存' : 'AI 供应商已新增', `${saved.provider_name} 当前状态：${saved.health_status}`);
      params.setAiProviderForm({ id: null, provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com', model_name: 'deepseek-v4-flash', api_key: '', api_key_header: 'Authorization', notes: '', is_active: true });
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
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
    params.setBusy(provider.is_active ? '禁用 AI 供应商' : '启用 AI 供应商');
    try {
      const updated = await api<AiProvider>(`/ai-providers/${provider.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: !provider.is_active }),
      });
      params.showResult('AI 供应商状态已更新', `${updated.provider_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function checkAiProvider(provider: AiProvider) {
    params.setBusy('检查 AI 供应商');
    try {
      const checked = await api<AiProvider>(`/ai-providers/${provider.id}/check`, { method: 'POST' });
      const detailLabel = checked.health_status === '健康' ? '警告' : '错误';
      const errorSummary = checked.last_error ? `，${detailLabel}：${checked.last_error.slice(0, 220)}${checked.last_error.length > 220 ? '...' : ''}` : '';
      params.showResult('AI 供应商检查完成', `${checked.provider_name}：${checked.health_status}${errorSummary}`);
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function saveTenantAiSetting() {
    if (!params.tenantAiSetting) return;
    params.setBusy('保存 AI 配置');
    try {
      await api('/tenant-ai-settings', {
        method: 'PATCH',
        body: JSON.stringify({
          default_provider_id: params.selectedAiProviderId || null,
          ai_enabled: params.tenantAiSetting.ai_enabled,
          fallback_to_mock: params.tenantAiSetting.fallback_to_mock,
          temperature: params.tenantAiSetting.temperature,
          max_tokens: params.tenantAiSetting.max_tokens,
        }),
      });
      params.closeModal();
      params.showResult('AI 配置已保存', '运营默认模型、温度、Token 和回退策略已更新。');
      await params.refresh();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function createPromptTemplate() {
    params.setBusy('新增提示词');
    const { id: _id, ...payload } = params.promptTemplateForm;
    const template = await api<PromptTemplate>('/prompt-templates', {
      method: 'POST',
      body: JSON.stringify({ ...payload, tenant_id: params.currentUser?.tenant_id ?? 1 }),
    });
    params.closeModal();
    params.showResult('提示词已新增', `已新增提示词模板：${template.name}`);
    params.setPromptTemplateForm({ ...params.promptTemplateForm, id: null, name: '运营群活跃模板', is_active: true });
    await params.refresh();
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
    params.setBusy('保存提示词');
    const { id, ...payload } = params.promptTemplateForm;
    const template = await api<PromptTemplate>(`/prompt-templates/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    params.setPromptTemplates((current) => current.map((item) => item.id === template.id ? template : item));
    params.closeModal();
    params.showResult('提示词已保存', `已更新提示词模板：${template.name}`);
    await params.refresh();
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
