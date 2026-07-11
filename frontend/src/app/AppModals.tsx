import { useAppContext } from './context';
import { Button, Checkbox, Descriptions, Input, InputNumber, Modal, QRCode, Select, Space, Table, Typography } from 'antd';
import { useLocation } from 'react-router-dom';
import { FormActions, StatusBadge } from './components/shared';
import { AccountDetailModal, AccountPoolDetailModal } from './views/AccountModals';
import { formatBeijingDateTime } from './time';
import { hasPermission } from './utils';
import type { AiProvider } from './types/content';

const DEFAULT_AI_MAX_TOKENS_LIMIT = 100000;
const MINIMAX_AI_MAX_TOKENS_LIMIT = 250000;

const accountPhone = (account: { phone_number?: string | null; phone_masked: string }) => account.phone_number || account.phone_masked;

function tenantAiMaxTokensLimit(provider?: AiProvider | null) {
  if (!provider) return DEFAULT_AI_MAX_TOKENS_LIMIT;
  const text = `${provider.provider_name} ${provider.base_url} ${provider.model_name}`.toLowerCase();
  return text.includes('minimax') || text.includes('minimaxi') ? MINIMAX_AI_MAX_TOKENS_LIMIT : DEFAULT_AI_MAX_TOKENS_LIMIT;
}

function riskControlReturnSearch(search: string) {
  const source = new URLSearchParams(search);
  const target = new URLSearchParams();
  const mappings: [string, string][] = [
    ['risk_tab', 'tab'],
    ['risk_query', 'query'],
    ['risk_page', 'page'],
    ['risk_page_size', 'page_size'],
    ['risk_quick_filter', 'quick_filter'],
  ];
  mappings.forEach(([from, to]) => {
    const value = source.get(from);
    if (value) target.set(to, value);
  });
  const query = target.toString();
  return query ? `?${query}` : '';
}

const verificationTargetLabel = (task: { target_display?: string | null; target_peer_id?: string | null; group_id?: number | null }) => (
  task.target_display || task.target_peer_id || (task.group_id ? `群聊 #${task.group_id}` : '未识别目标')
);

const adminPermissionGroups = [
  { menu: ['overview.view', '运营中心'], buttons: [['operation_plans.manage', '运营方案管理'], ['operation_issues.manage', '运营异常处理']] },
  { menu: ['accounts.view', 'TG账号管理'], buttons: [
    ['accounts.create', '新增账号'],
    ['accounts.login', '账号登录'],
    ['accounts.sync', '账号同步'],
    ['accounts.codes.read', '查看验证码'],
    ['accounts.security.read', '账号安全查看'],
    ['accounts.security.batch', '账号安全批次'],
    ['accounts.security.credential_manage', '托管 2FA 密码'],
    ['accounts.security.session_manage', '备用 session 批次'],
    ['accounts.authorizations.manage', '备用授权管理'],
    ['accounts.profile.batch_update', '批量资料初始化'],
    ['accounts.sensitive.read', '敏感账号状态'],
    ['accounts.delete', '删除账号'],
    ['accounts.pool_manage', '账号池管理'],
    ['accounts.clone', '账号克隆'],
    ['accounts.manual_send', '手动私信'],
  ] },
  { menu: ['targets.view', '运营目标'], buttons: [['targets.manage', '目标管理']] },
  { menu: ['target_profile.view', '目标画像'], buttons: [['target_profile.manage', '目标画像管理']] },
  { menu: ['message_sending.view', '消息发送'], buttons: [['message_sending.manage', '发送管理']] },
  { menu: ['materials.view', '素材中心'], buttons: [['materials.upload', '素材上传'], ['materials.manage', '素材管理']] },
  { menu: ['account_masks.view', '账号面具'], buttons: [['ai_voice_profiles.manage', '账号面具管理'], ['account_environment.manage', '账号环境管理']] },
  { menu: ['tasks.view', '任务中心'], buttons: [['tasks.manage', '任务管理'], ['tasks.dispatch_control', '调度控制']] },
  { menu: ['listeners.view', '监听中心'], buttons: [['listeners.manage', '监听管理']] },
  { menu: ['rules.view', '规则中心'], buttons: [['rules.publish', '规则发布']] },
  { menu: ['risk.view', '风控中心'], buttons: [['risk.manage', '风控管理'], ['proxies.manage', '代理管理']] },
  { menu: ['archives.view', '归档中心'], buttons: [['archives.manage', '归档管理'], ['archives.export', '归档导出']] },
  { menu: ['usage.view', '运营数据'], buttons: [['usage.export', '运营数据导出']] },
  { menu: ['manual.view', '操作手册'], buttons: [] },
  { menu: ['system.view', '系统设置'], buttons: [
    ['system.manage', '系统管理'],
    ['ai.manage', 'AI配置'],
    ['prompt_templates.manage', '提示词管理'],
    ['developer_apps.manage', '开发者应用'],
    ['permissions.view', '账号权限'],
    ['permissions.manage', '权限管理'],
  ] },
  { menu: ['audits.view', '审计记录'], buttons: [['audits.view_sensitive', '敏感审计'], ['audit.export', '审计导出']] },
];

const flattenPermissions = (groups: typeof adminPermissionGroups) => groups.flatMap((group) => [group.menu, ...group.buttons].map(([value]) => value));
const allAdminPermissions = flattenPermissions(adminPermissionGroups);

function isZipMaterialFile(file: File) {
  return file.name.toLowerCase().endsWith('.zip') || file.type === 'application/zip' || file.type === 'application/x-zip-compressed';
}

function zipGroupName(fileName: string) {
  return fileName.replace(/\.[^.]+$/, '').trim() || '素材包';
}

