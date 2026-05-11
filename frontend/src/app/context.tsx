import React, { createContext, useContext, useState, useMemo, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { App as AntdApp } from 'antd';
import { API_BASE, API_ORIGIN, api, ApiError } from '../shared/api/client';
import { operationLabel } from './components/shared';
import type {
  Overview,
  RuntimeConfig,
  CurrentUser,
  Tenant,
  AdminUser,
  AdminUserForm,
  CaptchaChallenge,
  CaptchaVerifyResponse,
  ActivationCode,
  ActivationCodeFilters,
  ActivationCodePage,
  SubscriptionPlan,
  SubscriptionPlanForm,
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
  SchedulingSetting,
  Material,
  ContentKeywordRule,
  Contact,
  Group,
  Campaign,
  Draft,
  MessageTask,
  ArchiveItem,
  ArchiveDetail,
  ArchiveExport,
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
  CampaignDetail,
  RecommendedAccount,
  ConfirmPayload,
  MessageSendBatchCreate,
  MessageSendTaskCreate,
  ModalState,
  AccountLoginForm,
} from './types';
import { VIEW_ROUTES, viewFromPath } from './routes';
import type { AppState } from './context/types';

const AppContext = createContext<AppState | null>(null);
const DEFAULT_ACTIVATION_CODE_FILTERS: ActivationCodeFilters = { search: '', status: '', plan_type: '', batch_no: '', start_at: '', end_at: '' };
const DEFAULT_ACTIVATION_CODE_PAGE: ActivationCodePage = { items: [], total: 0, page: 1, page_size: 20 };
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
  const [subscriptionPlans, setSubscriptionPlans] = useState<SubscriptionPlan[]>([]);
  const [subscriptionPlanForm, setSubscriptionPlanForm] = useState<SubscriptionPlanForm>({
    id: null,
    plan_type: 'monthly',
    name: '月卡',
    duration_days: 30,
    token_quota: 500000,
    is_active: true,
    note: '',
  });
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [selectedAdminUserId, setSelectedAdminUserId] = useState<number | null>(null);
  const [selectedUserTokenLedgers, setSelectedUserTokenLedgers] = useState<TokenLedger[]>([]);
  const [adminUserForm, setAdminUserForm] = useState<AdminUserForm>({
    id: null,
    name: '',
    email: '',
    phone: '',
    role: '普通用户',
    subscription_status: 'pending_activation',
    menu_permissions: ['overview', 'accounts', 'taskManagement', 'groupManagement', 'usageReports'],
    is_active: true,
  });
  const [tokenAdjustmentForm, setTokenAdjustmentForm] = useState({ delta_tokens: 500000, reason: '管理员充值' });
  const [activationCodes, setActivationCodes] = useState<ActivationCode[]>([]);
  const [activationCodePage, setActivationCodePage] = useState<ActivationCodePage>(DEFAULT_ACTIVATION_CODE_PAGE);
  const [activationCodeFilters, setActivationCodeFilters] = useState<ActivationCodeFilters>(DEFAULT_ACTIVATION_CODE_FILTERS);
  const [usageLedgers, setUsageLedgers] = useState<UsageLedger[]>([]);
  const [usageSummary, setUsageSummary] = useState<UsageSummary | null>(null);
  const [redeemCode, setRedeemCode] = useState('');
  const [activationBatch, setActivationBatch] = useState({ plan_type: 'monthly', plan_id: '' as number | '', quantity: 10, batch_no: '', serial_prefix: '', note: '' });
  const [aiProviders, setAiProviders] = useState<AiProvider[]>([]);
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [tenantAiSetting, setTenantAiSetting] = useState<TenantAiSetting | null>(null);
  const [schedulingSetting, setSchedulingSetting] = useState<SchedulingSetting | null>(null);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [contentKeywordRules, setContentKeywordRules] = useState<ContentKeywordRule[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [tasks, setTasks] = useState<MessageTask[]>([]);
  const [selectedCampaignId, setSelectedCampaignId] = useState<number | null>(null);
  const [taskManagementTab, setTaskManagementTab] = useState('任务列表');
  const [archives, setArchives] = useState<ArchiveItem[]>([]);
  const [archiveDetail, setArchiveDetail] = useState<ArchiveDetail | null>(null);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [auditFilters, setAuditFilters] = useState({ actor: '', action: '', target_type: '', start_at: '', end_at: '' });
  const [accountDetail, setAccountDetail] = useState<AccountDetail | null>(null);
  const [accountDetailTab, setAccountDetailTab] = useState('资料');
  const [accountPoolDetail, setAccountPoolDetail] = useState<AccountPoolDetail | null>(null);
  const [poolDirectAccountId, setPoolDirectAccountId] = useState<number | ''>('');
  const [returnAfterVerification, setReturnAfterVerification] = useState<'accountDetail' | 'accountPoolDetail'>('accountDetail');
  const [groupDetail, setGroupDetail] = useState<GroupDetail | null>(null);
  const [campaignDetail, setCampaignDetail] = useState<CampaignDetail | null>(null);
  const [draftEditTarget, setDraftEditTarget] = useState<Draft | null>(null);
  const [draftEditForm, setDraftEditForm] = useState({ content: '', risk_level: '低', suggested_account_id: '' as number | '' });
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
  const [campaignStep, setCampaignStep] = useState(1);
  const [campaignMode, setCampaignMode] = useState('ai_activity');
  const [selectedTargetGroupIds, setSelectedTargetGroupIds] = useState<number[]>([]);
  const [selectedSourceGroupIds, setSelectedSourceGroupIds] = useState<number[]>([]);
  const [recommendedAccounts, setRecommendedAccounts] = useState<RecommendedAccount[]>([]);
  const [selectedAccountsByGroup, setSelectedAccountsByGroup] = useState<Record<string, number[]>>({});
  const [topic, setTopic] = useState('围绕新品体验做一轮自然讨论');
  const [sendWindow, setSendWindow] = useState('10:00-22:00');
  const [intensity, setIntensity] = useState('轻度');
  const [draftCount, setDraftCount] = useState(4);
  const [tone, setTone] = useState('自然、像真实群成员聊天');
  const [selectedAiProviderId, setSelectedAiProviderId] = useState<number | ''>('');
  const [selectedMaterialIds, setSelectedMaterialIds] = useState<number[]>([]);
  const [jitterMinSeconds, setJitterMinSeconds] = useState(15);
  const [jitterMaxSeconds, setJitterMaxSeconds] = useState(180);
  const [batchIntervalSeconds, setBatchIntervalSeconds] = useState(45);
  const [respectSendWindow, setRespectSendWindow] = useState(true);
  const [campaignEndsAt, setCampaignEndsAt] = useState(() => {
    const value = new Date(Date.now() + 2 * 60 * 60 * 1000);
    return value.toISOString().slice(0, 16);
  });
  const [maxAiTokens, setMaxAiTokens] = useState(100000);
  const [runIntervalSeconds, setRunIntervalSeconds] = useState(300);
  const [participationMinRatio, setParticipationMinRatio] = useState(0.6);
  const [participationMaxRatio, setParticipationMaxRatio] = useState(1);
  const [maxMessagesPerAccount, setMaxMessagesPerAccount] = useState(2);
  const [maxDraftsPerBatch, setMaxDraftsPerBatch] = useState(50);
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [groupPolicy, setGroupPolicy] = useState({
    active_window: '09:00-23:00',
    daily_limit: 120,
    account_cooldown_seconds: 180,
    group_cooldown_seconds: 60,
    topic_direction: '',
    banned_words: '',
    link_whitelist: '',
    require_review: true,
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
    name: '客户群活跃模板',
    template_type: '群活跃草稿',
    content: '请为 {{group_title}} 围绕 {{topic}} 生成 {{count}} 条自然 Telegram 群聊草稿，语气 {{tone}}，素材 {{materials}}，输出 JSON drafts。',
  });
  const [materialForm, setMaterialForm] = useState({
    title: '活动表情包',
    material_type: '表情包',
    content: 'https://example.local/stickers/welcome.webp',
    tags: '表情包,欢迎',
  });
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
  const selectedCampaign = useMemo(() => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? campaigns[0] ?? null, [campaigns, selectedCampaignId]);
  const choosePoolSendAccount = (detail: AccountPoolDetail) => (
    detail.accounts.find((account) => account.status === '在线' && detail.contacts.some((contact) => contact.account_id === account.id))
    ?? detail.accounts.find((account) => account.status === '在线')
    ?? detail.accounts[0]
  );
  const selectedCampaignDrafts = useMemo(
    () => selectedCampaign ? drafts.filter((draft) => draft.campaign_id === selectedCampaign.id).sort((left, right) => (left.sequence_index || 0) - (right.sequence_index || 0) || left.id - right.id) : [],
    [drafts, selectedCampaign],
  );
  const selectedCampaignTasks = useMemo(
    () => selectedCampaign ? tasks.filter((task) => task.campaign_id === selectedCampaign.id).sort((left, right) => new Date(left.scheduled_at).getTime() - new Date(right.scheduled_at).getTime() || left.id - right.id) : [],
    [tasks, selectedCampaign],
  );
  const targetGroupsMissingAccounts = useMemo(
    () => selectedTargetGroupIds.filter((groupId) => !(selectedAccountsByGroup[String(groupId)] ?? []).length),
    [selectedTargetGroupIds, selectedAccountsByGroup],
  );
  const taskSummary = useMemo(() => ({
    campaigns: campaigns.length,
    pendingDrafts: drafts.filter((draft) => draft.status === '待审核').length,
    queued: tasks.filter((task) => task.status === '排队中').length,
    sending: tasks.filter((task) => task.status === '发送中').length,
    sent: tasks.filter((task) => task.status === '已发送').length,
    failed: tasks.filter((task) => task.status === '失败').length,
  }), [campaigns, drafts, tasks]);

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
    if (!campaigns.length) {
      setSelectedCampaignId(null);
      return;
    }
    if (!selectedCampaignId || !campaigns.some((campaign) => campaign.id === selectedCampaignId)) {
      setSelectedCampaignId(campaigns[0].id);
    }
  }, [campaigns, selectedCampaignId]);

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

  function activationCodeQuery(filters = activationCodeFilters, page = activationCodePage.page, pageSize = activationCodePage.page_size) {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
    return `/admin/activation-codes?${params.toString()}`;
  }

  async function loadActivationCodes(filters = activationCodeFilters, page = activationCodePage.page, pageSize = activationCodePage.page_size) {
    const data = await api<ActivationCodePage>(activationCodeQuery(filters, page, pageSize));
    setActivationCodeFilters(filters);
    setActivationCodePage(data);
    setActivationCodes(data.items);
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
        api<Draft[]>('/ai-drafts'),
        api<MessageTask[]>(`/message-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`),
        api<ArchiveItem[]>('/archives'),
        api<AuditLog[]>(auditQuery()),
        api<AiProvider[]>('/ai-providers'),
        api<PromptTemplate[]>('/prompt-templates'),
        api<TenantAiSetting>('/tenant-ai-settings'),
        api<SchedulingSetting>('/scheduling-settings'),
        api<Material[]>('/materials'),
        api<ContentKeywordRule[]>('/content-keyword-rules'),
      ]);
      const runtimeData = settledValue(results[0], {} as RuntimeConfig);
      const overviewData = settledValue(results[1], {} as Overview);
      const poolData = settledValue(results[2], [] as AccountPool[]);
      const accountData = settledValue(results[3], [] as Account[]);
      const groupData = settledValue(results[4], [] as Group[]);
      const campaignData: Campaign[] = [];
      const draftData = settledValue(results[5], [] as Draft[]);
      const taskData = settledValue(results[6], [] as MessageTask[]);
      const archiveData = settledValue(results[7], [] as ArchiveItem[]);
      const auditData = settledValue(results[8], [] as AuditLog[]);
      const aiProviderData = settledValue(results[9], [] as AiProvider[]);
      const promptTemplateData = settledValue(results[10], [] as PromptTemplate[]);
      const tenantAiData = settledValue(results[11], {} as TenantAiSetting);
      const schedulingData = settledValue(results[12], {} as SchedulingSetting);
      const materialData = settledValue(results[13], [] as Material[]);
      const keywordRuleData = settledValue(results[14], [] as ContentKeywordRule[]);
      const developerAppData = me.role === '系统管理员' ? await api<DeveloperApp[]>('/developer-apps').catch(() => [] as DeveloperApp[]) : [];
      const tenantData = me.role === '系统管理员' ? await api<Tenant[]>('/tenants').catch(() => [] as Tenant[]) : [];
      const subscriptionPlanData: SubscriptionPlan[] = [];
      const adminUserData: AdminUser[] = [];
      const activationCodeData = DEFAULT_ACTIVATION_CODE_PAGE;
      const usageLedgerData: UsageLedger[] = [];
      const usageSummaryData: UsageSummary | null = null;
      setRuntime(runtimeData);
      setOverview(overviewData);
      setAccountPools(poolData);
      setAccounts(accountData);
      setDeveloperApps(developerAppData);
      setTenants(tenantData);
      setSubscriptionPlans(subscriptionPlanData);
      setAdminUsers(adminUserData);
      setSelectedAdminUserId((current) => current ?? adminUserData[0]?.id ?? null);
      setActivationCodes(activationCodeData.items);
      setActivationCodePage(activationCodeData);
      setUsageLedgers(usageLedgerData);
      setUsageSummary(usageSummaryData);
      setAiProviders(aiProviderData);
      setPromptTemplates(promptTemplateData);
      setTenantAiSetting(tenantAiData);
      setSchedulingSetting(schedulingData);
      setMaterials(materialData);
      setContentKeywordRules(keywordRuleData);
      setGroups(groupData);
      setCampaigns(campaignData);
      setDrafts(draftData);
      setTasks(taskData);
      setArchives(archiveData);
      setAudits(auditData);
      setSelectedGroupId((current) => current ?? groupData[0]?.id ?? null);
      setSelectedAiProviderId((current) => current || tenantAiData.default_provider_id || aiProviderData[0]?.id || '');
      setJitterMinSeconds((current) => current || schedulingData.jitter_min_seconds);
      setJitterMaxSeconds((current) => current || schedulingData.jitter_max_seconds);
      setBatchIntervalSeconds((current) => current || schedulingData.batch_interval_seconds);
      setRespectSendWindow(schedulingData.respect_send_window);
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

  function openCampaignModal(groupId?: number) {
    const targetIds = groupId ? [groupId] : selectedTargetGroupIds.length ? selectedTargetGroupIds : [selectedGroupId ?? groups[0]?.id].filter(Boolean) as number[];
    if (groupId) setSelectedGroupId(groupId);
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
    setCampaignMode('ai_activity');
    setSelectedTargetGroupIds(targetIds);
    setSelectedSourceGroupIds([]);
    setRecommendedAccounts([]);
    setSelectedAccountsByGroup({});
    setCampaignStep(1);
    setModal(null);
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
    showResult('验证辅助已处理', `${updated.verification_type}：${updated.status}`);
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

  async function loadCampaignDetail(campaign: Campaign) {
    setSelectedCampaignId(campaign.id);
    setCampaignDetail(null);
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
  }

  function openDraftEdit(draft: Draft) {
    setDraftEditTarget(draft);
    setDraftEditForm({
      content: draft.content,
      risk_level: draft.risk_level,
      suggested_account_id: draft.suggested_account_id ?? '',
    });
    setModal({ type: 'draftEdit' });
  }

  async function saveDraftEdit() {
    if (!draftEditTarget) return;
    setBusy('保存草稿');
    const updated = await api<Draft>(`/ai-drafts/${draftEditTarget.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        content: draftEditForm.content,
        risk_level: draftEditForm.risk_level,
        suggested_account_id: draftEditForm.suggested_account_id || null,
      }),
    });
    setDrafts((current) => current.map((draft) => draft.id === updated.id ? updated : draft));
    closeModal();
    showResult('草稿已更新', '草稿内容、风险等级和建议账号已保存。');
    await refresh();
    if (selectedCampaign) await loadCampaignDetail(selectedCampaign);
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

  function toggleTargetGroup(groupId: number) {
    setSelectedTargetGroupIds((current) => {
      const next = current.includes(groupId) ? current.filter((id) => id !== groupId) : [...current, groupId];
      setRecommendedAccounts([]);
      setSelectedAccountsByGroup({});
      return next;
    });
  }

  function toggleSourceGroup(groupId: number) {
    setSelectedSourceGroupIds((current) => current.includes(groupId) ? current.filter((id) => id !== groupId) : [...current, groupId]);
  }

  async function recommendAccounts(groupIds = selectedTargetGroupIds) {
    if (!groupIds.length) {
      setRecommendedAccounts([]);
      setSelectedAccountsByGroup({});
      return;
    }
    const recommendations = await api<RecommendedAccount[]>('/tasks/recommend-accounts', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: currentUser?.tenant_id ?? 1, target_group_ids: groupIds }),
    });
    setRecommendedAccounts(recommendations);
    const selected: Record<string, number[]> = {};
    for (const item of recommendations) {
      if (!selected[String(item.group_id)]) selected[String(item.group_id)] = [];
      if (item.recommended && (item.is_selectable ?? item.can_send)) selected[String(item.group_id)].push(item.account_id);
    }
    setSelectedAccountsByGroup(selected);
  }

  function toggleRecommendedAccount(groupId: number, accountId: number) {
    setSelectedAccountsByGroup((current) => {
      const key = String(groupId);
      const existing = current[key] ?? [];
      return {
        ...current,
        [key]: existing.includes(accountId) ? existing.filter((id) => id !== accountId) : [...existing, accountId],
      };
    });
  }

  function setGroupAccountsSelected(groupId: number, accountIds: number[]) {
    setSelectedAccountsByGroup((current) => ({ ...current, [String(groupId)]: accountIds }));
  }

  async function goCampaignAccountStep() {
    if (!selectedTargetGroupIds.length) return;
    setBusy('推荐账号');
    try {
      await recommendAccounts(selectedTargetGroupIds);
      setCampaignStep(3);
    } catch (error) {
      setNotice(`推荐账号失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setBusy('');
    }
  }

  function goCampaignContentStep() {
    if (targetGroupsMissingAccounts.length) return;
    setCampaignStep(4);
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
      setNotice('注册成功，请先使用卡密激活订阅。');
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

  async function submitRedeemCode() {
    if (!redeemCode.trim()) return;
    setBusy('兑换卡密');
    const redeemed = await api<{ plan_name: string; token_quota: number; token_balance: number }>('/subscription/redeem', { method: 'POST', body: JSON.stringify({ code: redeemCode.trim() }) });
    setRedeemCode('');
    setNotice(`卡密兑换成功：${redeemed.plan_name}，赠送 ${redeemed.token_quota.toLocaleString()} Token，当前余额 ${redeemed.token_balance.toLocaleString()}。`);
    await refresh();
    setBusy('');
  }

  async function createActivationCodes() {
    if (!activationBatch.batch_no.trim() || !activationBatch.serial_prefix.trim()) {
      setNotice('请填写批次号和序列号前缀。');
      throw new Error('activation code batch and serial prefix required');
    }
    setBusy('生成卡密');
    try {
      const payload = {
        ...activationBatch,
        plan_id: activationBatch.plan_id || null,
        batch_no: activationBatch.batch_no.trim().toUpperCase(),
        serial_prefix: activationBatch.serial_prefix.trim().toUpperCase(),
      };
      const created = await api<ActivationCode[]>('/admin/activation-codes', { method: 'POST', body: JSON.stringify(payload) });
      const preview = created.map((item) => item.code).join('\n');
      showResult('卡密已生成', preview || '没有生成新的卡密。');
      setNotice(`已生成 ${created.length} 个卡密。`);
      await loadActivationCodes(activationCodeFilters, 1, activationCodePage.page_size).catch(() => undefined);
    } catch (error) {
      handleActionError(error);
      throw error;
    } finally {
      setBusy('');
    }
  }

  async function disableActivationCode(code: ActivationCode) {
    setBusy('停用卡密');
    try {
      await api<ActivationCode>(`/admin/activation-codes/${code.id}/disable`, { method: 'POST' });
      setNotice(`已停用卡密 ${code.code}`);
      await loadActivationCodes();
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

  async function createCampaignAndDrafts() {
    closeModal();
    showResult('请使用新任务中心', '旧任务创建入口已下线，请在任务中心按 5 类型分步创建任务。');
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
  }

  async function approveDraft(draft: Draft) {
    setBusy('审核草稿');
    await api(`/ai-drafts/${draft.id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    showResult('草稿已通过', '已生成排队消息任务。');
    setTaskManagementTab('发送进度');
    await refresh();
    setBusy('');
  }

  async function cancelCampaign(campaign: Campaign) {
    setSelectedCampaignId(campaign.id);
    showResult('旧任务入口已下线', '请在新任务中心管理当前 5 类型任务。');
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
  }

  async function rejectDraft(draft: Draft) {
    setBusy('驳回草稿');
    const updated = await api<Draft>(`/ai-drafts/${draft.id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    setDrafts((current) => current.map((item) => item.id === updated.id ? updated : item));
    showResult('草稿已驳回', '这条草稿不会生成消息任务，可重新生成或编辑其他草稿。');
    await refresh();
    setBusy('');
  }

  async function approveAllDrafts() {
    showResult('旧批量审核入口已下线', '新任务中心的审核请在审核队列中处理。');
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
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
    showResult('运营配置已保存', `${selectedGroup.title} 的限频、审核和内容规则已更新。`);
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

  async function createSubscriptionPlan() {
    setBusy(subscriptionPlanForm.id ? '保存套餐' : '新增套餐');
    try {
      const payload = {
        plan_type: subscriptionPlanForm.plan_type,
        name: subscriptionPlanForm.name,
        duration_days: subscriptionPlanForm.duration_days,
        token_quota: subscriptionPlanForm.token_quota,
        is_active: subscriptionPlanForm.is_active,
        note: subscriptionPlanForm.note,
      };
      const saved = subscriptionPlanForm.id
        ? await api<SubscriptionPlan>(`/admin/subscription-plans/${subscriptionPlanForm.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api<SubscriptionPlan>('/admin/subscription-plans', { method: 'POST', body: JSON.stringify(payload) });
      closeModal();
      setNotice(`套餐已保存：${saved.name}`);
      setSubscriptionPlanForm({ id: null, plan_type: 'monthly', name: '月卡', duration_days: 30, token_quota: 500000, is_active: true, note: '' });
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  function openSubscriptionPlanEdit(plan: SubscriptionPlan) {
    setSubscriptionPlanForm({
      id: plan.id,
      plan_type: plan.plan_type,
      name: plan.name,
      duration_days: plan.duration_days,
      token_quota: plan.token_quota,
      is_active: plan.is_active,
      note: plan.note,
    });
    setModal({ type: 'subscriptionPlanEdit' });
  }

  async function saveSubscriptionPlan() {
    await createSubscriptionPlan();
  }

  function openAdminUserEdit(user: AdminUser) {
    setAdminUserForm({
      id: user.id,
      name: user.name,
      email: user.email,
      phone: user.phone ?? '',
      role: user.role,
      subscription_status: user.subscription_status,
      menu_permissions: user.menu_permissions.includes('*') ? ['overview', 'accounts', 'taskManagement', 'groupManagement', 'usageReports'] : user.menu_permissions,
      is_active: user.is_active,
    });
    setSelectedAdminUserId(user.id);
    void loadUserTokenLedgers(user.id);
    setModal({ type: 'adminUserEdit' });
  }

  async function saveAdminUser() {
    if (!adminUserForm.id) return;
    setBusy('保存用户');
    try {
      const saved = await api<AdminUser>(`/admin/users/${adminUserForm.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: adminUserForm.name,
          email: adminUserForm.email,
          phone: adminUserForm.phone || null,
          role: adminUserForm.role,
          subscription_status: adminUserForm.subscription_status,
          menu_permissions: adminUserForm.menu_permissions,
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
      showResult('AI 配置已保存', '客户默认模型、温度、Token 和回退策略已更新。');
      await refresh();
    } catch (error) {
      handleActionError(error);
    } finally {
      setBusy('');
    }
  }

  async function saveSchedulingSetting() {
    setBusy('保存发送节奏');
    await api('/scheduling-settings', {
      method: 'PATCH',
      body: JSON.stringify({
        jitter_min_seconds: jitterMinSeconds,
        jitter_max_seconds: jitterMaxSeconds,
        batch_interval_seconds: batchIntervalSeconds,
        respect_send_window: respectSendWindow,
      }),
    });
    closeModal();
    showResult('发送节奏已保存', '默认抖动、批次间隔和时间窗策略已更新。');
    await refresh();
  }

  async function createPromptTemplate() {
    setBusy('新增提示词');
    const template = await api<PromptTemplate>('/prompt-templates', {
      method: 'POST',
      body: JSON.stringify({ ...promptTemplateForm, tenant_id: currentUser?.tenant_id ?? 1 }),
    });
    closeModal();
    showResult('提示词已新增', `已新增提示词模板：${template.name}`);
    setPromptTemplateForm({ ...promptTemplateForm, name: '客户群活跃模板' });
    await refresh();
  }

  async function createMaterial() {
    setBusy('新增素材');
    const material = await api<Material>('/materials', {
      method: 'POST',
      body: JSON.stringify({ ...materialForm, tenant_id: currentUser?.tenant_id ?? 1 }),
    });
    closeModal();
    showResult('素材已新增', `已新增素材：${material.title}`);
    setSelectedMaterialIds((current) => [...current, material.id]);
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

  function toggleMaterial(materialId: number) {
    setSelectedMaterialIds((current) => current.includes(materialId) ? current.filter((id) => id !== materialId) : [...current, materialId]);
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
  const value: AppState = useMemo(() => ({
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
    subscriptionPlans,
    setSubscriptionPlans,
    subscriptionPlanForm,
    setSubscriptionPlanForm,
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

    // Activation & Usage
    activationCodes,
    setActivationCodes,
    activationCodePage,
    setActivationCodePage,
    activationCodeFilters,
    setActivationCodeFilters,
    usageLedgers,
    setUsageLedgers,
    usageSummary,
    setUsageSummary,
    redeemCode,
    setRedeemCode,
    activationBatch,
    setActivationBatch,

    // AI & Templates
    aiProviders,
    setAiProviders,
    promptTemplates,
    setPromptTemplates,
    tenantAiSetting,
    setTenantAiSetting,
    schedulingSetting,
    setSchedulingSetting,
    materials,
    setMaterials,
    contentKeywordRules,
    setContentKeywordRules,

    // Groups & Campaigns
    groups,
    setGroups,
    campaigns,
    setCampaigns,
    drafts,
    setDrafts,
    tasks,
    setTasks,
    selectedCampaignId,
    setSelectedCampaignId,
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
    campaignDetail,
    setCampaignDetail,

    // Draft edit
    draftEditTarget,
    setDraftEditTarget,
    draftEditForm,
    setDraftEditForm,

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

    // Group & Campaign state
    selectedGroupId,
    setSelectedGroupId,
    campaignStep,
    setCampaignStep,
    campaignMode,
    setCampaignMode,
    selectedTargetGroupIds,
    setSelectedTargetGroupIds,
    selectedSourceGroupIds,
    setSelectedSourceGroupIds,
    recommendedAccounts,
    setRecommendedAccounts,
    selectedAccountsByGroup,
    setSelectedAccountsByGroup,
    topic,
    setTopic,
    sendWindow,
    setSendWindow,
    intensity,
    setIntensity,
    draftCount,
    setDraftCount,
    tone,
    setTone,
    selectedAiProviderId,
    setSelectedAiProviderId,
    selectedMaterialIds,
    setSelectedMaterialIds,
    jitterMinSeconds,
    setJitterMinSeconds,
    jitterMaxSeconds,
    setJitterMaxSeconds,
    batchIntervalSeconds,
    setBatchIntervalSeconds,
    respectSendWindow,
    setRespectSendWindow,
    campaignEndsAt,
    setCampaignEndsAt,
    maxAiTokens,
    setMaxAiTokens,
    runIntervalSeconds,
    setRunIntervalSeconds,
    participationMinRatio,
    setParticipationMinRatio,
    participationMaxRatio,
    setParticipationMaxRatio,
    maxMessagesPerAccount,
    setMaxMessagesPerAccount,
    maxDraftsPerBatch,
    setMaxDraftsPerBatch,
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
    selectedCampaign,
    selectedCampaignDrafts,
    selectedCampaignTasks,
    targetGroupsMissingAccounts,
    taskSummary,

    // Handler functions
    refresh: () => runWithLoading('app:refresh', '刷新数据', refresh),
    showResult,
    closeModal,
    openConfirm,
    openCampaignModal,
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
    dismissVerificationTask: (task) => runWithLoading(`verification:${task.id}:dismiss`, '忽略验证辅助', () => dismissVerificationTask(task)),
    refreshAccountDetail: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:detail-refresh`, '刷新账号详情', refreshAccountDetail),
    syncAccountContacts: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:contacts`, '同步联系人', syncAccountContacts),
    queueAccountSyncNow: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:sync`, '同步账号数据', queueAccountSyncNow),
    startDirectMessageToContact,
    openGroupDetail: (group) => runWithLoading(`group:${group.id}:detail`, '读取群详情', () => openGroupDetail(group)),
    loadCampaignDetail: (campaign) => runWithLoading(`campaign:${campaign.id}:detail`, '读取任务详情', () => loadCampaignDetail(campaign)),
    openDraftEdit,
    saveDraftEdit: () => runWithLoading(`draft:${draftEditTarget?.id ?? 'current'}:save`, '保存草稿', saveDraftEdit),
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes: (silent = false) => silent ? pollVerificationCodes(true) : runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:codes`, '同步验证码', () => pollVerificationCodes(false)),
    toggleTargetGroup,
    toggleSourceGroup,
    recommendAccounts,
    toggleRecommendedAccount,
    setGroupAccountsSelected,
    goCampaignAccountStep: () => runWithLoading('campaign:recommend', '推荐账号', goCampaignAccountStep),
    goCampaignContentStep,
    createDirectMessageTask: () => runWithLoading('direct-message:create', '创建私发任务', createDirectMessageTask),
    createMessageSendTask: (payload) => runWithLoading('message-send:create', '创建消息发送任务', () => createMessageSendTask(payload)),
    saveAccountProfile: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:profile:save`, '保存账号资料', saveAccountProfile),
    retryAccountProfileSync: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:profile-sync`, '重试资料同步', retryAccountProfileSync),
    login: () => runWithLoading('auth:login', '登录', login),
    register: () => runWithLoading('auth:register', '注册', register),
    changePassword: () => runWithLoading('modal:password:change', '修改密码', changePassword),
    submitRedeemCode: () => runWithLoading('subscription:redeem', '兑换卡密', submitRedeemCode),
    loadActivationCodes: (filters, page, pageSize) => runWithLoading('activation-codes:load', '读取卡密', () => loadActivationCodes(filters, page, pageSize)),
    createActivationCodes: () => runWithLoading('activation-codes:create', '生成卡密', createActivationCodes),
    disableActivationCode: (code) => runWithLoading(`activation-code:${code.id}:disable`, '停用卡密', () => disableActivationCode(code)),
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
    createCampaignAndDrafts: () => runWithLoading('campaign:create', '创建任务', createCampaignAndDrafts),
    cancelCampaign: (campaign) => runWithLoading(`campaign:${campaign.id}:cancel`, '取消任务', () => cancelCampaign(campaign)),
    approveDraft: (draft) => runWithLoading(`draft:${draft.id}:approve`, '审核草稿', () => approveDraft(draft)),
    rejectDraft: (draft) => runWithLoading(`draft:${draft.id}:reject`, '驳回草稿', () => rejectDraft(draft)),
    approveAllDrafts: () => runWithLoading(`campaign:${selectedCampaign?.id ?? 'current'}:approve-all`, '批量审核', approveAllDrafts),
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
    createSubscriptionPlan: () => runWithLoading('subscription-plan:save', subscriptionPlanForm.id ? '保存套餐' : '新增套餐', createSubscriptionPlan),
    openSubscriptionPlanEdit,
    saveSubscriptionPlan: () => runWithLoading('subscription-plan:save', subscriptionPlanForm.id ? '保存套餐' : '新增套餐', saveSubscriptionPlan),
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
    saveSchedulingSetting: () => runWithLoading('scheduling:save', '保存发送节奏', saveSchedulingSetting),
    createPromptTemplate: () => runWithLoading('prompt-template:create', '新增提示词', createPromptTemplate),
    createMaterial: () => runWithLoading('material:create', '新增素材', createMaterial),
    createContentKeywordRule: () => runWithLoading('keyword-rule:create', '新增关键词', createContentKeywordRule),
    openContentKeywordRuleEdit,
    saveContentKeywordRule: () => runWithLoading(`keyword-rule:${keywordRuleForm.id ?? 'create'}:save`, keywordRuleForm.id ? '保存关键词' : '新增关键词', saveContentKeywordRule),
    toggleMaterial,
    accountName,
    groupName,
    choosePoolSendAccount,
  }), [token, currentUser, authMode, loginEmail, loginPassword, registerForm, changePasswordForm, captchaChallenge, captchaInput, captchaToken, captchaError, captchaLoading, activeView, runtime, overview, accountPools, selectedPoolId, accounts, developerApps, tenants, subscriptionPlans, subscriptionPlanForm, adminUsers, selectedAdminUserId, selectedUserTokenLedgers, adminUserForm, tokenAdjustmentForm, activationCodes, activationCodePage, activationCodeFilters, usageLedgers, usageSummary, redeemCode, activationBatch, aiProviders, promptTemplates, tenantAiSetting, schedulingSetting, materials, contentKeywordRules, groups, campaigns, drafts, tasks, selectedCampaignId, taskManagementTab, archives, archiveDetail, audits, auditFilters, accountDetail, accountContacts, selectedDirectContact, accountDetailTab, accountPoolDetail, poolDirectAccountId, returnAfterVerification, groupDetail, campaignDetail, draftEditTarget, draftEditForm, accountCreateForm, accountPoolForm, cloneForm, loginAfterCreate, accountLoginForm, profileForm, avatarFile, selectedGroupId, campaignStep, campaignMode, selectedTargetGroupIds, selectedSourceGroupIds, recommendedAccounts, selectedAccountsByGroup, topic, sendWindow, intensity, draftCount, tone, selectedAiProviderId, selectedMaterialIds, jitterMinSeconds, jitterMaxSeconds, batchIntervalSeconds, respectSendWindow, campaignEndsAt, maxAiTokens, runIntervalSeconds, participationMinRatio, participationMaxRatio, maxMessagesPerAccount, maxDraftsPerBatch, taskStatusFilter, groupPolicy, developerAppForm, tenantForm, aiProviderForm, promptTemplateForm, materialForm, keywordRuleForm, modal, busy, pendingActionKeys, isActionPending, notice, directMessageForm, selectedPool, selectedGroup, selectedCampaign, selectedCampaignDrafts, selectedCampaignTasks, targetGroupsMissingAccounts, taskSummary, runWithLoading, refresh, showResult, closeModal, openConfirm, openCampaignModal, openAccountCreate, openAccountDetail, openAccountVerificationCodes, openAccountMovePool, openAccountPoolDetail, refreshAccountPoolDetail, createAccount, deleteAccount, createAccountPool, moveCurrentAccountPool, createClonePlan, confirmClonePlan, retryCloneItem, confirmVerificationTask, dismissVerificationTask, refreshAccountDetail, syncAccountContacts, queueAccountSyncNow, startDirectMessageToContact, openGroupDetail, loadCampaignDetail, openDraftEdit, saveDraftEdit, avatarUrl, openAccountProfileEdit, pollVerificationCodes, toggleTargetGroup, toggleSourceGroup, recommendAccounts, toggleRecommendedAccount, setGroupAccountsSelected, goCampaignAccountStep, goCampaignContentStep, createDirectMessageTask, createMessageSendTask, saveAccountProfile, retryAccountProfileSync, login, register, changePassword, submitRedeemCode, loadActivationCodes, createActivationCodes, disableActivationCode, logout, runLogin, verifyAccount, chooseAccountLoginMethod, submitAccountLoginCode, submitAccountLoginPassword, resendAccountLoginCode, checkAccountQrLogin, healthCheck, syncAccountGroups, createCampaignAndDrafts, cancelCampaign, approveDraft, rejectDraft, approveAllDrafts, cancelTask, dispatchTask, drainQueue, retryTask, authorizeSelectedGroup, createArchive, saveGroupPolicy, openArchiveDetail, exportArchive, rerunArchive, createDeveloperApp, openDeveloperAppEdit, toggleDeveloperApp, checkDeveloperApp, openTenantEdit, saveTenantQuota, createSubscriptionPlan, openSubscriptionPlanEdit, saveSubscriptionPlan, openAdminUserEdit, saveAdminUser, resetAdminUserPassword, adjustAdminUserTokens, loadUserTokenLedgers, createAiProvider, openAiProviderEdit, toggleAiProvider, checkAiProvider, createPromptTemplate, saveTenantAiSetting, saveSchedulingSetting, createMaterial, createContentKeywordRule, openContentKeywordRuleEdit, saveContentKeywordRule, toggleMaterial, accountName, groupName, choosePoolSendAccount, refreshCaptchaChallenge, verifyCaptcha, message, modalApi]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
