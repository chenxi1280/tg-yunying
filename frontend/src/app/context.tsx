import React, { createContext, useContext, useState, useMemo, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { App as AntdApp } from 'antd';
import { api, AUTH_EXPIRED_EVENT, isAuthExpiredError } from '../shared/api/client';
import { operationLabel } from './components/shared';
import type {
  Overview,
  RuntimeConfig,
  CurrentUser,
  Tenant,
  AdminUser,
  AdminUserForm,
  CaptchaChallenge,
  TokenLedger,
  UsageLedger,
  UsageSummary,
  Account,
  AccountPool,
  DeveloperApp,
  AiProvider,
  PromptTemplate,
  TenantAiSetting,
  Material,
  MaterialCacheHealth,
  MaterialCacheConfig,
  MaterialImportResult,
  ContentKeywordRule,
  Group,
  MessageTask,
  ArchiveItem,
  ArchiveDetail,
  ArchiveExport,
  AuditFilters,
  AuditLog,
  AccountDetail,
  AccountPoolDetail,
  GroupDetail,
  ModalState,
  AccountLoginForm,
} from './types';
import { VIEW_ROUTES, viewFromPath } from './routes';
import type { AppState } from './context/types';
import { createAccountActions } from './context/accountActions';
import { useActionRunner } from './context/actionRunner';
import { createAuthActions } from './context/authActions';
import { createContentActions } from './context/contentActions';
import { createMessageActions } from './context/messageActions';
import { createModalStateActions } from './context/modalState';
import { createSystemActions } from './context/systemActions';
import { EMPTY_ACCOUNT_LOGIN_FORM, defaultAccountCreateForm, defaultAccountPoolForm, defaultAdminUserForm, defaultAiProviderForm, defaultAuditFilters, defaultCloneForm, defaultDeveloperAppForm, defaultDirectMessageForm, defaultGroupPolicy, defaultKeywordRuleForm, defaultMaterialForm, defaultProfileForm, defaultPromptTemplateForm, defaultTenantForm } from './context/defaults';
import { loadAppSnapshot } from './context/refresh';

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
  const [adminUserForm, setAdminUserForm] = useState<AdminUserForm>(() => defaultAdminUserForm());
  const [tokenAdjustmentForm, setTokenAdjustmentForm] = useState({ delta_tokens: 500000, reason: '管理员充值' });
  const [usageLedgers, setUsageLedgers] = useState<UsageLedger[]>([]);
  const [usageSummary, setUsageSummary] = useState<UsageSummary | null>(null);
  const [aiProviders, setAiProviders] = useState<AiProvider[]>([]);
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [tenantAiSetting, setTenantAiSetting] = useState<TenantAiSetting | null>(null);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [materialCacheHealth, setMaterialCacheHealth] = useState<MaterialCacheHealth | null>(null);
  const [materialCacheConfig, setMaterialCacheConfig] = useState<MaterialCacheConfig | null>(null);
  const [materialImports, setMaterialImports] = useState<MaterialImportResult[]>([]);
  const [contentKeywordRules, setContentKeywordRules] = useState<ContentKeywordRule[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [tasks, setTasks] = useState<MessageTask[]>([]);
  const [taskManagementTab, setTaskManagementTab] = useState('任务列表');
  const [archives, setArchives] = useState<ArchiveItem[]>([]);
  const [archiveDetail, setArchiveDetail] = useState<ArchiveDetail | null>(null);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [auditFilters, setAuditFilters] = useState<AuditFilters>(() => defaultAuditFilters());
  const [accountDetail, setAccountDetail] = useState<AccountDetail | null>(null);
  const [accountDetailTab, setAccountDetailTab] = useState('资料');
  const [accountPoolDetail, setAccountPoolDetail] = useState<AccountPoolDetail | null>(null);
  const [poolDirectAccountId, setPoolDirectAccountId] = useState<number | ''>('');
  const [returnAfterVerification, setReturnAfterVerification] = useState<'accountDetail' | 'accountPoolDetail'>('accountDetail');
  const [groupDetail, setGroupDetail] = useState<GroupDetail | null>(null);
  const [accountCreateForm, setAccountCreateForm] = useState(() => defaultAccountCreateForm());
  const [accountPoolForm, setAccountPoolForm] = useState(() => defaultAccountPoolForm());
  const [cloneForm, setCloneForm] = useState(() => defaultCloneForm());
  const [loginAfterCreate, setLoginAfterCreate] = useState(false);
  const [accountLoginForm, setAccountLoginForm] = useState<AccountLoginForm>(EMPTY_ACCOUNT_LOGIN_FORM);
  const [profileForm, setProfileForm] = useState(() => defaultProfileForm());
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState<number | null>(null);
  const [selectedAiProviderId, setSelectedAiProviderId] = useState<number | ''>('');
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [groupPolicy, setGroupPolicy] = useState(() => defaultGroupPolicy());
  const [developerAppForm, setDeveloperAppForm] = useState(() => defaultDeveloperAppForm());
  const [tenantForm, setTenantForm] = useState(() => defaultTenantForm());
  const [aiProviderForm, setAiProviderForm] = useState(() => defaultAiProviderForm());
  const [promptTemplateForm, setPromptTemplateForm] = useState(() => defaultPromptTemplateForm());
  const [materialForm, setMaterialForm] = useState(() => defaultMaterialForm());
  const [materialFile, setMaterialFile] = useState<File[] | null>(null);
  const [keywordRuleForm, setKeywordRuleForm] = useState(() => defaultKeywordRuleForm());
  const [modal, setModal] = useState<ModalState>(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [directMessageForm, setDirectMessageForm] = useState(() => defaultDirectMessageForm());
  const { pendingActionKeys, isActionPending, runWithLoading } = useActionRunner(setBusy);

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

  async function refresh() {
    setBusy('刷新数据');
    try {
      const snapshot = await loadAppSnapshot({ activeView, selectedPoolId, taskStatusFilter, auditFilters });
      setCurrentUser(snapshot.me);
      setRuntime(snapshot.runtime);
      setOverview(snapshot.overview);
      setAccountPools(snapshot.accountPools);
      setAccounts(snapshot.accounts);
      setDeveloperApps(snapshot.developerApps);
      setTenants(snapshot.tenants);
      setAdminUsers(snapshot.adminUsers);
      setSelectedAdminUserId((current) => current ?? snapshot.adminUsers[0]?.id ?? null);
      setUsageLedgers(snapshot.usageLedgers);
      setUsageSummary(snapshot.usageSummary);
      setAiProviders(snapshot.aiProviders);
      setPromptTemplates(snapshot.promptTemplates);
      setTenantAiSetting(snapshot.tenantAiSetting);
      if (snapshot.contentResources) {
        setMaterials(snapshot.contentResources.materials);
        setMaterialCacheHealth(snapshot.contentResources.materialCacheHealth);
        setMaterialCacheConfig(snapshot.contentResources.materialCacheConfig);
        setMaterialImports(snapshot.contentResources.materialImports);
        setContentKeywordRules(snapshot.contentResources.contentKeywordRules);
      }
      setGroups(snapshot.groups);
      setTasks(snapshot.tasks);
      setArchives(snapshot.archives);
      setAudits(snapshot.audits);
      setSelectedGroupId((current) => current ?? snapshot.groups[0]?.id ?? null);
      setSelectedAiProviderId((current) => current || snapshot.tenantAiSetting.default_provider_id || snapshot.aiProviders[0]?.id || '');
    } finally {
      setBusy('');
    }
  }

  function expireAdminSession() {
    localStorage.removeItem('tg_ops_token');
    setToken('');
    setCurrentUser(null);
    setModal(null);
    setBusy('');
    setNotice('登录已过期，请重新登录。');
  }

  useEffect(() => {
    function handleAuthExpired() {
      expireAdminSession();
    }
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, []);

  useEffect(() => {
    if (!token) return;
    refresh().catch((error) => {
      if (isAuthExpiredError(error)) {
        expireAdminSession();
        return;
      }
      setNotice(`后端未连接或接口异常：${error.message}`);
    });
  }, [token, taskStatusFilter, selectedPoolId, activeView]);

  const { showResult, errorMessage, handleActionError, closeModal, openConfirm } = createModalStateActions({
    message,
    modalApi,
    setModal,
  });

  const {
    refreshCaptchaChallenge,
    verifyCaptcha,
    login,
    register,
    changePassword,
    logout,
  } = createAuthActions({
    captchaChallenge,
    captchaInput,
    captchaToken,
    loginEmail,
    loginPassword,
    registerForm,
    changePasswordForm,
    setAuthMode,
    setBusy,
    setCaptchaChallenge,
    setCaptchaError,
    setCaptchaInput,
    setCaptchaLoading,
    setCaptchaToken,
    setChangePasswordForm,
    setCurrentUser,
    setNotice,
    setToken,
    closeModal,
    handleActionError,
    showResult,
  });

  const {
    createMaterial,
    disableMaterial,
    openMaterialEdit,
    restoreMaterial,
    saveMaterial,
    createContentKeywordRule,
    openContentKeywordRuleEdit,
    saveContentKeywordRule,
  } = createContentActions({
    currentUser,
    keywordRuleForm,
    materialFile,
    materialForm,
    setKeywordRuleForm,
    setMaterialFile,
    setMaterialForm,
    setMaterials,
    setModal,
    setBusy,
    closeModal,
    refresh,
    showResult,
  });

  const accountActions = createAccountActions({
    accountCreateForm,
    accountDetail,
    accountLoginForm,
    accountPoolDetail,
    accountPoolForm,
    accountPools,
    avatarFile,
    cloneForm,
    currentUser,
    groupDetail,
    poolDirectAccountId,
    profileForm,
    runtime,
    selectedPoolId,
    choosePoolSendAccount,
    closeModal,
    errorMessage,
    goToView,
    handleActionError,
    refresh,
    runWithLoading,
    setAccountCreateForm,
    setAccountDetail,
    setAccountDetailTab,
    setAccountLoginForm,
    setAccountPoolDetail,
    setAccountPoolForm,
    setAvatarFile,
    setBusy,
    setDirectMessageForm,
    setGroupDetail,
    setLoginAfterCreate,
    setModal,
    setNotice,
    setPoolDirectAccountId,
    setProfileForm,
    setSelectedGroupId,
    setSelectedPoolId,
    showResult,
  });
  const {
    openAccountCreate,
    openAccountDetail,
    openAccountVerificationCodes,
    openAccountMovePool,
    openAccountPoolDetail,
    refreshAccountPoolDetail,
    createAccount,
    createAccountPool,
    moveCurrentAccountPool,
    createClonePlan,
    confirmClonePlan,
    retryCloneItem,
    confirmVerificationTask,
    resolveGroupRestrictionTask,
    resolveGroupRestrictionBatch,
    dismissVerificationTask,
    refreshAccountDetail,
    syncAccountContacts,
    queueAccountSyncNow,
    openGroupDetail,
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes,
    saveAccountProfile,
    retryAccountProfileSync,
    runLogin,
    verifyAccount,
    chooseAccountLoginMethod,
    submitAccountLoginCode,
    submitAccountLoginPassword,
    resendAccountLoginCode,
    checkAccountQrLogin,
    deleteAccount,
    healthCheck,
    syncAccountGroups,
  } = accountActions;

  const {
    startDirectMessageToContact,
    createDirectMessageTask,
    createMessageSendTask,
    cancelTask,
    dispatchTask,
    drainQueue,
    retryTask,
  } = createMessageActions({
    accountDetail,
    accountPoolDetail,
    directMessageForm,
    modalType: modal?.type ?? null,
    poolDirectAccountId,
    setAccountDetailTab,
    setBusy,
    setDirectMessageForm,
    setPoolDirectAccountId,
    setTasks,
    refresh,
    refreshAccountDetail: accountActions.refreshAccountDetail,
    refreshAccountPoolDetail: accountActions.refreshAccountPoolDetail,
    showResult,
  });

  const {
    createDeveloperApp,
    openDeveloperAppEdit,
    toggleDeveloperApp,
    checkDeveloperApp,
    openTenantEdit,
    saveTenantQuota,
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
  } = createSystemActions({
    adminUserForm,
    aiProviderForm,
    currentUser,
    developerAppForm,
    promptTemplateForm,
    selectedAiProviderId,
    tenantAiSetting,
    tenantForm,
    tokenAdjustmentForm,
    closeModal,
    handleActionError,
    refresh,
    setAdminUserForm,
    setAiProviderForm,
    setBusy,
    setDeveloperAppForm,
    setModal,
    setNotice,
    setPromptTemplateForm,
    setPromptTemplates,
    setSelectedAdminUserId,
    setSelectedUserTokenLedgers,
    setTenantForm,
    showResult,
  });

  useEffect(() => {
    if (!token) {
      void refreshCaptchaChallenge();
    }
  }, [token, authMode]);

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

  function accountName(accountId: number | null | undefined) {
    if (!accountId) return '未指定';
    const account = accounts.find((item) => item.id === accountId);
    return account ? `${account.display_name} #${account.id}` : `账号 ${accountId}`;
  }

  function groupName(groupId: number | null | undefined) {
    if (!groupId) return '未指定';
    return groups.find((item) => item.id === groupId)?.title ?? `群 ${groupId}`;
  }

  function goToView(viewId: string, search: string = '') {
    setActiveView(viewId);
    navigate(`${VIEW_ROUTES[viewId] ?? '/dashboard'}${search}`);
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
    materialCacheConfig,
    setMaterialCacheConfig,
    materialImports,
    setMaterialImports,
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
    resolveGroupRestrictionBatch: (task) => runWithLoading(`verification:${task.id}:resolve-group-batch`, '批量重查群限制', () => resolveGroupRestrictionBatch(task)),
    dismissVerificationTask: (task) => runWithLoading(`verification:${task.id}:dismiss`, '忽略验证辅助', () => dismissVerificationTask(task)),
    refreshAccountDetail: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:detail-refresh`, '刷新账号详情', refreshAccountDetail),
    syncAccountContacts: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:contacts`, '同步联系人', syncAccountContacts),
    queueAccountSyncNow: () => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:sync`, '同步账号数据', queueAccountSyncNow),
    startDirectMessageToContact,
    openGroupDetail: (group) => runWithLoading(`group:${group.id}:detail`, '读取群详情', () => openGroupDetail(group)),
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes: (reason) => runWithLoading(`account:${accountDetail?.account.id ?? 'current'}:codes`, '同步验证码', () => pollVerificationCodes(reason)),
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
    disableMaterial: (material) => runWithLoading(`material:${material.id}:disable`, '禁用素材', () => disableMaterial(material)),
    openMaterialEdit,
    restoreMaterial: (material) => runWithLoading(`material:${material.id}:restore`, '恢复素材', () => restoreMaterial(material)),
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
