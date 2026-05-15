import React, { createContext, useContext, useState, useMemo, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { App as AntdApp } from 'antd';
import { API_BASE, API_ORIGIN, api, ApiError } from '../shared/api/client';
import { operationLabel } from './components/shared';
import { hasPermission } from './utils';
import type {
  Overview,
  RuntimeConfig,
  CurrentUser,
  Tenant,
  AdminUser,
  AdminUserForm,
  CaptchaChallenge,
  CaptchaVerifyResponse,
  TokenLedger,
  UsageLedger,
  UsageSummary,
  LoginFlow,
  Account,
  AccountPool,
  DeveloperApp,
  AiProvider,
  PromptTemplate,
  TenantAiSetting,
  Material,
  MaterialCacheHealth,
  ContentKeywordRule,
  Contact,
  Group,
  MessageTask,
  ArchiveItem,
  ArchiveDetail,
  ArchiveExport,
  AuditFilters,
  AuditLog,
  VerificationCode,
  AccountSyncRecord,
  VerificationTask,
  AccountCloneItem,
  AccountClonePlan,
  AccountGroup,
  ProfileSyncRecord,
  AccountDetail,
  AccountPoolDetail,
  GroupDetail,
  ConfirmPayload,
  MessageSendBatchCreate,
  MessageSendTaskCreate,
  ModalState,
  AccountLoginForm,
} from './types';
import { VIEW_ROUTES, viewFromPath } from './routes';
import type { AppState } from './context/types';

const AppContext = createContext<AppState | null>(null);
const EMPTY_ACCOUNT_LOGIN_FORM: AccountLoginForm = {
  account: null,
  step: 'method',
  method: 'code',
  code: '',
  password_2fa: '',
  flow: null,
  error: '',
};

export function useAppContext(): AppState {
  const context = useContext(AppContext);
  if (!context) {
    throw new Error('useAppContext must be used within AppProvider');
  }
  return context;
}

interface AppProviderProps {
  children: React.ReactNode;
}

export function AppProvider({ children }: AppProviderProps) {
  const { message, modal: modalApi } = AntdApp.useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState(localStorage.getItem('tg_ops_token') ?? '');
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [loginEmail, setLoginEmail] = useState('');
  const [loginPassword, setLoginPassword] = useState('');
  const [registerForm, setRegisterForm] = useState({ name: '', email: '', phone: '', password: '' });
  const [changePasswordForm, setChangePasswordForm] = useState({ current_password: '', new_password: '', confirm_password: '' });
  const [captchaChallenge, setCaptchaChallenge] = useState<CaptchaChallenge | null>(null);
  const [captchaInput, setCaptchaInput] = useState('');
  const [captchaToken, setCaptchaToken] = useState('');
  const [captchaError, setCaptchaError] = useState('');
  const [captchaLoading, setCaptchaLoading] = useState(false);
  const [activeView, setActiveView] = useState(() => viewFromPath(location.pathname));
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [accountPools, setAccountPools] = useState<AccountPool[]>([]);
  const [selectedPoolId, setSelectedPoolId] = useState<number | ''>('');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [developerApps, setDeveloperApps] = useState<DeveloperApp[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [selectedAdminUserId, setSelectedAdminUserId] = useState<number | null>(null);
  const [selectedUserTokenLedgers, setSelectedUserTokenLedgers] = useState<TokenLedger[]>([]);
  const [adminUserForm, setAdminUserForm] = useState<AdminUserForm>({
    id: null,
    name: '',
    email: '',
    phone: '',
    role: '普通用户',
    role_template: '账号添加专员',
    subscription_status: 'pending_activation',
    menu_permissions: ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'],
    permissions: ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'],
    is_active: true,
  });
  const [tokenAdjustmentForm, setTokenAdjustmentForm] = useState({ delta_tokens: 500000, reason: '管理员充值' });
  const [usageLedgers, setUsageLedgers] = useState<UsageLedger[]>([]);
  const [usageSummary, setUsageSummary] = useState<UsageSummary | null>(null);
  const [aiProviders, setAiProviders] = useState<AiProvider[]>([]);
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [tenantAiSetting, setTenantAiSetting] = useState<TenantAiSetting | null>(null);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [materialCacheHealth, setMaterialCacheHealth] = useState<MaterialCacheHealth | null>(null);
  const [contentKeywordRules, setContentKeywordRules] = useState<ContentKeywordRule[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [tasks, setTasks] = useState<MessageTask[]>([]);
  const [taskManagementTab, setTaskManagementTab] = useState('任务列表');
  const [archives, setArchives] = useState<ArchiveItem[]>([]);
  const [archiveDetail, setArchiveDetail] = useState<ArchiveDetail | null>(null);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [auditFilters, setAuditFilters] = useState<AuditFilters>({ actor: '', action: '', target_type: '', target_id: '', keyword: '', account_id: '', operation_target_id: '', task_id: '', status: '', start_at: '', end_at: '' });
  const [accountDetail, setAccountDetail] = useState<AccountDetail | null>(null);
  const [accountDetailTab, setAccountDetailTab] = useState('资料');
  const [accountPoolDetail, setAccountPoolDetail] = useState<AccountPoolDetail | null>(null);
  const [poolDirectAccountId, setPoolDirectAccountId] = useState<number | ''>('');
  const [returnAfterVerification, setReturnAfterVerification] = useState<'accountDetail' | 'accountPoolDetail'>('accountDetail');
  const [groupDetail, setGroupDetail] = useState<GroupDetail | null>(null);
  const [accountCreateForm, setAccountCreateForm] = useState({
    display_name: '新托管账号',
    username: '',
    phone_number: '',
    pool_id: '' as number | '',
    login_method: 'code' as 'code' | 'qr',
  });
  const [accountPoolForm, setAccountPoolForm] = useState({
    name: '新账号分组',
    description: '',
    is_default: false,
  });
  const [cloneForm, setCloneForm] = useState({
    target_account_ids: [] as number[],
    clone_contacts: true,
    clone_groups: true,
  });
  const [loginAfterCreate, setLoginAfterCreate] = useState(false);
  const [accountLoginForm, setAccountLoginForm] = useState<AccountLoginForm>(EMPTY_ACCOUNT_LOGIN_FORM);
  const [profileForm, setProfileForm] = useState({
    display_name: '',
    tg_first_name: '',
    tg_last_name: '',
    tg_bio: '',
    avatar_object_key: '',
  });
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState<number | null>(null);
  const [selectedAiProviderId, setSelectedAiProviderId] = useState<number | ''>('');
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [groupPolicy, setGroupPolicy] = useState({
    active_window: '09:00-23:00',
    daily_limit: 120,
    account_cooldown_seconds: 180,
    group_cooldown_seconds: 60,
    topic_direction: '',
    banned_words: '',
    link_whitelist: '',
    require_review: false,
    listener_enabled: false,
    listener_auto_reply_enabled: true,
    listener_interval_seconds: 60,
    listener_context_limit: 20,
    listener_account_ids: [] as number[],
  });
  const [developerAppForm, setDeveloperAppForm] = useState({
    id: null as number | null,
    app_name: 'Telegram 开发者应用',
    api_id: '',
    api_hash: '',
    max_accounts: 0,
    notes: '',
    is_active: true,
  });
  const [tenantForm, setTenantForm] = useState({
    id: null as number | null,
    name: '',
    plan_name: '',
    account_quota: 50,
    task_quota: 5000,
  });
  const [aiProviderForm, setAiProviderForm] = useState({
    id: null as number | null,
    provider_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model_name: 'deepseek-v4-flash',
    api_key: '',
    api_key_header: 'Authorization',
    notes: '',
    is_active: true,
  });
  const [promptTemplateForm, setPromptTemplateForm] = useState({
    id: null as number | null,
    name: '运营群活跃模板',
    template_type: '群活跃对话计划',
    content: '请为 {{group_title}} 围绕 {{topic}} 生成 {{count}} 条自然 Telegram 群聊发言计划，语气 {{tone}}，素材 {{materials}}，输出 JSON turns，并包含角色、意图、延迟和自动校验建议。',
    is_active: true,
  });
  const [materialForm, setMaterialForm] = useState({
    id: null as number | null,
    title: '活动表情包',
    material_type: '表情包',
    content: 'https://example.local/stickers/welcome.webp',
    tags: '表情包,欢迎',
    emoji_asset_kind: 'image_meme',
    cache_ready_status: 'not_cached',
    delivery_mode: 'download_reupload',
    source_kind: 'url',
  });
  const [materialFile, setMaterialFile] = useState<File[] | null>(null);
  const [keywordRuleForm, setKeywordRuleForm] = useState({
    id: null as number | null,
    keyword: '',
    match_type: 'contains',
    is_active: true,
    note: '',
  });
  const [modal, setModal] = useState<ModalState>(null);
  const [busy, setBusy] = useState('');
  const [pendingActionKeys, setPendingActionKeys] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [directMessageForm, setDirectMessageForm] = useState({
    target_peer_id: '',
    target_display: '',
    content: '',
  });

  const accountContacts = accountDetail?.contacts ?? [];
  const poolContacts = accountPoolDetail?.contacts ?? [];
  const directMessageContacts = modal?.type === 'accountPoolDetail' ? poolContacts : accountContacts;
  const selectedDirectContact = useMemo(
    () => directMessageContacts.find((contact) => {
      const peerTarget = directMessageForm.target_peer_id;
      return peerTarget === contact.peer_id || peerTarget === (contact.username ? `@${contact.username}` : '');
    }) ?? null,
    [directMessageContacts, directMessageForm.target_peer_id],
  );
  const selectedPool = useMemo(() => accountPools.find((pool) => pool.id === selectedPoolId) ?? accountPools.find((pool) => pool.is_default) ?? accountPools[0] ?? null, [accountPools, selectedPoolId]);
  const selectedGroup = useMemo(() => groups.find((group) => group.id === selectedGroupId) ?? groups[0], [groups, selectedGroupId]);
  const choosePoolSendAccount = (detail: AccountPoolDetail) => (
    detail.accounts.find((account) => account.status === '在线' && detail.contacts.some((contact) => contact.account_id === account.id))
    ?? detail.accounts.find((account) => account.status === '在线')
    ?? detail.accounts[0]
  );
  const taskSummary = useMemo(() => ({
    queued: tasks.filter((task) => task.status === '排队中').length,
    sending: tasks.filter((task) => task.status === '发送中').length,
    sent: tasks.filter((task) => task.status === '已发送').length,
    failed: tasks.filter((task) => task.status === '失败').length,
  }), [tasks]);

  const isActionPending = React.useCallback((key: string) => pendingActionKeys.includes(key), [pendingActionKeys]);

  const runWithLoading = React.useCallback(async <T,>(key: string, busyLabel: string, task: () => Promise<T>): Promise<T> => {
    setPendingActionKeys((current) => [...current, key]);
    setBusy(busyLabel);
    try {
      return await task();
    } finally {
      setPendingActionKeys((current) => {
        const index = current.indexOf(key);
        if (index < 0) return current;
        const next = [...current];
        next.splice(index, 1);
        return next;
      });
      setBusy('');
    }
  }, []);

  useEffect(() => {
    const nextView = viewFromPath(location.pathname);
    if (activeView !== nextView) {
      setActiveView(nextView);
    }
  }, [location.pathname, activeView]);

  useEffect(() => {
    if (!selectedGroup) return;
    setGroupPolicy({
      active_window: selectedGroup.active_window,
      daily_limit: selectedGroup.daily_limit,
      account_cooldown_seconds: selectedGroup.account_cooldown_seconds,
      group_cooldown_seconds: selectedGroup.group_cooldown_seconds,
      topic_direction: selectedGroup.topic_direction,
      banned_words: selectedGroup.banned_words,
      link_whitelist: selectedGroup.link_whitelist,
      require_review: selectedGroup.require_review,
      listener_enabled: selectedGroup.listener_enabled,
      listener_auto_reply_enabled: selectedGroup.listener_auto_reply_enabled,
      listener_interval_seconds: selectedGroup.listener_interval_seconds,
      listener_context_limit: selectedGroup.listener_context_limit,
      listener_account_ids: selectedGroup.listener_account_ids ?? [],
    });
  }, [selectedGroup?.id]);

  useEffect(() => {
    if (!token) {
      void refreshCaptchaChallenge();
    }
  }, [token, authMode]);

  function auditQuery() {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(auditFilters)) {
      if (value) params.set(key, value);
    }
    const query = params.toString();
    return query ? `/audit-logs?${query}` : '/audit-logs';
  }

  // 从 Promise.allSettled 结果中提取成功值，失败时返回 fallback
  function settledValue<T>(result: PromiseSettledResult<T>, fallback: T): T {
    return result.status === 'fulfilled' ? result.value : fallback;
  }

  async function refresh() {
    setBusy('刷新数据');
    try {
      const me = await api<CurrentUser>('/auth/me');
      setCurrentUser(me);
      const accountQuery = selectedPoolId ? `/tg-accounts?pool_id=${selectedPoolId}` : '/tg-accounts';
      const results = await Promise.allSettled([
        api<RuntimeConfig>('/config/runtime'),
        api<Overview>('/overview'),
        api<AccountPool[]>('/account-pools'),
        api<Account[]>(accountQuery),
        api<Group[]>('/groups'),
        api<MessageTask[]>(`/message-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`),
        api<ArchiveItem[]>('/archives'),
        api<AuditLog[]>(auditQuery()),
        api<AiProvider[]>('/ai-providers'),
        api<PromptTemplate[]>('/prompt-templates'),
        api<TenantAiSetting>('/tenant-ai-settings'),
        api<Material[]>('/materials'),
        api<MaterialCacheHealth>('/materials/cache/health'),
        api<ContentKeywordRule[]>('/content-keyword-rules'),
      ]);
      const runtimeData = settledValue(results[0], {} as RuntimeConfig);
      const overviewData = settledValue(results[1], {} as Overview);
      const poolData = settledValue(results[2], [] as AccountPool[]);
      const accountData = settledValue(results[3], [] as Account[]);
      const groupData = settledValue(results[4], [] as Group[]);
      const taskData = settledValue(results[5], [] as MessageTask[]);
      const archiveData = settledValue(results[6], [] as ArchiveItem[]);
      const auditData = settledValue(results[7], [] as AuditLog[]);
      const aiProviderData = settledValue(results[8], [] as AiProvider[]);
      const promptTemplateData = settledValue(results[9], [] as PromptTemplate[]);
      const tenantAiData = settledValue(results[10], {} as TenantAiSetting);
      const materialData = settledValue(results[11], [] as Material[]);
      const materialCacheHealthData = settledValue(results[12], null as MaterialCacheHealth | null);
      const keywordRuleData = settledValue(results[13], [] as ContentKeywordRule[]);
      const developerAppData = hasPermission(me, 'system.view') ? await api<DeveloperApp[]>('/developer-apps').catch(() => [] as DeveloperApp[]) : [];
      const tenantData = hasPermission(me, 'system.view') ? await api<Tenant[]>('/tenants').catch(() => [] as Tenant[]) : [];
      const adminUserData = hasPermission(me, 'permissions.view') ? await api<AdminUser[]>('/admin/users').catch(() => [] as AdminUser[]) : [];
      const usageLedgerData: UsageLedger[] = [];
      const usageSummaryData: UsageSummary | null = null;
      setRuntime(runtimeData);
      setOverview(overviewData);
      setAccountPools(poolData);
      setAccounts(accountData);
      setDeveloperApps(developerAppData);
      setTenants(tenantData);
      setAdminUsers(adminUserData);
      setSelectedAdminUserId((current) => current ?? adminUserData[0]?.id ?? null);
      setUsageLedgers(usageLedgerData);
      setUsageSummary(usageSummaryData);
      setAiProviders(aiProviderData);
      setPromptTemplates(promptTemplateData);
      setTenantAiSetting(tenantAiData);
      setMaterials(materialData);
      setMaterialCacheHealth(materialCacheHealthData);
      setContentKeywordRules(keywordRuleData);
      setGroups(groupData);
      setTasks(taskData);
      setArchives(archiveData);
      setAudits(auditData);
      setSelectedGroupId((current) => current ?? groupData[0]?.id ?? null);
      setSelectedAiProviderId((current) => current || tenantAiData.default_provider_id || aiProviderData[0]?.id || '');
    } catch (error) {
      throw error;
    } finally {
      setBusy('');
    }
  }

  useEffect(() => {
    if (!token) return;
    refresh().catch((error) => {
      if (error instanceof ApiError && (error.status === 401 || error.body.includes('token expired'))) {
        localStorage.removeItem('tg_ops_token');
        setToken('');
        setCurrentUser(null);
        setNotice('登录已过期，请重新登录。');
        return;
      }
      setNotice(`后端未连接或接口异常：${error.message}`);
    });
  }, [token, taskStatusFilter, selectedPoolId]);

  function showResult(title: string, detail: string) {
    const content = title === detail ? title : `${title}：${detail}`;
    const combined = `${title} ${detail}`;
    if (/失败|异常|错误/.test(combined)) {
      void modalApi.error({ title, content: detail });
      return;
    }
    if (/请先|需要先/.test(combined)) {
      void modalApi.info({ title, content: detail });
      return;
    }
    void message.success(content);
  }

  function errorMessage(error: unknown) {
    if (error instanceof ApiError) {
      try {
        const parsed = JSON.parse(error.body) as { detail?: unknown };
        if (typeof parsed.detail === 'string') return parsed.detail;
      } catch {
        // Fall back to the raw body below.
      }
      return error.body || error.message;
    }
    return error instanceof Error ? error.message : String(error);
  }

  function handleActionError(error: unknown) {
    const message = errorMessage(error);
    showResult('操作失败', message);
  }

  function closeModal() {
    setModal(null);
  }

  function openConfirm(payload: ConfirmPayload) {
    void modalApi.confirm({
      title: payload.title,
      content: payload.message,
      okText: payload.confirmLabel ?? '确认',
      cancelText: '取消',
      okButtonProps: payload.tone === 'danger' ? { danger: true } : undefined,
      centered: true,
      onOk: async () => {
        await payload.onConfirm();
        if (payload.restoreModalType) {
          setModal({ type: payload.restoreModalType });
        }
      },
    });
  }

  function openAccountCreate(loginNow = false) {
    if (!runtime?.can_create_tg_account) {
      goToView('systemConfig');
      showResult('请先配置开发者应用', '新增 TG 账号前，需要先在开发者应用中配置可用的 Telegram api_id/api_hash。');
      return;
    }
    setLoginAfterCreate(loginNow);
    setAccountCreateForm({
      display_name: '新托管账号',
      username: '',
      phone_number: '',
      pool_id: selectedPoolId || accountPools.find((pool) => pool.is_default)?.id || accountPools[0]?.id || '',
      login_method: 'code',
    });
    setModal({ type: 'accountCreate' });
  }

  async function openAccountDetail(account: Account) {
    setBusy('读取账号详情');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    setAccountDetail(detail);
    setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    setAccountDetailTab('资料');
    setModal({ type: 'accountDetail' });
    setBusy('');
  }

  async function openAccountVerificationCodes(account: Account) {
    setBusy('提取验证码');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    setAccountDetail(detail);
    setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    setAccountDetailTab('TG 官方验证码');
    setModal({ type: 'accountDetail' });
    const codes = await api<VerificationCode[]>(`/tg-accounts/${account.id}/verification-codes/poll`, { method: 'POST' });
    setAccountDetail((current) => current?.account.id === account.id ? { ...current, verification_codes: codes } : current);
    setNotice(`${account.display_name} 已同步提取 TG 官方验证码。`);
    setBusy('');
  }

  async function openAccountMovePool(account: Account) {
    setBusy('移动账号分组');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    setAccountDetail(detail);
    setModal({ type: 'accountMovePool' });
    setBusy('');
  }

  async function openAccountPoolDetail(pool: AccountPool) {
    setBusy('读取账号分组详情');
    const detail = await api<AccountPoolDetail>(`/account-pools/${pool.id}/detail`);
    const defaultAccount = choosePoolSendAccount(detail);
    setAccountPoolDetail(detail);
    setPoolDirectAccountId(defaultAccount?.id || '');
    setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    setModal({ type: 'accountPoolDetail' });
    setBusy('');
  }

  async function refreshAccountPoolDetail() {
    if (!accountPoolDetail) return;
    const detail = await api<AccountPoolDetail>(`/account-pools/${accountPoolDetail.pool.id}/detail`);
    const selectedAccount = detail.accounts.find((account) => account.id === poolDirectAccountId);
    const defaultAccount = choosePoolSendAccount(detail);
    setAccountPoolDetail(detail);
    if (!selectedAccount || selectedAccount.status !== '在线') {
      setPoolDirectAccountId(defaultAccount?.id || '');
    }
  }

  function latestUsableCodeFlow(detail: AccountDetail) {
    return detail.login_flows.find((flow) => (
      flow.method === 'code'
      && flow.status === '等待验证码'
      && (!flow.code_expires_at || new Date(flow.code_expires_at).getTime() > Date.now())
    )) ?? null;
  }

  async function startOrResumeAccountLogin(account: Account, method: 'code' | 'qr' = 'code', resend = false) {
    const isQr = method === 'qr';
    setBusy(isQr ? '启动扫码登录' : resend ? '重新发送验证码' : '启动登录');
    setModal({ type: 'accountLogin' });
    setAccountLoginForm({
      ...EMPTY_ACCOUNT_LOGIN_FORM,
      account,
      method,
      step: account.status === '等待2FA' ? 'password' : isQr || account.status === '等待扫码' ? 'qr' : 'code',
    });
    try {
      let flow: LoginFlow | null = null;
      if (!resend && account.status === '等待验证码' && method === 'code') {
        const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
        flow = latestUsableCodeFlow(detail);
        if (accountDetail?.account.id === account.id) {
          setAccountDetail(detail);
        }
      }
      if (!flow && account.status !== '等待2FA') {
        flow = await api<LoginFlow>(`/tg-accounts/${account.id}/login/start`, {
          method: 'POST',
          body: JSON.stringify({ method }),
        });
      }
      const nextAccount = flow ? { ...account, status: flow.status } : account;
      setAccountLoginForm((current) => ({
        ...current,
        account: nextAccount,
        method,
        step: nextAccount.status === '等待2FA' ? 'password' : method === 'qr' || nextAccount.status === '等待扫码' ? 'qr' : 'code',
        flow,
        error: '',
      }));
      setNotice(isQr ? '请使用 Telegram 扫码确认登录。' : resend ? '已重新发送登录验证码。' : '请完成验证码登录。');
      await refresh();
      if (accountDetail?.account.id === account.id) await refreshAccountDetail();
    } catch (error) {
      const message = errorMessage(error);
      setAccountLoginForm((current) => ({ ...current, error: message }));
    } finally {
      setBusy('');
    }
  }

  async function completeAccountLogin(updated: Account) {
    setAccountLoginForm((current) => ({ ...current, account: updated, error: '' }));
    await refresh();
    if (updated.status === '等待2FA') {
      setAccountLoginForm((current) => ({ ...current, account: updated, step: 'password', code: '', error: '' }));
      setNotice('验证码已通过，请输入 Telegram 二步验证密码。');
      return;
    }
    if (updated.status !== '在线') {
      setAccountLoginForm((current) => ({ ...current, account: updated, error: `登录未完成，当前状态：${updated.status}` }));
      return;
    }
    const detail = await api<AccountDetail>(`/tg-accounts/${updated.id}/detail`);
    setAccountDetail(detail);
    setAccountDetailTab('TG 官方验证码');
    setAccountLoginForm(EMPTY_ACCOUNT_LOGIN_FORM);
    setModal({ type: 'accountDetail' });
    setNotice(`${updated.display_name} 已完成登录，并已同步资料、健康、群聊、联系人和验证码。`);
  }

  async function createAccount() {
    setBusy('添加账号');
    try {
      const created = await api<Account>('/tg-accounts', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: currentUser?.tenant_id ?? 1,
          pool_id: accountCreateForm.pool_id || null,
          display_name: accountCreateForm.display_name,
          username: accountCreateForm.username || null,
          phone_number: accountCreateForm.phone_number,
        }),
      });
      setAccountCreateForm({ display_name: '新托管账号', username: '', phone_number: '', pool_id: '', login_method: 'code' });
      await refresh();
      await startOrResumeAccountLogin(created, accountCreateForm.login_method, accountCreateForm.login_method === 'code');
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function deleteAccount(account: Account) {
    setBusy('移除账号');
    try {
      const removed = await api<Account>(`/tg-accounts/${account.id}`, { method: 'DELETE' });
      if (accountLoginForm.account?.id === removed.id) {
        setAccountLoginForm(EMPTY_ACCOUNT_LOGIN_FORM);
      }
      if (accountDetail?.account.id === removed.id) {
        setAccountDetail(null);
      }
      await refresh();
      setNotice(`${removed.display_name} 已移除，历史任务和归档记录仍会保留。`);
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function createAccountPool() {
    setBusy('新增账号分组');
    const pool = await api<AccountPool>('/account-pools', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: currentUser?.tenant_id ?? 1, ...accountPoolForm }),
    });
    closeModal();
    showResult('账号分组已新增', `已新增账号分组：${pool.name}`);
    setAccountPoolForm({ name: '新账号分组', description: '', is_default: false });
    setSelectedPoolId(pool.id);
    await refresh();
    setBusy('');
  }

  async function moveCurrentAccountPool(poolId: number) {
    if (!accountDetail) return;
    setBusy('移动账号分组');
    const updated = await api<Account>(`/tg-accounts/${accountDetail.account.id}/move-pool`, {
      method: 'POST',
      body: JSON.stringify({ pool_id: poolId }),
    });
    showResult('账号分组已更新', `${updated.display_name} 已移动到 ${updated.pool_name}`);
    await refresh();
    await refreshAccountDetail();
    setModal({ type: 'accountDetail' });
    setBusy('');
  }

  async function createClonePlan() {
    if (!accountDetail || !cloneForm.target_account_ids.length) return;
    setBusy('创建克隆计划');
    const clone_scope = [
      cloneForm.clone_contacts ? 'contacts' : '',
      cloneForm.clone_groups ? 'groups' : '',
    ].filter(Boolean);
    const plan = await api<AccountClonePlan>('/account-clone-plans', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: currentUser?.tenant_id ?? 1,
        source_account_id: accountDetail.account.id,
        target_account_ids: cloneForm.target_account_ids,
        clone_scope,
      }),
    });
    showResult('克隆计划已生成', `已生成 ${plan.items_total} 个克隆项，请确认后执行。`);
    await refreshAccountDetail();
    setAccountDetailTab('克隆');
    setModal({ type: 'accountDetail' });
    setBusy('');
  }

  async function confirmClonePlan(plan: AccountClonePlan) {
    setBusy('执行克隆计划');
    await api<AccountClonePlan>(`/account-clone-plans/${plan.id}/confirm`, { method: 'POST' });
    showResult('克隆计划已执行', '已按克隆计划逐项执行，失败或需人工处理的项目可在账号详情中查看。');
    await refreshAccountDetail();
    setBusy('');
  }

  async function retryCloneItem(item: AccountCloneItem) {
    setBusy('重试克隆项');
    await api<AccountCloneItem>(`/account-clone-items/${item.id}/retry`, { method: 'POST' });
    showResult('克隆项已重试', '克隆项执行结果已刷新。');
    await refreshAccountDetail();
    setBusy('');
  }

  async function confirmVerificationTask(task: VerificationTask) {
    setBusy('处理验证辅助');
    const updated = await api<VerificationTask>(`/verification-tasks/${task.id}/confirm-action`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    if (updated.status === '失败') {
      showResult('验证辅助处理失败', updated.failure_detail || `${updated.verification_type}：失败`);
    } else if (updated.status === '需人工处理') {
      showResult('仍需人工处理', updated.failure_detail || updated.detected_reason || updated.verification_type);
    } else {
      showResult('验证辅助已处理', `${updated.verification_type}：${updated.status}`);
    }
    if (accountDetail) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
    if (groupDetail) {
      const detail = await api<GroupDetail>(`/groups/${groupDetail.group.id}/detail`);
      setGroupDetail(detail);
    }
    setBusy('');
  }

  async function resolveGroupRestrictionTask(task: VerificationTask) {
    setBusy('解除群限制重查');
    const updated = await api<VerificationTask>(`/verification-tasks/${task.id}/resolve-group-restriction`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    if (updated.status === '已处理') {
      showResult('群限制已解除', updated.failure_detail || `${updated.verification_type}：目标已可发言`);
    } else if (updated.status === '需人工处理') {
      showResult('仍需管理员处理', updated.failure_detail || '当前账号在该群仍不可发言。');
    } else {
      showResult('解除群限制重查失败', updated.failure_detail || `${updated.verification_type}：${updated.status}`);
    }
    if (accountDetail) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
    if (groupDetail) {
      const detail = await api<GroupDetail>(`/groups/${groupDetail.group.id}/detail`);
      setGroupDetail(detail);
    }
    setBusy('');
  }

  async function dismissVerificationTask(task: VerificationTask) {
    setBusy('忽略验证辅助');
    await api<VerificationTask>(`/verification-tasks/${task.id}/dismiss`, { method: 'POST' });
    showResult('验证辅助已忽略', '该验证事项已从待处理列表移除。');
    if (accountDetail) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
    setBusy('');
  }

  async function refreshAccountDetail() {
    if (!accountDetail) return;
    const detail = await api<AccountDetail>(`/tg-accounts/${accountDetail.account.id}/detail`);
    setAccountDetail(detail);
  }

  async function syncAccountContacts() {
    if (!accountDetail) return;
    setBusy('同步联系人');
    await api<Contact[]>(`/tg-accounts/${accountDetail.account.id}/contacts/sync`, { method: 'POST' });
    await refreshAccountDetail();
    showResult('联系人已同步', '已刷新联系人和群友候选，可以直接选中对象创建平台发送任务。');
    setBusy('');
  }

  async function queueAccountSyncNow() {
    if (!accountDetail) return;
    setBusy('同步账号数据');
    await api<AccountSyncRecord[]>(`/tg-accounts/${accountDetail.account.id}/sync-now`, { method: 'POST' });
    await refreshAccountDetail();
    await refresh();
    showResult('同步完成', '已同步资料、健康、群聊、云联系人和 TG 官方验证码。');
    setBusy('');
  }

  function startDirectMessageToContact(contact: Contact) {
    if (modal?.type === 'accountPoolDetail') {
      setPoolDirectAccountId(contact.account_id);
    }
    setDirectMessageForm({
      target_peer_id: contact.username ? `@${contact.username}` : contact.peer_id,
      target_display: contact.display_name,
      content: '',
    });
    setAccountDetailTab('云联系人');
  }

  async function openGroupDetail(group: Group) {
    setBusy('读取群详情');
    const detail = await api<GroupDetail>(`/groups/${group.id}/detail`);
    setGroupDetail(detail);
    setSelectedGroupId(group.id);
    setModal({ type: 'groupDetail' });
    setBusy('');
  }

  function avatarUrl(value: string) {
    if (!value) return '';
    return value.startsWith('http') ? value : `${API_ORIGIN}${value}`;
  }

  function openAccountProfileEdit() {
    if (!accountDetail) return;
    setProfileForm({
      display_name: accountDetail.account.display_name,
      tg_first_name: accountDetail.account.tg_first_name || '',
      tg_last_name: accountDetail.account.tg_last_name || '',
      tg_bio: accountDetail.account.tg_bio || '',
      avatar_object_key: accountDetail.account.avatar_object_key || '',
    });
    setAvatarFile(null);
    setModal({ type: 'accountProfileEdit' });
  }

  async function pollVerificationCodes(silent = false) {
    if (!accountDetail) return;
    const accountId = accountDetail.account.id;
    if (!silent) setBusy('同步验证码');
    const codes = await api<VerificationCode[]>(`/tg-accounts/${accountId}/verification-codes/poll`, { method: 'POST' });
    setAccountDetail((current) => current?.account.id === accountId ? { ...current, verification_codes: codes } : current);
    if (!silent) {
      showResult('验证码已同步', '已从 TG 官方服务消息同步最新验证码，验证码会短时展示并写入审计。');
      setBusy('');
    }
  }

  async function createDirectMessageTask() {
    if (!accountDetail && !accountPoolDetail) return;
    setBusy('创建私发任务');
    const path = accountPoolDetail
      ? `/account-pools/${accountPoolDetail.pool.id}/direct-message-tasks`
      : `/tg-accounts/${accountDetail?.account.id}/direct-message-tasks`;
    await api<MessageTask>(path, {
      method: 'POST',
      body: JSON.stringify({
        ...directMessageForm,
        account_id: accountPoolDetail ? poolDirectAccountId || null : accountDetail?.account.id,
        target_display: directMessageForm.target_display || directMessageForm.target_peer_id,
        message_type: '文本',
      }),
    });
    showResult('私发消息已提交', '系统会按账号状态发送，可在账号发送记录中查看结果。');
    setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    await refresh();
    if (accountDetail) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
    setBusy('');
  }

  async function createMessageSendTask(payload: MessageSendTaskCreate | MessageSendBatchCreate) {
    setBusy('创建消息发送任务');
    try {
      const isBatch = 'targets' in payload;
      const result = await api<MessageTask | MessageTask[]>('/message-send-tasks' + (isBatch ? '/batch' : ''), {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const created = Array.isArray(result) ? result : [result];
      setTasks((current) => [...created, ...current.filter((item) => !created.some((task) => task.id === item.id))]);
      await refresh();
      return created;
    } finally {
      setBusy('');
    }
  }

  async function saveAccountProfile() {
    if (!accountDetail) return;
    setBusy('保存账号资料');
    let avatarObjectKey = profileForm.avatar_object_key;
    if (avatarFile) {
      const form = new FormData();
      form.append('file', avatarFile);
      const uploaded = await api<{ object_key: string; preview_url: string }>(`/tg-accounts/${accountDetail.account.id}/avatar`, {
        method: 'POST',
        body: form,
      });
      avatarObjectKey = uploaded.object_key;
    }
    await api<Account>(`/tg-accounts/${accountDetail.account.id}/profile`, {
      method: 'PATCH',
      body: JSON.stringify({ ...profileForm, avatar_object_key: avatarObjectKey }),
    });
    closeModal();
    showResult('账号资料已保存', '资料已进入后台同步处理，可在账号详情中查看同步状态。');
    await refresh();
    const detail = await api<AccountDetail>(`/tg-accounts/${accountDetail.account.id}/detail`);
    setAccountDetail(detail);
    setAccountDetailTab('资料');
    setModal({ type: 'accountDetail' });
    setBusy('');
  }

  async function retryAccountProfileSync() {
    if (!accountDetail) return;
    setBusy('重试资料同步');
    await api<ProfileSyncRecord>(`/tg-accounts/${accountDetail.account.id}/profile-sync/retry`, { method: 'POST' });
    showResult('已重新提交', '账号资料同步已重新提交处理。');
    await refresh();
    await refreshAccountDetail();
    setBusy('');
  }

  async function refreshCaptchaChallenge() {
    setCaptchaLoading(true);
    setCaptchaError('');
    setCaptchaToken('');
    try {
      const challenge = await api<CaptchaChallenge>('/auth/captcha/challenge');
      setCaptchaChallenge(challenge);
      setCaptchaInput('');
    } catch (error) {
      setCaptchaChallenge(null);
      setCaptchaError('验证码加载失败，请刷新重试');
    } finally {
      setCaptchaLoading(false);
    }
  }

  async function verifyCaptcha() {
    if (!captchaChallenge) {
      setCaptchaError('请先刷新验证码');
      return;
    }
    setCaptchaLoading(true);
    setCaptchaError('');
    setCaptchaToken('');
    try {
      const captcha = await api<CaptchaVerifyResponse>('/auth/captcha/verify', {
        method: 'POST',
        body: JSON.stringify({ challenge_id: captchaChallenge.challenge_id, captcha_value: captchaInput }),
      });
      setCaptchaToken(captcha.captcha_token);
    } catch (error) {
      setCaptchaError('验证码验证失败，请重新输入');
    } finally {
      setCaptchaLoading(false);
    }
  }

  async function login() {
    if (!captchaToken) {
      setNotice('请先完成验证码验证');
      return;
    }
    setBusy('登录');
    setNotice('');
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier: loginEmail, email: loginEmail, password: loginPassword, captcha_token: captchaToken }),
    });
    if (!response.ok) {
      setBusy('');
      setNotice('登录失败，请检查账号和密码');
      await refreshCaptchaChallenge();
      return;
    }
    const data = await response.json();
    localStorage.setItem('tg_ops_token', data.access_token);
    setToken(data.access_token);
    setCurrentUser(data.user);
    setNotice('');
    setBusy('');
  }

  async function register() {
    if (!captchaToken) {
      setNotice('请先完成验证码验证');
      return;
    }
    setBusy('注册');
    setNotice('');
    try {
      const response = await fetch(`${API_BASE}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...registerForm, captcha_token: captchaToken }),
      });
      if (!response.ok) {
        setBusy('');
        setNotice('注册失败，请检查填写信息');
        await refreshCaptchaChallenge();
        return;
      }
      const data = await response.json();
      localStorage.setItem('tg_ops_token', data.access_token);
      setToken(data.access_token);
      setCurrentUser(data.user);
      setAuthMode('login');
      setNotice('注册成功，已进入运营管理平台。');
    } finally {
      setBusy('');
    }
  }

  async function changePassword() {
    if (changePasswordForm.new_password !== changePasswordForm.confirm_password) {
      setNotice('两次输入的新密码不一致');
      return;
    }
    setBusy('修改密码');
    try {
      const user = await api<CurrentUser>('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: changePasswordForm.current_password,
          new_password: changePasswordForm.new_password,
        }),
      });
      setCurrentUser(user);
      setChangePasswordForm({ current_password: '', new_password: '', confirm_password: '' });
      closeModal();
      showResult('密码已修改', '下次登录请使用新密码。');
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  function logout() {
    localStorage.removeItem('tg_ops_token');
    setToken('');
    setCurrentUser(null);
    setNotice('');
  }

  async function runLogin(account: Account, method: 'code' | 'qr') {
    await startOrResumeAccountLogin(account, method, method === 'code');
  }

  async function verifyAccount(account: Account) {
    setModal({ type: 'accountLogin' });
    setAccountLoginForm({
      ...EMPTY_ACCOUNT_LOGIN_FORM,
      account,
      method: account.status === '等待扫码' ? 'qr' : 'code',
      step: account.status === '等待2FA' ? 'password' : 'method',
    });
  }

  async function chooseAccountLoginMethod(method: 'code' | 'qr') {
    if (!accountLoginForm.account) return;
    await startOrResumeAccountLogin(accountLoginForm.account, method, false);
  }

  async function submitAccountLoginCode() {
    if (!accountLoginForm.account || !accountLoginForm.code.trim()) return;
    setBusy('验证登录');
    try {
      const updated = await api<Account>(`/tg-accounts/${accountLoginForm.account.id}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ code: accountLoginForm.code.trim() }),
      });
      await completeAccountLogin(updated);
    } catch (error) {
      const message = errorMessage(error);
      setAccountLoginForm((current) => ({ ...current, error: message }));
    } finally {
      setBusy('');
    }
  }

  async function submitAccountLoginPassword() {
    if (!accountLoginForm.account || !accountLoginForm.password_2fa) return;
    setBusy('验证二步密码');
    try {
      const updated = await api<Account>(`/tg-accounts/${accountLoginForm.account.id}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ password_2fa: accountLoginForm.password_2fa }),
      });
      await completeAccountLogin(updated);
    } catch (error) {
      const message = errorMessage(error);
      setAccountLoginForm((current) => ({ ...current, error: message }));
    } finally {
      setBusy('');
    }
  }

  async function resendAccountLoginCode() {
    if (!accountLoginForm.account) return;
    await startOrResumeAccountLogin(accountLoginForm.account, 'code', true);
  }

  async function checkAccountQrLogin() {
    if (!accountLoginForm.account) return;
    setBusy('检查扫码结果');
    try {
      const updated = await api<Account>(`/tg-accounts/${accountLoginForm.account.id}/login/qr/check`, { method: 'POST' });
      await completeAccountLogin(updated);
    } catch (error) {
      const message = errorMessage(error);
      setAccountLoginForm((current) => ({ ...current, error: message }));
    } finally {
      setBusy('');
    }
  }

  async function healthCheck(account: Account) {
    setBusy('健康检查');
    const result = await api<Account>(`/tg-accounts/${account.id}/health-check`, { method: 'POST' });
    showResult('健康检查完成', `${account.display_name}：${result.status}，健康分 ${result.health_score}`);
    await refresh();
    if (accountDetail?.account.id === account.id) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
  }

  async function syncAccountGroups(account: Account) {
    setBusy('同步账号数据');
    await api<AccountSyncRecord[]>(`/tg-accounts/${account.id}/sync-now`, { method: 'POST' });
    await api(`/tg-accounts/${account.id}/sync-targets`, { method: 'POST' }).catch(() => undefined);
    showResult('同步完成', `${account.display_name} 已同步资料、健康、群/频道目标、云联系人和验证码。`);
    await refresh();
    if (accountDetail?.account.id === account.id) await refreshAccountDetail();
    if (accountPoolDetail) await refreshAccountPoolDetail();
  }

  async function cancelTask(task: MessageTask) {
    setBusy('取消任务');
    const updated = await api<MessageTask>(`/message-tasks/${task.id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    showResult('发送明细已取消', `发送明细 #${updated.id} 已取消，不会继续发送。`);
    await refresh();
    setBusy('');
  }

  async function dispatchTask(task: MessageTask) {
    setBusy('派发消息');
    const result = await api<MessageTask>(`/message-tasks/${task.id}/dispatch`, { method: 'POST' });
    showResult('调度完成', result.status === '已发送' ? '消息已发送并记录回执。' : `发送失败：${result.failure_type}`);
    await refresh();
  }

  async function drainQueue() {
    setBusy('处理到期发送');
    const result = await api<{ processed: number }>('/worker/drain-once', { method: 'POST' });
    showResult('到期发送已处理', `本次已处理 ${result.processed} 条到期任务。`);
    await refresh();
  }

  async function retryTask(task: MessageTask) {
    setBusy('重试任务');
    const result = await api<MessageTask>(`/message-tasks/${task.id}/retry`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户', dispatch_now: true }),
    });
    showResult('重试完成', result.status === '已发送' ? '重试成功，消息已发送。' : `重试结果：${result.status}`);
    await refresh();
  }

  async function authorizeSelectedGroup(status: string) {
    if (!selectedGroup) return;
    setBusy('更新授权');
    await api(`/groups/${selectedGroup.id}/authorize`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户', auth_status: status }),
    });
    showResult('群使用范围已更新', `${selectedGroup.title} 已设置为 ${operationLabel(status)}`);
    await refresh();
  }

  async function createArchive() {
    if (!selectedGroup) return;
    setBusy('创建归档');
    await api('/archives', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: currentUser?.tenant_id ?? 1,
        group_id: selectedGroup.id,
        title: `${selectedGroup.title} 内容与成员归档`,
      }),
    });
    showResult('归档已创建', '已生成群归档和新群初始化方案。');
    goToView('groupManagement');
    await refresh();
  }

  async function saveGroupPolicy() {
    if (!selectedGroup) return;
    setBusy('保存群配置');
    await api(`/groups/${selectedGroup.id}`, {
      method: 'PATCH',
      body: JSON.stringify(groupPolicy),
    });
    closeModal();
    showResult('运营配置已保存', `${selectedGroup.title} 的限频、自动校验和内容规则已更新。`);
    await refresh();
  }

  async function openArchiveDetail(archive: ArchiveItem) {
    setBusy('读取归档');
    const detail = await api<ArchiveDetail>(`/archives/${archive.id}`);
    setArchiveDetail(detail);
    setBusy('');
  }

  async function exportArchive(archive: ArchiveItem) {
    setBusy('导出归档');
    const exported = await api<ArchiveExport>(`/archives/${archive.id}/export`, {
      method: 'POST',
      body: JSON.stringify({ export_format: 'json' }),
    });
    showResult('归档导出已生成', `已生成 JSON 导出数据：消息 ${exported.message_count} 条，成员 ${exported.member_count} 个，并写入审计。`);
    await refresh();
    setBusy('');
  }

  async function rerunArchive(archive: ArchiveItem) {
    setBusy('重跑归档');
    const updated = await api<ArchiveItem>(`/archives/${archive.id}/rerun`, { method: 'POST' });
    setNotice(`${updated.title} 已重新进入归档流程。`);
    await refresh();
    setBusy('');
  }

  async function createDeveloperApp() {
    const editing = developerAppForm.id !== null;
    setBusy(editing ? '保存开发者应用' : '新增开发者应用');
    try {
      const payload = {
        app_name: developerAppForm.app_name,
        api_id: Number(developerAppForm.api_id),
        api_hash: developerAppForm.api_hash || undefined,
        max_accounts: Number(developerAppForm.max_accounts),
        notes: developerAppForm.notes,
        is_active: developerAppForm.is_active,
      };
      const saved = editing
        ? await api<DeveloperApp>(`/developer-apps/${developerAppForm.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<DeveloperApp>('/developer-apps', { method: 'POST', body: JSON.stringify(payload) });
      closeModal();
      showResult(editing ? '开发者应用已保存' : '开发者应用已新增', `${saved.app_name} 当前状态：${saved.health_status}`);
      setDeveloperAppForm({ id: null, app_name: 'Telegram 开发者应用', api_id: '', api_hash: '', max_accounts: 0, notes: '', is_active: true });
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  function openDeveloperAppEdit(app: DeveloperApp) {
    setDeveloperAppForm({
      id: app.id,
      app_name: app.app_name,
      api_id: String(app.api_id),
      api_hash: '',
      max_accounts: app.max_accounts,
      notes: app.notes,
      is_active: app.is_active,
    });
    setModal({ type: 'developerAppEdit' });
  }

  function openTenantEdit(tenant: Tenant) {
    setTenantForm({
      id: tenant.id,
      name: tenant.name,
      plan_name: tenant.plan_name,
      account_quota: tenant.account_quota,
      task_quota: tenant.task_quota,
    });
    setModal({ type: 'tenantEdit' });
  }

  async function saveTenantQuota() {
    if (!tenantForm.id) return;
    setBusy('保存租户配额');
    await api(`/tenants/${tenantForm.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        name: tenantForm.name,
        plan_name: tenantForm.plan_name,
        account_quota: tenantForm.account_quota,
        task_quota: tenantForm.task_quota,
      }),
    });
    closeModal();
    showResult('租户配额已更新', `${tenantForm.name} 的账号和任务配额已保存。`);
    await refresh();
  }

  function openAdminUserEdit(user: AdminUser) {
    setAdminUserForm({
      id: user.id,
      name: user.name,
      email: user.email,
      phone: user.phone ?? '',
      role: user.role,
      role_template: user.role_template,
      subscription_status: user.subscription_status,
      menu_permissions: user.permissions?.includes('*') ? ['*'] : (user.permissions ?? user.menu_permissions),
      permissions: user.permissions?.includes('*') ? ['*'] : (user.permissions ?? user.menu_permissions),
      is_active: user.is_active,
    });
    setSelectedAdminUserId(user.id);
    void loadUserTokenLedgers(user.id);
    setModal({ type: 'adminUserEdit' });
  }

  function openAdminUserCreate() {
    const permissions = ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'];
    setAdminUserForm({
      id: null,
      name: '',
      email: '',
      phone: '',
      role: '后台用户',
      role_template: '账号添加专员',
      subscription_status: 'active',
      menu_permissions: permissions,
      permissions,
      is_active: true,
    });
    setSelectedAdminUserId(null);
    setModal({ type: 'adminUserEdit' });
  }

  async function saveAdminUser() {
    setBusy('保存用户');
    try {
      const saved = await api<AdminUser>(adminUserForm.id ? `/admin/users/${adminUserForm.id}` : '/admin/users', {
        method: adminUserForm.id ? 'PATCH' : 'POST',
        body: JSON.stringify({
          name: adminUserForm.name,
          email: adminUserForm.email,
          phone: adminUserForm.phone || null,
          role: adminUserForm.role,
          role_template: adminUserForm.role_template,
          subscription_status: adminUserForm.subscription_status,
          menu_permissions: adminUserForm.permissions,
          permissions: adminUserForm.permissions,
          is_active: adminUserForm.is_active,
        }),
      });
      setNotice(`用户已保存：${saved.name}`);
      closeModal();
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function resetAdminUserPassword(user: AdminUser, newPassword: string) {
    setBusy('重置密码');
    try {
      await api<AdminUser>(`/admin/users/${user.id}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ new_password: newPassword }),
      });
      setNotice(`${user.name} 的密码已重置。`);
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function adjustAdminUserTokens(user: AdminUser) {
    setBusy('调整 Token');
    try {
      const saved = await api<AdminUser>(`/admin/users/${user.id}/token-adjustments`, {
        method: 'POST',
        body: JSON.stringify(tokenAdjustmentForm),
      });
      setNotice(`${saved.name} 当前 Token 余额：${saved.token_balance}`);
      await loadUserTokenLedgers(user.id);
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function loadUserTokenLedgers(userId: number) {
    const ledgers = await api<TokenLedger[]>(`/admin/users/${userId}/token-ledgers`);
    setSelectedAdminUserId(userId);
    setSelectedUserTokenLedgers(ledgers);
  }

  async function toggleDeveloperApp(app: DeveloperApp) {
    setBusy(app.is_active ? '禁用开发者应用' : '启用开发者应用');
    try {
      const updated = await api<DeveloperApp>(`/developer-apps/${app.id}/${app.is_active ? 'disable' : 'enable'}`, { method: 'POST' });
      showResult('开发者应用状态已更新', `${updated.app_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function checkDeveloperApp(app: DeveloperApp) {
    setBusy('检查开发者应用');
    try {
      const checked = await api<DeveloperApp>(`/developer-apps/${app.id}/check`, { method: 'POST' });
      showResult('检查完成', `${checked.app_name}：${checked.health_status}`);
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function createAiProvider() {
    const editing = aiProviderForm.id !== null;
    setBusy(editing ? '保存 AI 供应商' : '新增 AI 供应商');
    try {
      const payload = {
        ...aiProviderForm,
        provider_type: 'openai_compatible',
        api_key: aiProviderForm.api_key || undefined,
      };
      const saved = editing
        ? await api<AiProvider>(`/ai-providers/${aiProviderForm.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<AiProvider>('/ai-providers', { method: 'POST', body: JSON.stringify(payload) });
      closeModal();
      showResult(editing ? 'AI 供应商已保存' : 'AI 供应商已新增', `${saved.provider_name} 当前状态：${saved.health_status}`);
      setAiProviderForm({ id: null, provider_name: 'DeepSeek', base_url: 'https://api.deepseek.com', model_name: 'deepseek-v4-flash', api_key: '', api_key_header: 'Authorization', notes: '', is_active: true });
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  function openAiProviderEdit(provider: AiProvider) {
    setAiProviderForm({
      id: provider.id,
      provider_name: provider.provider_name,
      base_url: provider.base_url,
      model_name: provider.model_name,
      api_key: '',
      api_key_header: provider.api_key_header,
      notes: provider.notes,
      is_active: provider.is_active,
    });
    setModal({ type: 'aiProviderEdit' });
  }

  async function toggleAiProvider(provider: AiProvider) {
    setBusy(provider.is_active ? '禁用 AI 供应商' : '启用 AI 供应商');
    try {
      const updated = await api<AiProvider>(`/ai-providers/${provider.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: !provider.is_active }),
      });
      showResult('AI 供应商状态已更新', `${updated.provider_name} 已${updated.is_active ? '启用' : '禁用'}`);
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function checkAiProvider(provider: AiProvider) {
    setBusy('检查 AI 供应商');
    try {
      const checked = await api<AiProvider>(`/ai-providers/${provider.id}/check`, { method: 'POST' });
      const detailLabel = checked.health_status === '健康' ? '警告' : '错误';
      const errorSummary = checked.last_error ? `，${detailLabel}：${checked.last_error.slice(0, 220)}${checked.last_error.length > 220 ? '...' : ''}` : '';
      showResult('AI 供应商检查完成', `${checked.provider_name}：${checked.health_status}${errorSummary}`);
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function saveTenantAiSetting() {
    if (!tenantAiSetting) return;
    setBusy('保存 AI 配置');
    try {
      await api('/tenant-ai-settings', {
        method: 'PATCH',
        body: JSON.stringify({
          default_provider_id: selectedAiProviderId || null,
          ai_enabled: tenantAiSetting.ai_enabled,
          fallback_to_mock: tenantAiSetting.fallback_to_mock,
          temperature: tenantAiSetting.temperature,
          max_tokens: tenantAiSetting.max_tokens,
        }),
      });
      closeModal();
      showResult('AI 配置已保存', '运营默认模型、温度、Token 和回退策略已更新。');
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function createPromptTemplate() {
    setBusy('新增提示词');
    const { id: _id, ...payload } = promptTemplateForm;
    const template = await api<PromptTemplate>('/prompt-templates', {
      method: 'POST',
      body: JSON.stringify({ ...payload, tenant_id: currentUser?.tenant_id ?? 1 }),
    });
    closeModal();
    showResult('提示词已新增', `已新增提示词模板：${template.name}`);
    setPromptTemplateForm({ ...promptTemplateForm, id: null, name: '运营群活跃模板', is_active: true });
    await refresh();
  }

  function openPromptTemplateEdit(template: PromptTemplate) {
    setPromptTemplateForm({
      id: template.id,
      name: template.name,
      template_type: template.template_type,
      content: template.content,
      is_active: template.is_active,
    });
    setModal({ type: 'promptTemplateEdit' });
  }

  async function savePromptTemplate() {
    if (!promptTemplateForm.id) return createPromptTemplate();
    setBusy('保存提示词');
    const { id, ...payload } = promptTemplateForm;
    const template = await api<PromptTemplate>(`/prompt-templates/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    setPromptTemplates((current) => current.map((item) => item.id === template.id ? template : item));
    closeModal();
    showResult('提示词已保存', `已更新提示词模板：${template.name}`);
    await refresh();
  }

  async function createMaterial() {
    setBusy('新增素材');
    const { id: _id, ...payload } = materialForm;
    let material: Material;
    if (payload.source_kind === 'upload') {
      const files = materialFile ?? [];
      if (!files.length) throw new Error('请先选择素材文件');
      const form = new FormData();
      form.append('title', payload.title);
      form.append('material_type', payload.material_type);
      form.append('tags', payload.tags);
      form.append('caption', payload.content);
      form.append('emoji_asset_kind', payload.emoji_asset_kind);
      form.append('tenant_id', String(currentUser?.tenant_id ?? 1));
      if (files.length === 1) {
        form.append('file', files[0]);
        material = await api<Material>('/materials/upload', {
          method: 'POST',
          body: form,
        });
      } else {
        files.forEach((file) => form.append('files', file));
        const uploaded = await api<Material[]>('/materials/upload/batch', {
          method: 'POST',
          body: form,
        });
        if (!uploaded.length) throw new Error('批量上传未返回素材');
        material = uploaded[0];
      }
    } else {
      material = await api<Material>('/materials', {
        method: 'POST',
        body: JSON.stringify({ ...payload, tenant_id: currentUser?.tenant_id ?? 1 }),
      });
    }
    setMaterialFile(null);
    closeModal();
    showResult('素材已新增', `已新增素材：${payload.source_kind === 'upload' && (materialFile?.length ?? 0) > 1 ? `${materialFile?.length ?? 0} 个文件` : material.title}`);
    await refresh();
  }

  function openMaterialEdit(material: Material) {
    setMaterialForm({
      id: material.id,
      title: material.title,
      material_type: material.material_type,
      content: material.content,
      tags: material.tags,
      emoji_asset_kind: material.emoji_asset_kind || (material.material_type === '表情包' ? 'image_meme' : ''),
      cache_ready_status: material.cache_ready_status || 'not_cached',
      delivery_mode: material.delivery_mode || 'download_reupload',
      source_kind: material.source_kind || 'url',
    });
    setMaterialFile(null);
    setModal({ type: 'materialEdit' });
  }

  async function saveMaterial() {
    if (!materialForm.id) return createMaterial();
    setBusy('保存素材');
    const { id, ...payload } = materialForm;
    const material = await api<Material>(`/materials/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    setMaterials((current) => current.map((item) => item.id === material.id ? material : item));
    closeModal();
    showResult('素材已保存', `已更新素材：${material.title}`);
    await refresh();
  }

  function openContentKeywordRuleEdit(rule: ContentKeywordRule) {
    setKeywordRuleForm({
      id: rule.id,
      keyword: rule.keyword,
      match_type: rule.match_type,
      is_active: rule.is_active,
      note: rule.note,
    });
    setModal({ type: 'keywordRuleEdit' });
  }

  async function createContentKeywordRule() {
    setBusy('新增关键词');
    const rule = await api<ContentKeywordRule>('/content-keyword-rules', {
      method: 'POST',
      body: JSON.stringify({ ...keywordRuleForm, tenant_id: currentUser?.tenant_id ?? 1 }),
    });
    closeModal();
    showResult('关键词已新增', `已新增过滤关键词：${rule.keyword}`);
    setKeywordRuleForm({ id: null, keyword: '', match_type: 'contains', is_active: true, note: '' });
    await refresh();
  }

  async function saveContentKeywordRule() {
    if (!keywordRuleForm.id) return createContentKeywordRule();
    setBusy('保存关键词');
    const rule = await api<ContentKeywordRule>(`/content-keyword-rules/${keywordRuleForm.id}`, {
      method: 'PATCH',
      body: JSON.stringify(keywordRuleForm),
    });
    closeModal();
    showResult('关键词已保存', `已更新过滤关键词：${rule.keyword}`);
    await refresh();
  }

  function accountName(accountId: number | null | undefined) {
    if (!accountId) return '未指定';
    const account = accounts.find((item) => item.id === accountId);
    return account ? `${account.display_name} #${account.id}` : `账号 ${accountId}`;
  }

  function groupName(groupId: number | null | undefined) {
    if (!groupId) return '未指定';
    return groups.find((item) => item.id === groupId)?.title ?? `群 ${groupId}`;
  }

  function goToView(viewId: string) {
    setActiveView(viewId);
    navigate(VIEW_ROUTES[viewId] ?? '/dashboard');
  }

  // 使用 useMemo 稳定 context value 引用，避免消费者不必要的重渲染
  const value: AppState = {
    // Auth state
    token,
    setToken,
    currentUser,
    setCurrentUser,
    authMode,
    setAuthMode,
    loginEmail,
    setLoginEmail,
    loginPassword,
    setLoginPassword,
    registerForm,
    setRegisterForm,
    changePasswordForm,
    setChangePasswordForm,
    captchaChallenge,
    captchaInput,
    setCaptchaInput,
    captchaToken,
    captchaError,
    captchaLoading,
    refreshCaptchaChallenge,
    verifyCaptcha,

    // View state
    activeView,
    setActiveView,
    goToView,

    // Runtime & Overview
    runtime,
    setRuntime,
    overview,
    setOverview,

    // Account pools & accounts
    accountPools,
    setAccountPools,
    selectedPoolId,
    setSelectedPoolId,
    accounts,
    setAccounts,

    // Developer apps
    developerApps,
    setDeveloperApps,
    tenants,
    setTenants,
    adminUsers,
    setAdminUsers,
    selectedAdminUserId,
    setSelectedAdminUserId,
    selectedUserTokenLedgers,
    setSelectedUserTokenLedgers,
    adminUserForm,
    setAdminUserForm,
    tokenAdjustmentForm,
    setTokenAdjustmentForm,

    // Usage
    usageLedgers,
    setUsageLedgers,
    usageSummary,
    setUsageSummary,

    // AI & Templates
    aiProviders,
    setAiProviders,
    promptTemplates,
    setPromptTemplates,
    tenantAiSetting,
    setTenantAiSetting,
    selectedAiProviderId,
    setSelectedAiProviderId,
    materials,
    setMaterials,
    materialCacheHealth,
    setMaterialCacheHealth,
    contentKeywordRules,
    setContentKeywordRules,

    // Groups & tasks
    groups,
    setGroups,
    tasks,
    setTasks,
    taskManagementTab,
    setTaskManagementTab,

    // Archives
    archives,
    setArchives,
    archiveDetail,
    setArchiveDetail,

    // Audits
    audits,
    setAudits,
    auditFilters,
    setAuditFilters,

    // Detail states
    accountDetail,
    setAccountDetail,
    accountContacts,
    selectedDirectContact,
    accountDetailTab,
    setAccountDetailTab,
    accountPoolDetail,
    setAccountPoolDetail,
    poolDirectAccountId,
    setPoolDirectAccountId,
    returnAfterVerification,
    setReturnAfterVerification,
    groupDetail,
    setGroupDetail,

    // Account forms
    accountCreateForm,
    setAccountCreateForm,
    accountPoolForm,
    setAccountPoolForm,
    cloneForm,
    setCloneForm,
    loginAfterCreate,
    setLoginAfterCreate,
    accountLoginForm,
    setAccountLoginForm,
    profileForm,
    setProfileForm,
    avatarFile,
    setAvatarFile,

    // Group state
    selectedGroupId,
    setSelectedGroupId,
    taskStatusFilter,
    setTaskStatusFilter,
    groupPolicy,
    setGroupPolicy,

    // Developer & AI forms
    developerAppForm,
    setDeveloperAppForm,
    tenantForm,
    setTenantForm,
    aiProviderForm,
    setAiProviderForm,
    promptTemplateForm,
    setPromptTemplateForm,
    materialForm,
    setMaterialForm,
    materialFile,
    setMaterialFile,
    keywordRuleForm,
    setKeywordRuleForm,

    // Modal & Dialog
    modal,
    setModal,
    busy,
    setBusy,
    pendingActionKeys,
    isActionPending,
    notice,
    setNotice,

    // Direct message
    directMessageForm,
    setDirectMessageForm,

    // Computed values
    selectedPool,
    selectedGroup,
    taskSummary,

    // Handler functions
    refresh: () => runWithLoading('app:refresh', '刷新数据', refresh),
    showResult,
    closeModal,
    openConfirm,
    openAccountCreate,
    openAccountDetail: (account) => runWithLoading(`account:${account.id}:detail`, '读取账号详情', () => openAccountDetail(account)),
    openAccountVerificationCodes: (account) => runWithLoading(`account:${account.id}:codes`, '提取验证码', () => openAccountVerificationCodes(account)),
    openAccountMovePool: (account) => runWithLoading(`account:${account.id}:move-pool`, '移动账号分组', () => openAccountMovePool(account)),
    openAccountPoolDetail: (pool) => runWithLoading(`account-pool:${pool.id}:detail`, '读取账号分组详情', () => openAccountPoolDetail(pool)),
    refreshAccountPoolDetail: () => runWithLoading(`account-pool:${accountPoolDetail?.pool.id ?? 'current'}:refresh`, '刷新账号分组', refreshAccountPoolDetail),
    createAccount: () => runWithLoading('modal:account:create', '添加账号', createAccount),
    createAccountPool: () => runWithLoading('modal:account-pool:create', '新增账号分组', createAccountPool),
    moveCurrentAccountPool: (poolId) => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:move-pool`, '移动账号分组', () => moveCurrentAccountPool(poolId)),
    createClonePlan: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:clone-plan:create`, '创建克隆计划', createClonePlan),
    confirmClonePlan: (plan) => runWithLoading(`clone-plan:${plan.id}:confirm`, '执行克隆计划', () => confirmClonePlan(plan)),
    retryCloneItem: (item) => runWithLoading(`clone-item:${item.id}:retry`, '重试克隆项', () => retryCloneItem(item)),
    confirmVerificationTask: (task) => runWithLoading(`verification:${task.id}:confirm`, '处理验证辅助', () => confirmVerificationTask(task)),
    resolveGroupRestrictionTask: (task) => runWithLoading(`verification:${task.id}:resolve-group`, '解除群限制重查', () => resolveGroupRestrictionTask(task)),
    dismissVerificationTask: (task) => runWithLoading(`verification:${task.id}:dismiss`, '忽略验证辅助', () => dismissVerificationTask(task)),
    refreshAccountDetail: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:detail-refresh`, '刷新账号详情', refreshAccountDetail),
    syncAccountContacts: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:contacts`, '同步联系人', syncAccountContacts),
    queueAccountSyncNow: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:sync`, '同步账号数据', queueAccountSyncNow),
    startDirectMessageToContact,
    openGroupDetail: (group) => runWithLoading(`group:${group.id}:detail`, '读取群详情', () => openGroupDetail(group)),
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes: (silent = false) => silent ? pollVerificationCodes(true) : runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:codes`, '同步验证码', () => pollVerificationCodes(false)),
    createDirectMessageTask: () => runWithLoading('direct-message:create', '创建私发任务', createDirectMessageTask),
    createMessageSendTask: (payload) => runWithLoading('message-send:create', '创建消息发送任务', () => createMessageSendTask(payload)),
    saveAccountProfile: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:profile:save`, '保存账号资料', saveAccountProfile),
    retryAccountProfileSync: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:profile-sync`, '重试资料同步', retryAccountProfileSync),
    login: () => runWithLoading('auth:login', '登录', login),
    register: () => runWithLoading('auth:register', '注册', register),
    changePassword: () => runWithLoading('modal:password:change', '修改密码', changePassword),
    logout,
    runLogin,
    verifyAccount,
    chooseAccountLoginMethod: (method) => runWithLoading(`account-login:${accountLoginForm.account?.id ?? 'current'}:${method}`, method === 'qr' ? '启动扫码登录' : '启动登录', () => chooseAccountLoginMethod(method)),
    submitAccountLoginCode: () => runWithLoading(`account-login:${accountLoginForm.account?.id ?? 'current'}:code`, '验证登录', submitAccountLoginCode),
    submitAccountLoginPassword: () => runWithLoading(`account-login:${accountLoginForm.account?.id ?? 'current'}:password`, '验证二步密码', submitAccountLoginPassword),
    resendAccountLoginCode: () => runWithLoading(`account-login:${accountLoginForm.account?.id ?? 'current'}:resend`, '重新发送验证码', resendAccountLoginCode),
    checkAccountQrLogin: () => runWithLoading(`account-login:${accountLoginForm.account?.id ?? 'current'}:qr-check`, '检查扫码结果', checkAccountQrLogin),
    deleteAccount: (account) => runWithLoading(`account:${account.id}:delete`, '移除账号', () => deleteAccount(account)),
    healthCheck: (account) => runWithLoading(`account:${account.id}:health`, '健康检查', () => healthCheck(account)),
    syncAccountGroups: (account) => runWithLoading(`account:${account.id}:sync`, '同步账号数据', () => syncAccountGroups(account)),
    cancelTask: (task) => runWithLoading(`task:${task.id}:cancel`, '取消任务', () => cancelTask(task)),
    dispatchTask: (task) => runWithLoading(`task:${task.id}:dispatch`, '派发消息', () => dispatchTask(task)),
    drainQueue: () => runWithLoading('worker:drain', '处理到期发送', drainQueue),
    retryTask: (task) => runWithLoading(`task:${task.id}:retry`, '重试任务', () => retryTask(task)),
    authorizeSelectedGroup: (status) => runWithLoading(`group:${selectedGroup?.id ?? 'current'}:authorize:${status}`, '更新授权', () => authorizeSelectedGroup(status)),
    createArchive: () => runWithLoading(`group:${selectedGroup?.id ?? 'current'}:archive:create`, '创建归档', createArchive),
    saveGroupPolicy: () => runWithLoading(`group:${selectedGroup?.id ?? 'current'}:policy:save`, '保存群配置', saveGroupPolicy),
    openArchiveDetail: (archive) => runWithLoading(`archive:${archive.id}:detail`, '读取归档', () => openArchiveDetail(archive)),
    exportArchive: (archive) => runWithLoading(`archive:${archive.id}:export`, '导出归档', () => exportArchive(archive)),
    rerunArchive: (archive) => runWithLoading(`archive:${archive.id}:rerun`, '重跑归档', () => rerunArchive(archive)),
    createDeveloperApp: () => runWithLoading('developer-app:save', developerAppForm.id !== null ? '保存开发者应用' : '新增开发者应用', createDeveloperApp),
    openDeveloperAppEdit,
    toggleDeveloperApp: (app) => runWithLoading(`developer-app:${app.id}:toggle`, app.is_active ? '禁用开发者应用' : '启用开发者应用', () => toggleDeveloperApp(app)),
    checkDeveloperApp: (app) => runWithLoading(`developer-app:${app.id}:check`, '检查开发者应用', () => checkDeveloperApp(app)),
    openTenantEdit,
    saveTenantQuota: () => runWithLoading(`tenant:${tenantForm.id ?? 'current'}:save`, '保存租户配额', saveTenantQuota),
    openAdminUserCreate,
    openAdminUserEdit,
    saveAdminUser: () => runWithLoading(`admin-user:${adminUserForm.id ?? 'current'}:save`, '保存用户', saveAdminUser),
    resetAdminUserPassword: (user, newPassword) => runWithLoading(`admin-user:${user.id}:reset-password`, '重置密码', () => resetAdminUserPassword(user, newPassword)),
    adjustAdminUserTokens: (user) => runWithLoading(`admin-user:${user.id}:adjust-tokens`, '调整 Token', () => adjustAdminUserTokens(user)),
    loadUserTokenLedgers,
    createAiProvider: () => runWithLoading('ai-provider:save', aiProviderForm.id !== null ? '保存 AI 供应商' : '新增 AI 供应商', createAiProvider),
    openAiProviderEdit,
    toggleAiProvider: (provider) => runWithLoading(`ai-provider:${provider.id}:toggle`, provider.is_active ? '禁用 AI 供应商' : '启用 AI 供应商', () => toggleAiProvider(provider)),
    checkAiProvider: (provider) => runWithLoading(`ai-provider:${provider.id}:check`, '检查 AI 供应商', () => checkAiProvider(provider)),
    saveTenantAiSetting: () => runWithLoading('tenant-ai:save', '保存 AI 配置', saveTenantAiSetting),
    createPromptTemplate: () => runWithLoading('prompt-template:create', '新增提示词', createPromptTemplate),
    openPromptTemplateEdit,
    savePromptTemplate: () => runWithLoading(`prompt-template:${promptTemplateForm.id ?? 'create'}:save`, promptTemplateForm.id ? '保存提示词' : '新增提示词', savePromptTemplate),
    createMaterial: () => runWithLoading('material:create', '新增素材', createMaterial),
    openMaterialEdit,
    saveMaterial: () => runWithLoading(`material:${materialForm.id ?? 'create'}:save`, materialForm.id ? '保存素材' : '新增素材', saveMaterial),
    createContentKeywordRule: () => runWithLoading('keyword-rule:create', '新增关键词', createContentKeywordRule),
    openContentKeywordRuleEdit,
    saveContentKeywordRule: () => runWithLoading(`keyword-rule:${keywordRuleForm.id ?? 'create'}:save`, keywordRuleForm.id ? '保存关键词' : '新增关键词', saveContentKeywordRule),
    accountName,
    groupName,
    choosePoolSendAccount,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
