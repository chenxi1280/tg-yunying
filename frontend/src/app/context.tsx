import React, { createContext, useContext, useState, useMemo, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { API_BASE, API_ORIGIN, api, ApiError } from '../shared/api/client';
import { operationLabel } from './components/shared';
import type {
  Overview,
  RuntimeConfig,
  CurrentUser,
  Tenant,
  CaptchaChallenge,
  CaptchaVerifyResponse,
  ActivationCode,
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
  ModalState,
  ResultDialogState,
} from './types';
import { VIEW_ROUTES, viewFromPath } from './routes';
import type { AppState } from './context/types';

const AppContext = createContext<AppState | null>(null);

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
  const location = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState(localStorage.getItem('tg_ops_token') ?? '');
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [loginEmail, setLoginEmail] = useState('admin@demo.local');
  const [loginPassword, setLoginPassword] = useState('admin123');
  const [registerForm, setRegisterForm] = useState({ name: '', email: '', phone: '', password: '' });
  const [activeView, setActiveView] = useState(() => viewFromPath(location.pathname));
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [accountPools, setAccountPools] = useState<AccountPool[]>([]);
  const [selectedPoolId, setSelectedPoolId] = useState<number | ''>('');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [developerApps, setDeveloperApps] = useState<DeveloperApp[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [activationCodes, setActivationCodes] = useState<ActivationCode[]>([]);
  const [usageLedgers, setUsageLedgers] = useState<UsageLedger[]>([]);
  const [usageSummary, setUsageSummary] = useState<UsageSummary | null>(null);
  const [redeemCode, setRedeemCode] = useState('');
  const [activationBatch, setActivationBatch] = useState({ plan_type: 'monthly', quantity: 10, note: '' });
  const [aiProviders, setAiProviders] = useState<AiProvider[]>([]);
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [tenantAiSetting, setTenantAiSetting] = useState<TenantAiSetting | null>(null);
  const [schedulingSetting, setSchedulingSetting] = useState<SchedulingSetting | null>(null);
  const [materials, setMaterials] = useState<Material[]>([]);
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
  });
  const [accountPoolForm, setAccountPoolForm] = useState({
    name: '新账号池',
    description: '',
    is_default: false,
  });
  const [cloneForm, setCloneForm] = useState({
    target_account_ids: [] as number[],
    clone_contacts: true,
    clone_groups: true,
  });
  const [loginAfterCreate, setLoginAfterCreate] = useState(false);
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
  const [selectedTargetGroupIds, setSelectedTargetGroupIds] = useState<number[]>([]);
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
  });
  const [developerAppForm, setDeveloperAppForm] = useState({
    app_name: '备用开发者应用',
    api_id: '',
    api_hash: '',
    max_accounts: 0,
    notes: '',
  });
  const [tenantForm, setTenantForm] = useState({
    id: null as number | null,
    name: '',
    plan_name: '',
    account_quota: 50,
    task_quota: 5000,
  });
  const [aiProviderForm, setAiProviderForm] = useState({
    provider_name: 'MiMo V2.5 Pro',
    base_url: 'mock://openai-compatible',
    model_name: 'mimo-v2.5-pro',
    api_key: '',
    api_key_header: 'Authorization',
    notes: '',
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
  const [modal, setModal] = useState<ModalState>(null);
  const [resultDialog, setResultDialog] = useState<ResultDialogState>(null);
  const [busy, setBusy] = useState('');
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
        api<Campaign[]>('/campaigns'),
        api<Draft[]>('/ai-drafts'),
        api<MessageTask[]>(`/message-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`),
        api<ArchiveItem[]>('/archives'),
        api<AuditLog[]>(auditQuery()),
        api<AiProvider[]>('/ai-providers'),
        api<PromptTemplate[]>('/prompt-templates'),
        api<TenantAiSetting>('/tenant-ai-settings'),
        api<SchedulingSetting>('/scheduling-settings'),
        api<Material[]>('/materials'),
      ]);
      const runtimeData = settledValue(results[0], {} as RuntimeConfig);
      const overviewData = settledValue(results[1], {} as Overview);
      const poolData = settledValue(results[2], [] as AccountPool[]);
      const accountData = settledValue(results[3], [] as Account[]);
      const groupData = settledValue(results[4], [] as Group[]);
      const campaignData = settledValue(results[5], [] as Campaign[]);
      const draftData = settledValue(results[6], [] as Draft[]);
      const taskData = settledValue(results[7], [] as MessageTask[]);
      const archiveData = settledValue(results[8], [] as ArchiveItem[]);
      const auditData = settledValue(results[9], [] as AuditLog[]);
      const aiProviderData = settledValue(results[10], [] as AiProvider[]);
      const promptTemplateData = settledValue(results[11], [] as PromptTemplate[]);
      const tenantAiData = settledValue(results[12], {} as TenantAiSetting);
      const schedulingData = settledValue(results[13], {} as SchedulingSetting);
      const materialData = settledValue(results[14], [] as Material[]);
      const developerAppData = me.role === '系统管理员' ? await api<DeveloperApp[]>('/developer-apps').catch(() => [] as DeveloperApp[]) : [];
      const tenantData = me.role === '系统管理员' ? await api<Tenant[]>('/tenants').catch(() => [] as Tenant[]) : [];
      const activationCodeData = me.role === '系统管理员' ? await api<ActivationCode[]>('/admin/activation-codes').catch(() => [] as ActivationCode[]) : [];
      const usageLedgerData = me.role === '系统管理员' ? await api<UsageLedger[]>('/admin/usage-ledgers').catch(() => [] as UsageLedger[]) : [];
      const usageSummaryData = me.role === '系统管理员' ? await api<UsageSummary>('/admin/usage-summary').catch(() => null) : null;
      setRuntime(runtimeData);
      setOverview(overviewData);
      setAccountPools(poolData);
      setAccounts(accountData);
      setDeveloperApps(developerAppData);
      setTenants(tenantData);
      setActivationCodes(activationCodeData);
      setUsageLedgers(usageLedgerData);
      setUsageSummary(usageSummaryData);
      setAiProviders(aiProviderData);
      setPromptTemplates(promptTemplateData);
      setTenantAiSetting(tenantAiData);
      setSchedulingSetting(schedulingData);
      setMaterials(materialData);
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

  function showResult(title: string, message: string) {
    setResultDialog({ title, message });
  }

  function closeModal() {
    setModal(null);
  }

  function openConfirm(payload: ConfirmPayload) {
    setModal({
      type: 'confirmAction',
      payload: {
        ...payload,
        onConfirm: async () => {
          await payload.onConfirm();
          setModal(payload.restoreModalType ? { type: payload.restoreModalType } : null);
        },
      },
    });
  }

  function openCampaignModal(groupId?: number) {
    const targetIds = groupId ? [groupId] : selectedTargetGroupIds.length ? selectedTargetGroupIds : [selectedGroupId ?? groups[0]?.id].filter(Boolean) as number[];
    if (groupId) setSelectedGroupId(groupId);
    goToView('taskManagement');
    setTaskManagementTab('任务列表');
    setSelectedTargetGroupIds(targetIds);
    setRecommendedAccounts([]);
    setSelectedAccountsByGroup({});
    setCampaignStep(1);
    setModal({ type: 'campaignCreate' });
  }

  function openAccountCreate(loginNow = false) {
    setLoginAfterCreate(loginNow);
    setAccountCreateForm({
      display_name: '新托管账号',
      username: '',
      phone_number: '',
      pool_id: selectedPoolId || accountPools.find((pool) => pool.is_default)?.id || accountPools[0]?.id || '',
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

  async function openAccountPoolDetail(pool: AccountPool) {
    setBusy('读取账号池详情');
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

  async function createAccount() {
    setBusy('添加账号');
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
    closeModal();
    showResult('账号已添加', loginAfterCreate ? `${created.display_name} 已加入账号池，并准备进入登录流程。` : `${created.display_name} 已加入账号池，可继续启动扫码或验证码登录。`);
    setAccountCreateForm({ display_name: '新托管账号', username: '', phone_number: '', pool_id: '' });
    await refresh();
    if (loginAfterCreate) {
      await runLogin(created, 'qr');
    }
    await openAccountDetail(created);
    setBusy('');
  }

  async function createAccountPool() {
    setBusy('新增账号池');
    const pool = await api<AccountPool>('/account-pools', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: currentUser?.tenant_id ?? 1, ...accountPoolForm }),
    });
    closeModal();
    showResult('账号池已新增', `已新增账号池：${pool.name}`);
    setAccountPoolForm({ name: '新账号池', description: '', is_default: false });
    setSelectedPoolId(pool.id);
    await refresh();
    setBusy('');
  }

  async function moveCurrentAccountPool(poolId: number) {
    if (!accountDetail) return;
    setBusy('移动账号池');
    const updated = await api<Account>(`/tg-accounts/${accountDetail.account.id}/move-pool`, {
      method: 'POST',
      body: JSON.stringify({ pool_id: poolId }),
    });
    showResult('账号池已更新', `${updated.display_name} 已移动到 ${updated.pool_name}`);
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
    const contacts = await api<Contact[]>(`/tg-accounts/${accountDetail.account.id}/contacts/sync`, { method: 'POST' });
    setAccountDetail({ ...accountDetail, contacts, stats: { ...accountDetail.stats, contacts: contacts.length } });
    showResult('联系人已同步', `已同步 ${contacts.length} 个联系人和群友候选，可以直接选中对象创建平台发送任务。`);
    setBusy('');
  }

  async function queueAccountSyncNow() {
    if (!accountDetail) return;
    setBusy('创建同步任务');
    await api<AccountSyncRecord[]>(`/tg-accounts/${accountDetail.account.id}/sync-now`, { method: 'POST' });
    showResult('同步任务已创建', '群聊、云联系人和 TG 官方验证码会由后台任务同步，完成后在同步记录中查看。');
    await refreshAccountDetail();
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
    setBusy('读取任务详情');
    const detail = await api<CampaignDetail>(`/campaigns/${campaign.id}/detail`);
    setCampaignDetail(detail);
    setSelectedCampaignId(campaign.id);
    setBusy('');
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

  async function pollVerificationCodes() {
    if (!accountDetail) return;
    setBusy('同步验证码');
    const codes = await api<VerificationCode[]>(`/tg-accounts/${accountDetail.account.id}/verification-codes/poll`, { method: 'POST' });
    setAccountDetail({ ...accountDetail, verification_codes: codes });
    showResult('验证码已同步', '已从 TG 官方服务消息同步最新验证码，验证码会短时展示并写入审计。');
    setBusy('');
  }

  function toggleTargetGroup(groupId: number) {
    setSelectedTargetGroupIds((current) => {
      const next = current.includes(groupId) ? current.filter((id) => id !== groupId) : [...current, groupId];
      setRecommendedAccounts([]);
      setSelectedAccountsByGroup({});
      return next;
    });
  }

  async function recommendAccounts(groupIds = selectedTargetGroupIds) {
    if (!groupIds.length) {
      setRecommendedAccounts([]);
      setSelectedAccountsByGroup({});
      return;
    }
    const recommendations = await api<RecommendedAccount[]>('/campaigns/recommend-accounts', {
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
      setCampaignStep(2);
    } catch (error) {
      setNotice(`推荐账号失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setBusy('');
    }
  }

  function goCampaignContentStep() {
    if (targetGroupsMissingAccounts.length) return;
    setCampaignStep(3);
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

  async function requestCaptchaToken(): Promise<string> {
    const challenge = await api<CaptchaChallenge>('/auth/captcha/challenge');
    const captcha = await api<CaptchaVerifyResponse>('/auth/captcha/verify', {
      method: 'POST',
      body: JSON.stringify({ challenge_id: challenge.challenge_id, slider_value: challenge.target_value }),
    });
    return captcha.captcha_token;
  }

  async function login() {
    setBusy('登录');
    const captchaToken = await requestCaptchaToken();
    const response = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier: loginEmail, email: loginEmail, password: loginPassword, captcha_token: captchaToken }),
    });
    if (!response.ok) {
      setBusy('');
      setNotice('登录失败，请检查账号和密码');
      return;
    }
    const data = await response.json();
    localStorage.setItem('tg_ops_token', data.access_token);
    setToken(data.access_token);
    setCurrentUser(data.user);
    setBusy('');
  }

  async function register() {
    setBusy('注册');
    try {
      const captchaToken = await requestCaptchaToken();
      const response = await fetch(`${API_BASE}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...registerForm, captcha_token: captchaToken }),
      });
      if (!response.ok) {
        setBusy('');
        setNotice('注册失败，请检查填写信息');
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

  async function submitRedeemCode() {
    if (!redeemCode.trim()) return;
    setBusy('兑换卡密');
    await api('/subscription/redeem', { method: 'POST', body: JSON.stringify({ code: redeemCode.trim() }) });
    setRedeemCode('');
    setNotice('卡密兑换成功，订阅已更新。');
    await refresh();
    setBusy('');
  }

  async function createActivationCodes() {
    setBusy('生成卡密');
    await api('/admin/activation-codes', { method: 'POST', body: JSON.stringify(activationBatch) });
    setNotice('卡密已生成。');
    await refresh();
    setBusy('');
  }

  function logout() {
    localStorage.removeItem('tg_ops_token');
    setToken('');
    setCurrentUser(null);
    setNotice('');
  }

  async function runLogin(account: Account, method: 'code' | 'qr') {
    setBusy('启动登录');
    const flow = await api<{ code_preview?: string; qr_payload?: string; status: string }>(`/tg-accounts/${account.id}/login/start`, {
      method: 'POST',
      body: JSON.stringify({ method }),
    });
    showResult('登录流程已启动', method === 'code' ? `${account.display_name} 已进入验证码登录，请在账号详情中输入或查看短时验证码。` : `${account.display_name} 已进入扫码登录，请在账号详情查看扫码状态。`);
    await refresh();
    if (accountDetail?.account.id === account.id) await refreshAccountDetail();
  }

  async function verifyAccount(account: Account) {
    const code = window.prompt(`请输入 ${account.display_name} 收到的验证码：`);
    if (!code) return;
    setBusy('验证登录');
    await api(`/tg-accounts/${account.id}/login/verify`, {
      method: 'POST',
      body: JSON.stringify({ code }),
    });
    showResult('验证完成', `${account.display_name} 已完成登录验证`);
    setBusy('');
    await refresh();
    if (accountDetail?.account.id === account.id) await refreshAccountDetail();
  }

  async function healthCheck(account: Account) {
    setBusy('健康检查');
    const result = await api<Account>(`/tg-accounts/${account.id}/health-check`, { method: 'POST' });
    showResult('健康检查完成', `${account.display_name}：${result.status}，健康分 ${result.health_score}`);
    await refresh();
  }

  async function syncAccountGroups(account: Account) {
    setBusy('同步群聊');
    const synced = await api<Group[]>(`/tg-accounts/${account.id}/sync-groups`, { method: 'POST' });
    showResult('群聊同步完成', `${account.display_name} 已同步 ${synced.length} 个群聊`);
    await refresh();
  }

  async function createCampaignAndDrafts() {
    const targetIds = selectedTargetGroupIds.length ? selectedTargetGroupIds : selectedGroup ? [selectedGroup.id] : [];
    const primaryGroup = groups.find((group) => group.id === targetIds[0]) ?? selectedGroup;
    if (!primaryGroup || !targetIds.length) return;
    setBusy('创建任务');
    const campaign = await api<Campaign>('/campaigns', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: currentUser?.tenant_id ?? 1,
        group_id: primaryGroup.id,
        title: `${primaryGroup.title}${targetIds.length > 1 ? `等 ${targetIds.length} 个群` : ''} 活跃任务`,
        campaign_type: '多账号对话脚本',
        topic,
        send_window: sendWindow,
        intensity,
        ai_provider_id: selectedAiProviderId || null,
        jitter_min_seconds: jitterMinSeconds,
        jitter_max_seconds: jitterMaxSeconds,
        batch_interval_seconds: batchIntervalSeconds,
        respect_send_window: respectSendWindow,
        material_ids: selectedMaterialIds.join(','),
        target_group_ids: targetIds,
        selected_account_ids_by_group: selectedAccountsByGroup,
      }),
    });
    await api(`/campaigns/${campaign.id}/generate-drafts`, {
      method: 'POST',
      body: JSON.stringify({
        count: draftCount,
        tone,
        use_ai: true,
        fallback_to_mock: tenantAiSetting?.fallback_to_mock ?? true,
        selected_account_ids_by_group: selectedAccountsByGroup,
      }),
    });
    closeModal();
    showResult('任务已创建', '系统提示词已自动决策提示词模板，并生成草稿等待人工审核。');
    goToView('taskManagement');
    setTaskManagementTab('草稿审核');
    await refresh();
    setSelectedCampaignId(campaign.id);
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
    const campaign = selectedCampaign;
    if (!campaign) return;
    setBusy('批量审核');
    const created = await api<MessageTask[]>(`/campaigns/${campaign.id}/approve-all`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    showResult('批量审核完成', `已审核 ${created.length} 条草稿并生成消息任务。`);
    goToView('taskManagement');
    setTaskManagementTab('发送进度');
    await refresh();
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
    goToView('archives');
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

  async function createDeveloperApp() {
    setBusy('新增开发者应用');
    const created = await api<DeveloperApp>('/developer-apps', {
      method: 'POST',
      body: JSON.stringify({
        ...developerAppForm,
        api_id: Number(developerAppForm.api_id),
        max_accounts: Number(developerAppForm.max_accounts),
      }),
    });
    closeModal();
    showResult('开发者应用已新增', `已新增开发者应用：${created.app_name}`);
    setDeveloperAppForm({ app_name: '备用开发者应用', api_id: '', api_hash: '', max_accounts: 0, notes: '' });
    await refresh();
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

  async function toggleDeveloperApp(app: DeveloperApp) {
    setBusy(app.is_active ? '禁用开发者应用' : '启用开发者应用');
    const updated = await api<DeveloperApp>(`/developer-apps/${app.id}/${app.is_active ? 'disable' : 'enable'}`, { method: 'POST' });
    showResult('开发者应用状态已更新', `${updated.app_name} 已${updated.is_active ? '启用' : '禁用'}`);
    await refresh();
  }

  async function checkDeveloperApp(app: DeveloperApp) {
    setBusy('检查开发者应用');
    const checked = await api<DeveloperApp>(`/developer-apps/${app.id}/check`, { method: 'POST' });
    showResult('检查完成', `${checked.app_name}：${checked.health_status}`);
    await refresh();
  }

  async function createAiProvider() {
    setBusy('新增 AI 供应商');
    const created = await api<AiProvider>('/ai-providers', {
      method: 'POST',
      body: JSON.stringify({ ...aiProviderForm, provider_type: 'openai_compatible', is_active: true }),
    });
    closeModal();
    showResult('AI 供应商已新增', `已新增 AI 供应商：${created.provider_name}`);
    setAiProviderForm({ provider_name: 'MiMo V2.5 Pro', base_url: 'mock://openai-compatible', model_name: 'mimo-v2.5-pro', api_key: '', api_key_header: 'Authorization', notes: '' });
    await refresh();
  }

  async function checkAiProvider(provider: AiProvider) {
    setBusy('检查 AI 供应商');
    const checked = await api<AiProvider>(`/ai-providers/${provider.id}/check`, { method: 'POST' });
    showResult('AI 供应商检查完成', `${checked.provider_name}：${checked.health_status}`);
    await refresh();
  }

  async function saveTenantAiSetting() {
    if (!tenantAiSetting) return;
    setBusy('保存 AI 配置');
    await api('/tenant-ai-settings', {
      method: 'PATCH',
      body: JSON.stringify({
        default_provider_id: selectedAiProviderId || tenantAiSetting.default_provider_id,
        ai_enabled: tenantAiSetting.ai_enabled,
        fallback_to_mock: tenantAiSetting.fallback_to_mock,
        temperature: tenantAiSetting.temperature,
        max_tokens: tenantAiSetting.max_tokens,
      }),
    });
    closeModal();
    showResult('AI 配置已保存', '客户默认模型、温度、Token 和回退策略已更新。');
    await refresh();
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

    // Activation & Usage
    activationCodes,
    setActivationCodes,
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
    profileForm,
    setProfileForm,
    avatarFile,
    setAvatarFile,

    // Group & Campaign state
    selectedGroupId,
    setSelectedGroupId,
    campaignStep,
    setCampaignStep,
    selectedTargetGroupIds,
    setSelectedTargetGroupIds,
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

    // Modal & Dialog
    modal,
    setModal,
    resultDialog,
    setResultDialog,
    busy,
    setBusy,
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
    refresh,
    showResult,
    closeModal,
    openConfirm,
    openCampaignModal,
    openAccountCreate,
    openAccountDetail,
    openAccountPoolDetail,
    refreshAccountPoolDetail,
    createAccount,
    createAccountPool,
    moveCurrentAccountPool,
    createClonePlan,
    confirmClonePlan,
    retryCloneItem,
    confirmVerificationTask,
    dismissVerificationTask,
    refreshAccountDetail,
    syncAccountContacts,
    queueAccountSyncNow,
    startDirectMessageToContact,
    openGroupDetail,
    loadCampaignDetail,
    openDraftEdit,
    saveDraftEdit,
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes,
    toggleTargetGroup,
    recommendAccounts,
    toggleRecommendedAccount,
    setGroupAccountsSelected,
    goCampaignAccountStep,
    goCampaignContentStep,
    createDirectMessageTask,
    saveAccountProfile,
    retryAccountProfileSync,
    login,
    register,
    submitRedeemCode,
    createActivationCodes,
    logout,
    runLogin,
    verifyAccount,
    healthCheck,
    syncAccountGroups,
    createCampaignAndDrafts,
    approveDraft,
    rejectDraft,
    approveAllDrafts,
    cancelTask,
    dispatchTask,
    drainQueue,
    retryTask,
    authorizeSelectedGroup,
    createArchive,
    saveGroupPolicy,
    openArchiveDetail,
    exportArchive,
    createDeveloperApp,
    toggleDeveloperApp,
    checkDeveloperApp,
    openTenantEdit,
    saveTenantQuota,
    createAiProvider,
    checkAiProvider,
    saveTenantAiSetting,
    saveSchedulingSetting,
    createPromptTemplate,
    createMaterial,
    toggleMaterial,
    accountName,
    groupName,
    choosePoolSendAccount,
  }), [token, currentUser, authMode, loginEmail, loginPassword, registerForm, activeView, runtime, overview, accountPools, selectedPoolId, accounts, developerApps, tenants, activationCodes, usageLedgers, usageSummary, redeemCode, activationBatch, aiProviders, promptTemplates, tenantAiSetting, schedulingSetting, materials, groups, campaigns, drafts, tasks, selectedCampaignId, taskManagementTab, archives, archiveDetail, audits, auditFilters, accountDetail, accountContacts, selectedDirectContact, accountDetailTab, accountPoolDetail, poolDirectAccountId, returnAfterVerification, groupDetail, campaignDetail, draftEditTarget, draftEditForm, accountCreateForm, accountPoolForm, cloneForm, loginAfterCreate, profileForm, avatarFile, selectedGroupId, campaignStep, selectedTargetGroupIds, recommendedAccounts, selectedAccountsByGroup, topic, sendWindow, intensity, draftCount, tone, selectedAiProviderId, selectedMaterialIds, jitterMinSeconds, jitterMaxSeconds, batchIntervalSeconds, respectSendWindow, taskStatusFilter, groupPolicy, developerAppForm, tenantForm, aiProviderForm, promptTemplateForm, materialForm, modal, resultDialog, busy, notice, directMessageForm, selectedPool, selectedGroup, selectedCampaign, selectedCampaignDrafts, selectedCampaignTasks, targetGroupsMissingAccounts, taskSummary, refresh, showResult, closeModal, openConfirm, openCampaignModal, openAccountCreate, openAccountDetail, openAccountPoolDetail, refreshAccountPoolDetail, createAccount, createAccountPool, moveCurrentAccountPool, createClonePlan, confirmClonePlan, retryCloneItem, confirmVerificationTask, dismissVerificationTask, refreshAccountDetail, syncAccountContacts, queueAccountSyncNow, startDirectMessageToContact, openGroupDetail, loadCampaignDetail, openDraftEdit, saveDraftEdit, avatarUrl, openAccountProfileEdit, pollVerificationCodes, toggleTargetGroup, recommendAccounts, toggleRecommendedAccount, setGroupAccountsSelected, goCampaignAccountStep, goCampaignContentStep, createDirectMessageTask, saveAccountProfile, retryAccountProfileSync, login, register, submitRedeemCode, createActivationCodes, logout, runLogin, verifyAccount, healthCheck, syncAccountGroups, createCampaignAndDrafts, approveDraft, rejectDraft, approveAllDrafts, cancelTask, dispatchTask, drainQueue, retryTask, authorizeSelectedGroup, createArchive, saveGroupPolicy, openArchiveDetail, exportArchive, createDeveloperApp, toggleDeveloperApp, checkDeveloperApp, openTenantEdit, saveTenantQuota, createAiProvider, checkAiProvider, createPromptTemplate, saveTenantAiSetting, saveSchedulingSetting, createMaterial, toggleMaterial, accountName, groupName, choosePoolSendAccount]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