/** 从 AppShell 中提取的所有模态框渲染组件。 */
export function AppModals() {
  const ctx = useAppContext();
  const location = useLocation();
  const returnToRiskControl = new URLSearchParams(location.search).get('return_to') === 'risk-control';
  const {
    modal, closeModal,
    changePasswordForm, setChangePasswordForm, changePassword,
    // Developer App
    developerAppForm, setDeveloperAppForm, createDeveloperApp,
    // AI Provider
    aiProviderForm, setAiProviderForm, createAiProvider,
    // Tenant
    tenantForm, setTenantForm, saveTenantQuota,
    adminUserForm, setAdminUserForm, saveAdminUser, tokenAdjustmentForm, setTokenAdjustmentForm, adminUsers, selectedUserTokenLedgers, adjustAdminUserTokens, resetAdminUserPassword,
    // Tenant AI
    tenantAiSetting, setTenantAiSetting, selectedAiProviderId, setSelectedAiProviderId, aiProviders, saveTenantAiSetting,
    // Prompt Template
    promptTemplateForm, setPromptTemplateForm, createPromptTemplate, savePromptTemplate,
    // Material
    materialForm, setMaterialForm, materialFile, setMaterialFile, createMaterial, saveMaterial, keywordRuleForm, setKeywordRuleForm, createContentKeywordRule, saveContentKeywordRule,
    // Account Pool
    accountPoolForm, setAccountPoolForm, createAccountPool, accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId, refreshAccountPoolDetail,
    // Account
    accountCreateForm, setAccountCreateForm, createAccount, loginAfterCreate, accountLoginForm, setAccountLoginForm, chooseAccountLoginMethod, submitAccountLoginCode, submitAccountLoginPassword, resendAccountLoginCode, checkAccountQrLogin, accountPools, accounts, accountDetail, setAccountDetailTab, accountDetailTab, runtime, cloneForm, setCloneForm, createClonePlan, confirmClonePlan, moveCurrentAccountPool,
    // Profile
    profileForm, setProfileForm, avatarFile, setAvatarFile, avatarUrl, saveAccountProfile, openAccountProfileEdit, queueAccountSyncNow, pollVerificationCodes, retryAccountProfileSync,
    // Group
    selectedGroup, groupPolicy, setGroupPolicy, saveGroupPolicy, groupDetail,
    // Verification
    returnAfterVerification, setReturnAfterVerification, confirmVerificationTask, loadVerificationChallengeContext, refreshVerificationChallengeContext,
    resolveGroupRestrictionTask, resolveGroupRestrictionBatch, submitVerificationTaskResponse, dismissVerificationTask,
    // Direct Message
    directMessageForm, setDirectMessageForm, selectedDirectContact, startDirectMessageToContact, createDirectMessageTask,
    // Misc
    openAccountCreate, openAccountDetail, openConfirm, accountContacts, accountName, groupName, busy, isActionPending, currentUser,
  } = ctx;
  const selectedAiProvider = aiProviders.find((provider) => provider.id === selectedAiProviderId);
  const accountCreateDisabled = !accountCreateForm.phone_number.trim();

  function submitAccountCreate() {
    if (!accountCreateDisabled) createAccount();
  }

  if (!modal) return null;
  const isManualVerificationTask = modal.type === 'verificationTaskDetail' && modal.payload.suggested_action === '人工处理';
  const isGroupRestrictionTask = modal.type === 'verificationTaskDetail' && modal.payload.issue_category === 'group_restriction';
  const verificationTaskActionable = modal.type === 'verificationTaskDetail' && ['待处理', '失败', '需人工处理'].includes(modal.payload.status);
  const canManageDeveloperApps = hasPermission(currentUser, 'developer_apps.manage');
  const developerAppSaveDisabled = !developerAppForm.app_name.trim()
    || !developerAppForm.api_id
    || !canManageDeveloperApps
    || (modal.type === 'developerAppCreate' && developerAppForm.api_hash.length < 8);
  const tenantSaveDisabled = !tenantForm.name
    || !tenantForm.plan_name;

  function handleMaterialFileChange(files: File[] | null) {
    setMaterialFile(files);
    if (files?.length === 1 && isZipMaterialFile(files[0])) {
      setMaterialForm((current) => ({ ...current, title: zipGroupName(files[0].name), material_type: current.material_type === '文件' ? '图片' : current.material_type }));
    }
  }

  return (
    <>
      {(modal?.type === 'developerAppCreate' || modal?.type === 'developerAppEdit') && (
        <Modal className="tg-modal medium" title={modal.type === 'developerAppEdit' ? '编辑开发者应用' : '新增开发者应用'} open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>应用名称<Input value={developerAppForm.app_name} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, app_name: event.target.value })} /></label>
            <label>API ID<Input value={developerAppForm.api_id} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_id: event.target.value })} /></label>
            <label>账号上限<InputNumber min={0} value={developerAppForm.max_accounts} onChange={(value) => setDeveloperAppForm({ ...developerAppForm, max_accounts: Number(value ?? 0) })} /></label>
            <label>备注<Input value={developerAppForm.notes} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, notes: event.target.value })} /></label>
            <label className="wide-field">API Hash<Input.Password value={developerAppForm.api_hash} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_hash: event.target.value })} placeholder={modal.type === 'developerAppEdit' ? '不填写则保留原凭证' : ''} /></label>
            <Checkbox checked={developerAppForm.is_active} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, is_active: event.target.checked })}>启用应用</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'developerAppEdit' ? '保存应用' : '新增应用'} onCancel={closeModal} onSubmit={createDeveloperApp} loading={isActionPending('developer-app:save')} disabled={developerAppSaveDisabled} />
          </div>
        </Modal>
      )}

      {(modal?.type === 'aiProviderCreate' || modal?.type === 'aiProviderEdit') && (
        <Modal className="tg-modal medium" title={modal.type === 'aiProviderEdit' ? '编辑 AI 供应商' : '新增 AI 供应商'} open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>名称<Input value={aiProviderForm.provider_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, provider_name: event.target.value })} /></label>
            <label>Base URL<Input value={aiProviderForm.base_url} onChange={(event) => setAiProviderForm({ ...aiProviderForm, base_url: event.target.value })} /></label>
            <label>模型名<Input value={aiProviderForm.model_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, model_name: event.target.value })} /></label>
            <label>Key Header<Input value={aiProviderForm.api_key_header} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key_header: event.target.value })} /></label>
            <label className="wide-field">API Key<Input.Password value={aiProviderForm.api_key} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key: event.target.value })} placeholder={modal.type === 'aiProviderEdit' ? '不填写则保留原 Key' : ''} /></label>
            <label className="wide-field">备注<Input value={aiProviderForm.notes} onChange={(event) => setAiProviderForm({ ...aiProviderForm, notes: event.target.value })} /></label>
            <Checkbox checked={aiProviderForm.is_active} onChange={(event) => setAiProviderForm({ ...aiProviderForm, is_active: event.target.checked })}>启用供应商</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'aiProviderEdit' ? '保存供应商' : '新增供应商'} onCancel={closeModal} onSubmit={createAiProvider} loading={isActionPending('ai-provider:save')} disabled={!aiProviderForm.provider_name.trim() || !aiProviderForm.base_url.trim() || !aiProviderForm.model_name.trim() || (modal.type === 'aiProviderCreate' && aiProviderForm.api_key.length < 4)} />
          </div>
        </Modal>
      )}

      {modal?.type === 'tenantEdit' && (
        <Modal className="tg-modal medium" title="编辑运营空间配置" open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>运营空间名称<Input value={tenantForm.name} onChange={(event) => setTenantForm({ ...tenantForm, name: event.target.value })} /></label>
            <label>运行口径<Input value={tenantForm.plan_name} onChange={(event) => setTenantForm({ ...tenantForm, plan_name: event.target.value })} /></label>
            <label>账号上限<Input value="不限" disabled /></label>
            <label>任务配额<InputNumber min={0} value={tenantForm.task_quota} onChange={(value) => setTenantForm({ ...tenantForm, task_quota: Number(value ?? 0) })} /></label>
          </div>
          <FormActions submitLabel="保存配置" onCancel={closeModal} onSubmit={saveTenantQuota} loading={isActionPending(`tenant:${tenantForm.id ?? 'current'}:save`)} disabled={tenantSaveDisabled} />
          </div>
        </Modal>
      )}

      {modal?.type === 'adminUserEdit' && (
        <Modal className="tg-modal large" title="用户管理" open width={920} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>用户名称<Input value={adminUserForm.name} onChange={(event) => setAdminUserForm((current) => ({ ...current, name: event.target.value }))} /></label>
            {!adminUserForm.id && (
              <label>登录密码<Input.Password value={adminUserForm.password} onChange={(event) => setAdminUserForm((current) => ({ ...current, password: event.target.value }))} /></label>
            )}
            <label>账号类型<Select value={adminUserForm.role} onChange={(value) => setAdminUserForm((current) => ({ ...current, role: value }))} options={['后台用户', '系统管理员'].map((value) => ({ value, label: value }))} /></label>
            <label>角色模板<Select value={adminUserForm.role_template} onChange={(value) => {
              const templatePermissions: Record<string, string[]> = {
                '运营管理员': ['overview.view', 'operation_plans.manage', 'operation_issues.manage', 'accounts.view', 'accounts.sync', 'accounts.codes.read', 'accounts.security.read', 'accounts.security.batch', 'accounts.profile.batch_update', 'targets.view', 'targets.manage', 'target_profile.view', 'target_profile.manage', 'message_sending.view', 'message_sending.manage', 'materials.view', 'materials.upload', 'materials.manage', 'account_masks.view', 'ai_voice_profiles.manage', 'account_environment.manage', 'tasks.view', 'tasks.manage', 'listeners.view', 'listeners.manage', 'rules.view', 'rules.publish', 'risk.view', 'risk.manage', 'proxies.manage', 'archives.view', 'archives.manage', 'usage.view', 'usage.export', 'system.view', 'manual.view', 'audits.view', 'audit.export'],
                '账号添加专员': ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'],
                '只读观察员': ['overview.view', 'targets.view', 'target_profile.view', 'listeners.view', 'usage.view', 'manual.view', 'audits.view'],
              };
              setAdminUserForm((current) => ({ ...current, role_template: value, permissions: templatePermissions[value] ?? current.permissions, menu_permissions: templatePermissions[value] ?? current.menu_permissions }));
            }} options={['运营管理员', '账号添加专员', '只读观察员'].map((value) => ({ value, label: value }))} /></label>
            <label>账号状态<Select value={adminUserForm.subscription_status} onChange={(value) => setAdminUserForm((current) => ({ ...current, subscription_status: value }))} options={[{ value: 'pending_activation', label: '待启用' }, { value: 'active', label: '已启用' }, { value: 'expired', label: '已停用' }]} /></label>
            <Checkbox checked={adminUserForm.is_active} onChange={(event) => setAdminUserForm((current) => ({ ...current, is_active: event.target.checked }))}>允许登录</Checkbox>
            <div className="wide-field">
              <span className="field-label">菜单与按钮权限</span>
              <div className="choice-grid">
                {adminPermissionGroups.map((group) => {
                  const [menuValue, menuLabel] = group.menu;
                  const menuChecked = adminUserForm.permissions.includes(menuValue) || adminUserForm.permissions.includes('*');
                  return (
                    <div className="permission-group" key={menuValue}>
                      <Checkbox
                        checked={menuChecked}
                        onChange={(event) => {
                          const groupValues = [menuValue, ...group.buttons.map(([value]) => value)];
                          const next = event.target.checked
                            ? [...adminUserForm.permissions, menuValue]
                            : adminUserForm.permissions.filter((item) => !groupValues.includes(item));
                          const permissions = Array.from(new Set(next)).filter((item) => item === '*' || allAdminPermissions.includes(item));
                          setAdminUserForm((current) => ({ ...current, permissions, menu_permissions: permissions }));
                        }}
                      >
                        {menuLabel}
                      </Checkbox>
                      {menuChecked && group.buttons.length > 0 && (
                        <div className="permission-buttons">
                          {group.buttons.map(([value, label]) => (
                            <Checkbox
                              key={value}
                              checked={adminUserForm.permissions.includes(value) || adminUserForm.permissions.includes('*')}
                              onChange={(event) => {
                                const next = event.target.checked
                                  ? [...adminUserForm.permissions, menuValue, value]
                                  : adminUserForm.permissions.filter((item) => item !== value);
                                const permissions = Array.from(new Set(next)).filter((item) => item === '*' || allAdminPermissions.includes(item));
                                setAdminUserForm((current) => ({ ...current, permissions, menu_permissions: permissions }));
                              }}
                            >
                              {label}
                            </Checkbox>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
            <label>Token 调整<InputNumber value={tokenAdjustmentForm.delta_tokens} onChange={(value) => setTokenAdjustmentForm((current) => ({ ...current, delta_tokens: Number(value ?? 0) }))} /></label>
            <label>调整原因<Input value={tokenAdjustmentForm.reason} onChange={(event) => setTokenAdjustmentForm((current) => ({ ...current, reason: event.target.value }))} /></label>
          </div>
          <div className="mini-list">
            {selectedUserTokenLedgers.slice(0, 5).map((ledger) => (
              <p key={ledger.id} className="muted-line">{ledger.created_at.replace('T', ' ').slice(0, 16)} / {ledger.change_type} / {ledger.delta_tokens} / 余额 {ledger.balance_after}</p>
            ))}
          </div>
          <Space className="modal-actions">
            <Button onClick={closeModal}>取消</Button>
            <Button disabled={!adminUserForm.id} loading={adminUserForm.id ? isActionPending(`admin-user:${adminUserForm.id}:reset-password`) : false} onClick={() => {
              const user = adminUsers.find((item) => item.id === adminUserForm.id);
              if (user) void resetAdminUserPassword(user, 'user123456');
            }}>重置密码</Button>
            <Button disabled={!adminUserForm.id} loading={adminUserForm.id ? isActionPending(`admin-user:${adminUserForm.id}:adjust-tokens`) : false} onClick={() => {
              const user = adminUsers.find((item) => item.id === adminUserForm.id);
              if (user) void adjustAdminUserTokens(user);
            }}>调整 AI 用量</Button>
            <Button type="primary" loading={isActionPending(`admin-user:${adminUserForm.id ?? 'current'}:save`)} onClick={saveAdminUser} disabled={!adminUserForm.name.trim() || (!adminUserForm.id && adminUserForm.password.length < 6)}>保存用户</Button>
          </Space>
          </div>
        </Modal>
      )}

      {modal?.type === 'tenantAiEdit' && tenantAiSetting && (
        <Modal className="tg-modal medium" title="编辑运营空间 AI 配置" open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>默认模型<Select<number | ''> value={selectedAiProviderId || ''} disabled={!aiProviders.length} onChange={(value) => setSelectedAiProviderId(Number(value) || '')} options={aiProviders.length ? aiProviders.map((provider) => ({ value: provider.id, label: `${provider.provider_name} / ${provider.model_name}` })) : [{ value: '', label: '请先新增 AI 供应商' }]} /></label>
            <label>温度<InputNumber min={0} max={2} step={0.1} value={tenantAiSetting.temperature} onChange={(value) => setTenantAiSetting({ ...tenantAiSetting, temperature: Number(value ?? 0) })} /></label>
            <label>最大 Token<InputNumber min={128} max={tenantAiMaxTokensLimit(selectedAiProvider)} value={tenantAiSetting.max_tokens} onChange={(value) => setTenantAiSetting({ ...tenantAiSetting, max_tokens: Number(value ?? 128) })} /></label>
            <Checkbox checked={tenantAiSetting.ai_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_enabled: event.target.checked })}>启用 AI 内容生成</Checkbox>
            <Checkbox checked={tenantAiSetting.fallback_to_mock} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, fallback_to_mock: event.target.checked })}>失败回退模板</Checkbox>
            <Checkbox checked={tenantAiSetting.ai_group_model_fallback_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_group_model_fallback_enabled: event.target.checked })}>AI 活群启用 M2.5 回退</Checkbox>
            <Checkbox checked={tenantAiSetting.ai_group_grok_fallback_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_group_grok_fallback_enabled: event.target.checked })}>AI 活群启用 Grok 回退</Checkbox>
            <Checkbox checked={tenantAiSetting.ai_group_static_fallback_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_group_static_fallback_enabled: event.target.checked })}>AI 活群启用签到/表情兜底</Checkbox>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveTenantAiSetting} loading={isActionPending('tenant-ai:save')} disabled={!aiProviders.length} />
          </div>
        </Modal>
      )}

      {modal?.type === 'changePassword' && (
        <Modal className="tg-modal small" title="修改登录密码" open width={480} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label className="wide-field">当前密码<Input.Password value={changePasswordForm.current_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, current_password: event.target.value }))} /></label>
            <label className="wide-field">新密码<Input.Password value={changePasswordForm.new_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, new_password: event.target.value }))} /></label>
            <label className="wide-field">确认新密码<Input.Password value={changePasswordForm.confirm_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, confirm_password: event.target.value }))} /></label>
          </div>
          <FormActions submitLabel="修改密码" onCancel={closeModal} onSubmit={changePassword} loading={isActionPending('modal:password:change')} disabled={!changePasswordForm.current_password || changePasswordForm.new_password.length < 6 || changePasswordForm.new_password !== changePasswordForm.confirm_password} />
          </div>
        </Modal>
      )}

      {(modal?.type === 'promptTemplateCreate' || modal?.type === 'promptTemplateEdit') && (
        <Modal className="tg-modal large" title={modal.type === 'promptTemplateEdit' ? '编辑提示词模板' : '新增提示词模板'} open width={920} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>模板名称<Input value={promptTemplateForm.name} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, name: event.target.value })} /></label>
            <label>模板类型<Select value={promptTemplateForm.template_type} onChange={(value) => setPromptTemplateForm({ ...promptTemplateForm, template_type: value })} options={['系统决策提示词', '群活跃对话计划', '多账号对话脚本', 'AI黑话词表', '素材配文', '风险检查'].map((value) => ({ value, label: value }))} /></label>
            <label className="wide-field">模板内容<Input.TextArea value={promptTemplateForm.content} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, content: event.target.value })} /></label>
            <Checkbox checked={promptTemplateForm.is_active} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, is_active: event.target.checked })}>启用提示词</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'promptTemplateEdit' ? '保存提示词' : '新增提示词'} onCancel={closeModal} onSubmit={modal.type === 'promptTemplateEdit' ? savePromptTemplate : createPromptTemplate} loading={isActionPending(modal.type === 'promptTemplateEdit' ? `prompt-template:${promptTemplateForm.id ?? 'create'}:save` : 'prompt-template:create')} disabled={!promptTemplateForm.name || !promptTemplateForm.content} />
          </div>
        </Modal>
      )}

      {(modal?.type === 'materialCreate' || modal?.type === 'materialEdit') && (
        <Modal className="tg-modal medium" title={modal.type === 'materialEdit' ? '编辑素材' : '新增素材'} open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>素材标题 / ZIP 分组名<Input value={materialForm.title} onChange={(event) => setMaterialForm({ ...materialForm, title: event.target.value })} /></label>
            <label>素材类型 / ZIP 导入类型<Select value={materialForm.material_type} onChange={(value) => setMaterialForm({ ...materialForm, material_type: value })} options={['文本', '图片', '表情包', '头像包', '文件', '链接', '组合消息'].map((value) => ({ value, label: value }))} /></label>
            <label>入库方式<Select value={materialForm.source_kind} disabled={modal.type === 'materialEdit'} onChange={(value) => setMaterialForm({ ...materialForm, source_kind: value, content: value === 'upload' ? '' : materialForm.content })} options={[
              { value: 'url', label: 'URL 入库' },
              { value: 'upload', label: '上传文件' },
            ]} /></label>
            {materialForm.material_type === '表情包' && <label>表情包类型<Select value={materialForm.emoji_asset_kind} onChange={(value) => setMaterialForm({ ...materialForm, emoji_asset_kind: value })} options={[
              { value: 'image_meme', label: '图片伪表情包' },
              { value: 'static_sticker', label: '静态 sticker' },
              { value: 'animated_sticker', label: 'animated sticker' },
              { value: 'video_sticker', label: 'video sticker' },
              { value: 'custom_emoji', label: 'custom emoji' },
            ]} /></label>}
            <label>标签<Input value={materialForm.tags} onChange={(event) => setMaterialForm({ ...materialForm, tags: event.target.value })} /></label>
            {materialForm.source_kind === 'upload' && modal.type === 'materialCreate' ? (
              <>
                <label className="wide-field">素材文件 / ZIP 包<input type="file" multiple accept=".jpg,.jpeg,.png,.webp,.gif,.tgs,.webm,.mp4,.pdf,.zip,image/*,application/zip" onChange={(event) => handleMaterialFileChange(event.target.files ? Array.from(event.target.files) : null)} /></label>
                {materialFile?.length ? <span className="wide-field">已选择 {materialFile.length} 个文件；单个 ZIP 会按包内图片导入并返回逐文件结果。</span> : null}
                <span className="wide-field">ZIP 只作为批量导入容器；包内仅导入 PNG/JPG/JPEG，非图片、隐藏目录、重复或超限文件会在导入结果中展示原因。</span>
                <label className="wide-field">Caption<Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm({ ...materialForm, content: event.target.value })} /></label>
              </>
            ) : (
              <label className="wide-field">内容/URL<Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm({ ...materialForm, content: event.target.value })} /></label>
            )}
          </div>
          <FormActions submitLabel={modal.type === 'materialEdit' ? '保存素材' : '新增素材'} onCancel={closeModal} onSubmit={modal.type === 'materialEdit' ? saveMaterial : createMaterial} loading={isActionPending(modal.type === 'materialEdit' ? `material:${materialForm.id ?? 'create'}:save` : 'material:create')} disabled={!materialForm.title || (materialForm.source_kind === 'upload' && modal.type === 'materialCreate' ? !(materialFile?.length) : !materialForm.content)} />
          </div>
        </Modal>
      )}

      {modal?.type === 'materialImportResult' && (
        <Modal className="tg-modal large" title="ZIP 导入结果" open width={820} onCancel={closeModal} footer={<Button type="primary" onClick={closeModal}>关闭</Button>} destroyOnHidden centered>
          <div className="modal-body">
            <Descriptions
              size="small"
              column={3}
              bordered
              items={[
                { key: 'file', label: '压缩包', children: modal.payload.source_filename },
                { key: 'group', label: '素材包', children: modal.payload.target_group_name || '-' },
                { key: 'status', label: '状态', children: modal.payload.status },
                { key: 'total', label: '总数', children: modal.payload.total_count },
                { key: 'success', label: '成功', children: modal.payload.success_count },
                { key: 'skipped', label: '跳过', children: modal.payload.skipped_count },
                { key: 'failed', label: '失败', children: modal.payload.failed_count },
                { key: 'duplicate', label: '重复', children: modal.payload.duplicate_count },
                { key: 'oversize', label: '超限', children: modal.payload.oversize_count },
              ]}
            />
            <Table
              rowKey={(item) => `${item.file_name}:${item.material_id ?? item.reason}`}
              size="small"
              pagination={{ pageSize: 8, hideOnSinglePage: true }}
              dataSource={modal.payload.items}
              columns={[
                { title: '文件', dataIndex: 'file_name' },
                { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <StatusBadge status={value} label={value === 'created' ? '已导入' : value === 'skipped' ? '已跳过' : '失败'} /> },
                { title: '素材ID', dataIndex: 'material_id', width: 100, render: (value: number | null) => value ?? '-' },
                { title: '大小', dataIndex: 'file_size', width: 100, render: (value: number) => `${value} B` },
                { title: '原因', dataIndex: 'reason', render: (value: string) => <Typography.Text type={value ? 'secondary' : undefined}>{value || '-'}</Typography.Text> },
              ]}
            />
          </div>
        </Modal>
      )}

      {(modal?.type === 'keywordRuleCreate' || modal?.type === 'keywordRuleEdit') && (
        <Modal className="tg-modal medium" title={modal.type === 'keywordRuleEdit' ? '编辑关键词' : '新增关键词'} open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>关键词<Input value={keywordRuleForm.keyword} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, keyword: event.target.value })} /></label>
            <label>匹配方式<Select value={keywordRuleForm.match_type} onChange={(value) => setKeywordRuleForm({ ...keywordRuleForm, match_type: value })} options={[{ value: 'contains', label: '包含' }]} /></label>
            <label className="wide-field">备注<Input value={keywordRuleForm.note} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, note: event.target.value })} /></label>
            <Checkbox checked={keywordRuleForm.is_active} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, is_active: event.target.checked })}>启用关键词</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'keywordRuleEdit' ? '保存关键词' : '新增关键词'} onCancel={closeModal} onSubmit={modal.type === 'keywordRuleEdit' ? saveContentKeywordRule : createContentKeywordRule} loading={isActionPending(modal.type === 'keywordRuleEdit' ? `keyword-rule:${keywordRuleForm.id ?? 'create'}:save` : 'keyword-rule:create')} disabled={!keywordRuleForm.keyword.trim()} />
          </div>
        </Modal>
      )}

      {modal?.type === 'accountPoolDetail' && accountPoolDetail && (
        <AccountPoolDetailModal accountPoolDetail={accountPoolDetail} poolDirectAccountId={poolDirectAccountId} setPoolDirectAccountId={setPoolDirectAccountId} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} onClose={closeModal} onOpenAccountCreate={openAccountCreate} onOpenAccountDetail={openAccountDetail} onRefreshAccountPoolDetail={refreshAccountPoolDetail} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} accountName={accountName} isActionPending={isActionPending} canCreateAccount={hasPermission(currentUser, 'accounts.create')} canManualSend={hasPermission(currentUser, 'accounts.manual_send')} canSecurityRead={hasPermission(currentUser, 'accounts.security.read')} />
      )}

      {modal?.type === 'accountPoolCreate' && (
        <Modal className="tg-modal medium" title="新增账号分组" open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>账号分组名称<Input value={accountPoolForm.name} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, name: event.target.value })} /></label>
            <Checkbox checked={accountPoolForm.is_default} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, is_default: event.target.checked })}>设为默认账号分组</Checkbox>
            <label className="wide-field">说明<Input.TextArea value={accountPoolForm.description} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, description: event.target.value })} /></label>
          </div>
          <FormActions submitLabel="新增账号分组" onCancel={closeModal} onSubmit={createAccountPool} loading={isActionPending('modal:account-pool:create')} disabled={!accountPoolForm.name.trim()} />
          </div>
        </Modal>
      )}

      {modal?.type === 'accountCreate' && (
        <Modal className="tg-modal medium" title="新增账号" open width={640} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>所属账号分组<Select value={accountCreateForm.pool_id || ''} onChange={(value) => setAccountCreateForm({ ...accountCreateForm, pool_id: Number(value) || '' })} options={[{ value: '', label: '默认账号分组' }, ...accountPools.map((pool) => ({ value: pool.id, label: pool.name }))]} /></label>
            <label>登录方式<Select value={accountCreateForm.login_method} onChange={(value) => setAccountCreateForm({ ...accountCreateForm, login_method: value as 'code' | 'qr' })} options={[{ value: 'code', label: '手机号验证码' }, { value: 'qr', label: '二维码扫码' }]} /></label>
            <label>平台备注名<Input value={accountCreateForm.display_name} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, display_name: event.target.value })} placeholder="留空自动按导入时间和手机号尾号命名" /></label>
            <label>TG 用户名<Input value={accountCreateForm.username} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, username: event.target.value })} placeholder="可选，不含 @" /></label>
            <label className="wide-field">手机号<Input value={accountCreateForm.phone_number} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, phone_number: event.target.value })} onPressEnter={submitAccountCreate} placeholder="+8613800000000" /></label>
          </div>
          <p className="muted-line">创建后会进入所选登录方式；验证码和扫码是同级二选一流程。</p>
          <FormActions submitLabel="创建账号" onCancel={closeModal} onSubmit={createAccount} loading={isActionPending('modal:account:create')} disabled={accountCreateDisabled} />
          </div>
        </Modal>
      )}

      {modal?.type === 'accountLogin' && accountLoginForm.account && (
        <Modal className="tg-modal small" title={`${accountLoginForm.account.display_name} 完成登录`} open width={480} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="detail-list">
            <div><dt>账号</dt><dd>{accountPhone(accountLoginForm.account)}</dd></div>
            <div><dt>当前状态</dt><dd><StatusBadge status={accountLoginForm.account.status} /></dd></div>
            <div><dt>登录方式</dt><dd>{accountLoginForm.step === 'method' ? '待选择' : accountLoginForm.method === 'qr' ? '二维码扫码' : '手机号验证码'}</dd></div>
            {accountLoginForm.flow?.code_expires_at && <div><dt>验证码有效期</dt><dd>{formatBeijingDateTime(accountLoginForm.flow.code_expires_at)}</dd></div>}
          </div>
          {accountLoginForm.step === 'method' && (
            <>
              <p className="muted-line">请选择登录方式。扫码和验证码是同级二选一流程，选择后才会启动对应登录。</p>
              <Space direction="vertical" style={{ width: '100%' }}>
                <Button type="primary" block loading={isActionPending(`account-login:${accountLoginForm.account.id}:qr`)} onClick={() => chooseAccountLoginMethod('qr')} disabled={Boolean(busy)}>扫码登录</Button>
                <Button block loading={isActionPending(`account-login:${accountLoginForm.account.id}:code`)} onClick={() => chooseAccountLoginMethod('code')} disabled={Boolean(busy)}>验证码登录</Button>
              </Space>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
            </>
          )}
          {accountLoginForm.step === 'qr' && (
            <>
              {accountLoginForm.flow?.qr_payload && (
                <div style={{ display: 'flex', justifyContent: 'center', margin: '16px 0' }}>
                  <QRCode value={accountLoginForm.flow.qr_payload} />
                </div>
              )}
              <div className="policy-grid">
                <label className="wide-field">扫码 payload
                  <Input.TextArea value={accountLoginForm.flow?.qr_payload ?? ''} readOnly autoSize={{ minRows: 4, maxRows: 8 }} placeholder="二维码 payload 将在启动扫码登录后展示" />
                </label>
              </div>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={() => setAccountLoginForm((current) => ({ ...current, step: 'method', error: '' }))} disabled={Boolean(busy)}>重新选择登录方式</Button>
                <Button type="primary" loading={isActionPending(`account-login:${accountLoginForm.account.id}:qr-check`)} onClick={checkAccountQrLogin} disabled={Boolean(busy)}>检查扫码结果</Button>
              </Space>
            </>
          )}
          {accountLoginForm.step === 'code' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">验证码
                  <Input
                    value={accountLoginForm.code}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, code: event.target.value, error: '' }))}
                    onPressEnter={submitAccountLoginCode}
                    placeholder="输入 Telegram 收到的验证码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.flow?.code_preview && <p className="muted-line">开发模式验证码：{accountLoginForm.flow.code_preview}</p>}
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={() => setAccountLoginForm((current) => ({ ...current, step: 'method', error: '' }))} disabled={Boolean(busy)}>重新选择登录方式</Button>
                <Button loading={isActionPending(`account-login:${accountLoginForm.account.id}:resend`)} onClick={resendAccountLoginCode} disabled={Boolean(busy)}>{accountLoginForm.flow ? '重新发送验证码' : '获取验证码'}</Button>
                <Button type="primary" loading={isActionPending(`account-login:${accountLoginForm.account.id}:code`)} onClick={submitAccountLoginCode} disabled={Boolean(busy) || !accountLoginForm.code.trim()}>提交验证码</Button>
              </Space>
            </>
          )}
          {accountLoginForm.step === 'password' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">二步验证密码
                  <Input.Password
                    value={accountLoginForm.password_2fa}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, password_2fa: event.target.value, error: '' }))}
                    onPressEnter={submitAccountLoginPassword}
                    placeholder="输入 Telegram 2FA 密码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={() => setAccountLoginForm((current) => ({ ...current, step: 'method', password_2fa: '', error: '' }))}>重新选择登录方式</Button>
                <Button type="primary" loading={isActionPending(`account-login:${accountLoginForm.account.id}:password`)} onClick={submitAccountLoginPassword} disabled={Boolean(busy) || !accountLoginForm.password_2fa}>完成登录</Button>
              </Space>
            </>
          )}
          </div>
        </Modal>
      )}

      {modal?.type === 'accountMovePool' && accountDetail && (
        <Modal className="tg-modal small" title="移动账号分组" open width={480} onCancel={() => ctx.setModal({ type: 'accountDetail' })} footer={null} destroyOnHidden centered>
          <div className="modal-body">
          <div className="policy-grid">
            <label>目标账号分组<Select value={accountDetail.account.pool_id ?? ''} onChange={(value) => moveCurrentAccountPool(Number(value))} options={accountPools.map((pool) => ({ value: pool.id, label: pool.name }))} /></label>
          </div>
          <Space className="modal-actions"><Button onClick={() => ctx.setModal({ type: 'accountDetail' })}>返回</Button></Space>
          </div>
        </Modal>
      )}

      {modal?.type === 'accountCloneCreate' && accountDetail && (
        <Modal className="tg-modal medium" title="创建账号克隆计划" open width={640} onCancel={() => ctx.setModal({ type: 'accountDetail' })} footer={null} destroyOnHidden centered>
          <div className="modal-body">
          <div className="target-account-grid">
            {accounts.filter((account) => account.id !== accountDetail.account.id).map((account) => {
              const selected = cloneForm.target_account_ids.includes(account.id);
              return (
                <Button key={account.id} className={selected ? 'selected contact-pick' : 'contact-pick'} onClick={() => setCloneForm({ ...cloneForm, target_account_ids: selected ? cloneForm.target_account_ids.filter((id) => id !== account.id) : [...cloneForm.target_account_ids, account.id] })}>
                  <strong>{account.display_name}</strong><span>{account.pool_name}</span><StatusBadge status={account.status} />
                </Button>
              );
            })}
          </div>
          <div className="policy-grid">
            <Checkbox checked={cloneForm.clone_contacts} onChange={(event) => setCloneForm({ ...cloneForm, clone_contacts: event.target.checked })}>克隆好友和私聊对象</Checkbox>
            <Checkbox checked={cloneForm.clone_groups} onChange={(event) => setCloneForm({ ...cloneForm, clone_groups: event.target.checked })}>克隆群聊和频道清单</Checkbox>
          </div>
          <p className="muted-line">已选择 {cloneForm.target_account_ids.length} 个目标账号。系统会先生成计划，确认后逐项执行。</p>
          <FormActions submitLabel="生成克隆计划" onCancel={() => ctx.setModal({ type: 'accountDetail' })} onSubmit={createClonePlan} loading={isActionPending(`account:${accountDetail.account.id}:clone-plan:create`)} disabled={!cloneForm.target_account_ids.length || (!cloneForm.clone_contacts && !cloneForm.clone_groups)} />
          </div>
        </Modal>
      )}

      {modal?.type === 'verificationTaskDetail' && (
        <Modal className="tg-modal medium" title="验证辅助处理" open width={640} onCancel={() => ctx.setModal({ type: returnAfterVerification })} footer={null} destroyOnHidden centered>
          <div className="modal-body">
          <div className="detail-list">
            <div><dt>状态</dt><dd><StatusBadge status={modal.payload.status} /></dd></div>
            <div><dt>验证类型</dt><dd>{modal.payload.verification_type}</dd></div>
            <div><dt>建议操作</dt><dd>{modal.payload.suggested_action}</dd></div>
            <div><dt>目标</dt><dd>{verificationTargetLabel(modal.payload)}</dd></div>
          </div>
          <p className="dialog-message">{modal.payload.detected_reason || '平台检测到当前账号在该群可能需要完成验证后才能发言。'}</p>
          <p className="muted-line">{isGroupRestrictionTask ? '这类问题需要群管理员先在 Telegram 群内解除限制；平台重查目标能力，通过后才恢复可发。' : isManualVerificationTask ? '确认前请先在 Telegram 内完成人工处理；平台不会自动放行群权限。' : '平台只会在你确认后执行可控动作。'}</p>
          <Space className="modal-actions">
            <Button onClick={() => ctx.setModal({ type: returnAfterVerification })}>返回</Button>
            <Button loading={isActionPending(`verification:${modal.payload.id}:dismiss`)} onClick={() => dismissVerificationTask(modal.payload)}>忽略</Button>
            <Button
              type="primary"
              loading={isActionPending(isGroupRestrictionTask ? `verification:${modal.payload.id}:resolve-group` : `verification:${modal.payload.id}:confirm`)}
              disabled={!verificationTaskActionable}
              onClick={() => (isGroupRestrictionTask ? resolveGroupRestrictionTask(modal.payload) : confirmVerificationTask(modal.payload))}
            >
              {isGroupRestrictionTask ? '已解除，重新检查' : isManualVerificationTask ? '标记已人工处理' : '确认处理'}
            </Button>
          </Space>
          </div>
        </Modal>
      )}

      {modal?.type === 'groupPolicyEdit' && selectedGroup && (
        <Modal className="tg-modal large" title="编辑群运营配置" open width={920} onCancel={closeModal} footer={null} destroyOnHidden centered>
      <div className="modal-body">
          <div className="policy-grid">
            <label>活跃时间<Input value={groupPolicy.active_window} onChange={(event) => setGroupPolicy({ ...groupPolicy, active_window: event.target.value })} /></label>
            <label>每日上限<InputNumber value={groupPolicy.daily_limit} onChange={(value) => setGroupPolicy({ ...groupPolicy, daily_limit: Number(value ?? 0) })} /></label>
            <label>账号冷却秒<InputNumber value={groupPolicy.account_cooldown_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, account_cooldown_seconds: Number(value ?? 0) })} /></label>
            <label>群冷却秒<InputNumber value={groupPolicy.group_cooldown_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, group_cooldown_seconds: Number(value ?? 0) })} /></label>
            <label>话题方向<Input.TextArea value={groupPolicy.topic_direction} onChange={(event) => setGroupPolicy({ ...groupPolicy, topic_direction: event.target.value })} /></label>
            <label>禁用词<Input.TextArea value={groupPolicy.banned_words} onChange={(event) => setGroupPolicy({ ...groupPolicy, banned_words: event.target.value })} /></label>
            <label>链接白名单<Input.TextArea value={groupPolicy.link_whitelist} onChange={(event) => setGroupPolicy({ ...groupPolicy, link_whitelist: event.target.value })} /></label>
            <Checkbox checked={!groupPolicy.require_review} onChange={(event) => setGroupPolicy({ ...groupPolicy, require_review: !event.target.checked })}>规则内自动发送</Checkbox>
            <Checkbox checked={groupPolicy.listener_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_enabled: event.target.checked })}>启用监听续聊</Checkbox>
            <Checkbox checked={groupPolicy.listener_auto_reply_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_auto_reply_enabled: event.target.checked })}>监听触发后自动发送</Checkbox>
            <label>监听间隔秒<InputNumber min={30} value={groupPolicy.listener_interval_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, listener_interval_seconds: Number(value ?? 30) })} /></label>
            <label>上下文条数<InputNumber min={1} max={100} value={groupPolicy.listener_context_limit} onChange={(value) => setGroupPolicy({ ...groupPolicy, listener_context_limit: Number(value ?? 1) })} /></label>
            <div className="wide-field">
              <span className="field-label">监听号</span>
              <div className="choice-grid">
                {(groupDetail?.group.id === selectedGroup.id ? groupDetail.accounts : accounts).map((account) => (
                  <Checkbox
                    key={account.id}
                      checked={groupPolicy.listener_account_ids.includes(account.id)}
                      onChange={(event) => {
                        const nextIds = event.target.checked
                          ? [...groupPolicy.listener_account_ids, account.id]
                          : groupPolicy.listener_account_ids.filter((id) => id !== account.id);
                        setGroupPolicy({ ...groupPolicy, listener_account_ids: Array.from(new Set(nextIds)) });
                      }}
                    >
                    {account.display_name}{account.username ? ` / @${account.username}` : ''} / {account.status}
                  </Checkbox>
                ))}
              </div>
            </div>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveGroupPolicy} loading={isActionPending(`group:${selectedGroup.id}:policy:save`)} />
          </div>
        </Modal>
      )}

      {modal?.type === 'accountProfileEdit' && accountDetail && (
        <Modal className="tg-modal medium" title="编辑账号资料" open width={640} onCancel={() => ctx.setModal({ type: 'accountDetail' })} footer={null} destroyOnHidden centered>
          <div className="modal-body">
          <div className="profile-edit-layout">
            <div className="avatar-preview">
              {avatarFile ? <img src={URL.createObjectURL(avatarFile)} alt="" /> : profileForm.avatar_object_key ? <img src={avatarUrl(`/media/${profileForm.avatar_object_key}`)} alt="" /> : <span>{profileForm.display_name.slice(0, 1) || 'T'}</span>}
            </div>
            <div className="policy-grid">
              <label>平台备注名<Input value={profileForm.display_name} onChange={(event) => setProfileForm({ ...profileForm, display_name: event.target.value })} /></label>
              <label>TG 名<Input value={profileForm.tg_first_name} onChange={(event) => setProfileForm({ ...profileForm, tg_first_name: event.target.value })} /></label>
              <label>TG 姓<Input value={profileForm.tg_last_name} onChange={(event) => setProfileForm({ ...profileForm, tg_last_name: event.target.value })} /></label>
              <label className="wide-field">头像上传<input type="file" accept={runtime?.avatar_allowed_types.join(',') ?? 'image/jpeg,image/png,image/webp'} onChange={(event) => setAvatarFile(event.target.files?.[0] ?? null)} /></label>
              <label className="wide-field">TG 简介<Input.TextArea value={profileForm.tg_bio} maxLength={220} onChange={(event) => setProfileForm({ ...profileForm, tg_bio: event.target.value })} /></label>
            </div>
          </div>
          <p className="muted-line">头像最大 {Math.round((runtime?.avatar_max_bytes ?? 0) / 1024 / 1024) || 2}MB；保存后会自动进入后台同步处理。</p>
          <FormActions submitLabel="保存并同步" onCancel={() => ctx.setModal({ type: 'accountDetail' })} onSubmit={saveAccountProfile} loading={isActionPending(`account:${accountDetail.account.id}:profile:save`)} disabled={!profileForm.display_name.trim()} />
          </div>
        </Modal>
      )}

      {modal?.type === 'accountDetail' && accountDetail && (
        <AccountDetailModal accountDetail={accountDetail} accountDetailTab={accountDetailTab} setAccountDetailTab={setAccountDetailTab} runtime={runtime} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} accountContacts={accountContacts} accounts={accounts} avatarUrl={avatarUrl} onClose={closeModal} onOpenAccountProfileEdit={openAccountProfileEdit} onQueueAccountSyncNow={queueAccountSyncNow} onRefreshAccountDetail={ctx.refreshAccountDetail} onPollVerificationCodes={pollVerificationCodes} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onConfirmClonePlan={confirmClonePlan} onRetryCloneItem={ctx.retryCloneItem} onRetryAccountProfileSync={retryAccountProfileSync} onDismissVerificationTask={dismissVerificationTask} onConfirmVerificationTask={confirmVerificationTask} onLoadVerificationChallengeContext={loadVerificationChallengeContext} onRefreshVerificationChallengeContext={refreshVerificationChallengeContext} onResolveGroupRestrictionTask={resolveGroupRestrictionTask} onResolveGroupRestrictionBatch={resolveGroupRestrictionBatch} onSubmitVerificationTaskResponse={submitVerificationTaskResponse} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} onSetCloneForm={setCloneForm} accountName={accountName} isActionPending={isActionPending} onReturnToRiskControl={returnToRiskControl ? () => { closeModal(); ctx.goToView('riskControl', riskControlReturnSearch(location.search)); } : undefined} canSyncAccount={hasPermission(currentUser, 'accounts.sync')} canViewCodes={hasPermission(currentUser, 'accounts.codes.read')} canSecurityRead={hasPermission(currentUser, 'accounts.security.read')} canSecurityBatch={hasPermission(currentUser, 'accounts.security.batch')} canManageAuthorizations={hasPermission(currentUser, 'accounts.authorizations.manage')} canManageCredentials={hasPermission(currentUser, 'accounts.security.credential_manage')} canProfileBatchUpdate={hasPermission(currentUser, 'accounts.profile.batch_update')} canMovePool={hasPermission(currentUser, 'accounts.pool_manage')} canClone={hasPermission(currentUser, 'accounts.clone')} />
      )}

    </>
  );
}
