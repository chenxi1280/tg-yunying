import React from 'react';
import type {
  Overview, RuntimeConfig, CurrentUser, CaptchaChallenge, CaptchaVerifyResponse,
  ActivationCode, ActivationCodeCreateForm, ActivationCodeFilters, ActivationCodePage,
  UsageLedger, UsageSummary, LoginFlow, Account, AccountPool, AccountLoginForm,
  DeveloperApp, AiProvider, PromptTemplate, TenantAiSetting, SchedulingSetting,
  Material, Contact, Group, Campaign, Draft, MessageTask, ArchiveItem, ArchiveDetail,
  ArchiveExport, AuditLog, VerificationCode, AccountSyncRecord, VerificationTask,
  AccountCloneItem, AccountClonePlan, AccountGroup, ProfileSyncRecord,
  AccountDetail, AccountPoolDetail, GroupDetail, CampaignDetail, RecommendedAccount, Tenant,
  ConfirmPayload, ModalState, ResultDialogState,
} from '../types';

export interface AppState {
  // Auth state
  token: string;
  setToken: (token: string) => void;
  currentUser: CurrentUser | null;
  setCurrentUser: (user: CurrentUser | null) => void;
  authMode: 'login' | 'register';
  setAuthMode: (mode: 'login' | 'register') => void;
  loginEmail: string;
  setLoginEmail: (email: string) => void;
  loginPassword: string;
  setLoginPassword: (password: string) => void;
  registerForm: { name: string; email: string; phone: string; password: string };
  setRegisterForm: React.Dispatch<React.SetStateAction<{ name: string; email: string; phone: string; password: string }>>;
  changePasswordForm: { current_password: string; new_password: string; confirm_password: string };
  setChangePasswordForm: React.Dispatch<React.SetStateAction<{ current_password: string; new_password: string; confirm_password: string }>>;
  captchaChallenge: CaptchaChallenge | null;
  captchaInput: string;
  setCaptchaInput: (value: string) => void;
  captchaToken: string;
  captchaError: string;
  captchaLoading: boolean;
  refreshCaptchaChallenge: () => Promise<void>;
  verifyCaptcha: () => Promise<void>;

  // View state
  activeView: string;
  setActiveView: (view: string) => void;
  goToView: (viewId: string) => void;

  // Runtime & Overview
  runtime: RuntimeConfig | null;
  setRuntime: (config: RuntimeConfig | null) => void;
  overview: Overview | null;
  setOverview: (overview: Overview | null) => void;

  // Account pools & accounts
  accountPools: AccountPool[];
  setAccountPools: (pools: AccountPool[]) => void;
  selectedPoolId: number | '';
  setSelectedPoolId: (id: number | '') => void;
  accounts: Account[];
  setAccounts: (accounts: Account[]) => void;

  // Developer apps
  developerApps: DeveloperApp[];
  setDeveloperApps: (apps: DeveloperApp[]) => void;
  tenants: Tenant[];
  setTenants: (tenants: Tenant[]) => void;

  // Activation & Usage
  activationCodes: ActivationCode[];
  setActivationCodes: (codes: ActivationCode[]) => void;
  activationCodePage: ActivationCodePage;
  setActivationCodePage: (page: ActivationCodePage) => void;
  activationCodeFilters: ActivationCodeFilters;
  setActivationCodeFilters: React.Dispatch<React.SetStateAction<ActivationCodeFilters>>;
  usageLedgers: UsageLedger[];
  setUsageLedgers: (ledgers: UsageLedger[]) => void;
  usageSummary: UsageSummary | null;
  setUsageSummary: (summary: UsageSummary | null) => void;
  redeemCode: string;
  setRedeemCode: (code: string) => void;
  activationBatch: ActivationCodeCreateForm;
  setActivationBatch: React.Dispatch<React.SetStateAction<ActivationCodeCreateForm>>;

  // AI & Templates
  aiProviders: AiProvider[];
  setAiProviders: (providers: AiProvider[]) => void;
  promptTemplates: PromptTemplate[];
  setPromptTemplates: (templates: PromptTemplate[]) => void;
  tenantAiSetting: TenantAiSetting | null;
  setTenantAiSetting: (setting: TenantAiSetting | null) => void;
  schedulingSetting: SchedulingSetting | null;
  setSchedulingSetting: (setting: SchedulingSetting | null) => void;
  materials: Material[];
  setMaterials: (materials: Material[]) => void;

  // Groups & Campaigns
  groups: Group[];
  setGroups: (groups: Group[]) => void;
  campaigns: Campaign[];
  setCampaigns: (campaigns: Campaign[]) => void;
  drafts: Draft[];
  setDrafts: (drafts: Draft[]) => void;
  tasks: MessageTask[];
  setTasks: (tasks: MessageTask[]) => void;
  selectedCampaignId: number | null;
  setSelectedCampaignId: (id: number | null) => void;
  taskManagementTab: string;
  setTaskManagementTab: (tab: string) => void;

  // Archives
  archives: ArchiveItem[];
  setArchives: (archives: ArchiveItem[]) => void;
  archiveDetail: ArchiveDetail | null;
  setArchiveDetail: (detail: ArchiveDetail | null) => void;

  // Audits
  audits: AuditLog[];
  setAudits: (logs: AuditLog[]) => void;
  auditFilters: { actor: string; action: string; target_type: string; start_at: string; end_at: string };
  setAuditFilters: (filters: { actor: string; action: string; target_type: string; start_at: string; end_at: string }) => void;

  // Detail states
  accountDetail: AccountDetail | null;
  setAccountDetail: (detail: AccountDetail | null) => void;
  accountContacts: Contact[];
  selectedDirectContact: Contact | null;
  accountDetailTab: string;
  setAccountDetailTab: (tab: string) => void;
  accountPoolDetail: AccountPoolDetail | null;
  setAccountPoolDetail: (detail: AccountPoolDetail | null) => void;
  poolDirectAccountId: number | '';
  setPoolDirectAccountId: (id: number | '') => void;
  returnAfterVerification: 'accountDetail' | 'accountPoolDetail';
  setReturnAfterVerification: (mode: 'accountDetail' | 'accountPoolDetail') => void;
  groupDetail: GroupDetail | null;
  setGroupDetail: (detail: GroupDetail | null) => void;
  campaignDetail: CampaignDetail | null;
  setCampaignDetail: (detail: CampaignDetail | null) => void;

  // Draft edit
  draftEditTarget: Draft | null;
  setDraftEditTarget: (draft: Draft | null) => void;
  draftEditForm: { content: string; risk_level: string; suggested_account_id: number | '' };
  setDraftEditForm: (form: { content: string; risk_level: string; suggested_account_id: number | '' }) => void;

  // Account forms
  accountCreateForm: { display_name: string; username: string; phone_number: string; pool_id: number | '' };
  setAccountCreateForm: (form: { display_name: string; username: string; phone_number: string; pool_id: number | '' }) => void;
  accountPoolForm: { name: string; description: string; is_default: boolean };
  setAccountPoolForm: (form: { name: string; description: string; is_default: boolean }) => void;
  cloneForm: { target_account_ids: number[]; clone_contacts: boolean; clone_groups: boolean };
  setCloneForm: (form: { target_account_ids: number[]; clone_contacts: boolean; clone_groups: boolean }) => void;
  loginAfterCreate: boolean;
  setLoginAfterCreate: (login: boolean) => void;
  accountLoginForm: AccountLoginForm;
  setAccountLoginForm: React.Dispatch<React.SetStateAction<AccountLoginForm>>;
  profileForm: { display_name: string; tg_first_name: string; tg_last_name: string; tg_bio: string; avatar_object_key: string };
  setProfileForm: (form: { display_name: string; tg_first_name: string; tg_last_name: string; tg_bio: string; avatar_object_key: string }) => void;
  avatarFile: File | null;
  setAvatarFile: (file: File | null) => void;

  // Group & Campaign state
  selectedGroupId: number | null;
  setSelectedGroupId: (id: number | null) => void;
  campaignStep: number;
  setCampaignStep: (step: number) => void;
  selectedTargetGroupIds: number[];
  setSelectedTargetGroupIds: (ids: number[]) => void;
  recommendedAccounts: RecommendedAccount[];
  setRecommendedAccounts: (accounts: RecommendedAccount[]) => void;
  selectedAccountsByGroup: Record<string, number[]>;
  setSelectedAccountsByGroup: (accounts: Record<string, number[]>) => void;
  topic: string;
  setTopic: (topic: string) => void;
  sendWindow: string;
  setSendWindow: (window: string) => void;
  intensity: string;
  setIntensity: (intensity: string) => void;
  draftCount: number;
  setDraftCount: (count: number) => void;
  tone: string;
  setTone: (tone: string) => void;
  selectedAiProviderId: number | '';
  setSelectedAiProviderId: (id: number | '') => void;
  selectedMaterialIds: number[];
  setSelectedMaterialIds: (ids: number[]) => void;
  jitterMinSeconds: number;
  setJitterMinSeconds: (seconds: number) => void;
  jitterMaxSeconds: number;
  setJitterMaxSeconds: (seconds: number) => void;
  batchIntervalSeconds: number;
  setBatchIntervalSeconds: (seconds: number) => void;
  respectSendWindow: boolean;
  setRespectSendWindow: (respect: boolean) => void;
  taskStatusFilter: string;
  setTaskStatusFilter: (filter: string) => void;
  groupPolicy: {
    active_window: string;
    daily_limit: number;
    account_cooldown_seconds: number;
    group_cooldown_seconds: number;
    topic_direction: string;
    banned_words: string;
    link_whitelist: string;
    require_review: boolean;
    listener_enabled: boolean;
    listener_auto_reply_enabled: boolean;
    listener_interval_seconds: number;
    listener_context_limit: number;
    listener_account_ids: number[];
  };
  setGroupPolicy: (policy: {
    active_window: string;
    daily_limit: number;
    account_cooldown_seconds: number;
    group_cooldown_seconds: number;
    topic_direction: string;
    banned_words: string;
    link_whitelist: string;
    require_review: boolean;
    listener_enabled: boolean;
    listener_auto_reply_enabled: boolean;
    listener_interval_seconds: number;
    listener_context_limit: number;
    listener_account_ids: number[];
  }) => void;

  // Developer & AI forms
  developerAppForm: { id: number | null; app_name: string; api_id: string; api_hash: string; max_accounts: number; notes: string; is_active: boolean };
  setDeveloperAppForm: (form: { id: number | null; app_name: string; api_id: string; api_hash: string; max_accounts: number; notes: string; is_active: boolean }) => void;
  tenantForm: { id: number | null; name: string; plan_name: string; account_quota: number; task_quota: number };
  setTenantForm: (form: { id: number | null; name: string; plan_name: string; account_quota: number; task_quota: number }) => void;
  aiProviderForm: { id: number | null; provider_name: string; base_url: string; model_name: string; api_key: string; api_key_header: string; notes: string; is_active: boolean };
  setAiProviderForm: (form: { id: number | null; provider_name: string; base_url: string; model_name: string; api_key: string; api_key_header: string; notes: string; is_active: boolean }) => void;
  promptTemplateForm: { name: string; template_type: string; content: string };
  setPromptTemplateForm: (form: { name: string; template_type: string; content: string }) => void;
  materialForm: { title: string; material_type: string; content: string; tags: string };
  setMaterialForm: (form: { title: string; material_type: string; content: string; tags: string }) => void;

  // Modal & Dialog
  modal: ModalState;
  setModal: (modal: ModalState) => void;
  resultDialog: ResultDialogState;
  setResultDialog: (dialog: ResultDialogState) => void;
  busy: string;
  setBusy: (busy: string) => void;
  notice: string;
  setNotice: (notice: string) => void;

  // Direct message
  directMessageForm: { target_peer_id: string; target_display: string; content: string };
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;

  // Computed values
  selectedPool: AccountPool | null;
  selectedGroup: Group | null;
  selectedCampaign: Campaign | null;
  selectedCampaignDrafts: Draft[];
  selectedCampaignTasks: MessageTask[];
  targetGroupsMissingAccounts: number[];
  taskSummary: {
    campaigns: number;
    pendingDrafts: number;
    queued: number;
    sending: number;
    sent: number;
    failed: number;
  };

  // Handler functions
  refresh: () => Promise<void>;
  showResult: (title: string, message: string) => void;
  closeModal: () => void;
  openConfirm: (payload: ConfirmPayload) => void;
  openCampaignModal: (groupId?: number) => void;
  openAccountCreate: (loginNow?: boolean) => void;
  openAccountDetail: (account: Account) => Promise<void>;
  openAccountPoolDetail: (pool: AccountPool) => Promise<void>;
  refreshAccountPoolDetail: () => Promise<void>;
  createAccount: () => Promise<void>;
  createAccountPool: () => Promise<void>;
  moveCurrentAccountPool: (poolId: number) => Promise<void>;
  createClonePlan: () => Promise<void>;
  confirmClonePlan: (plan: AccountClonePlan) => Promise<void>;
  retryCloneItem: (item: AccountCloneItem) => Promise<void>;
  confirmVerificationTask: (task: VerificationTask) => Promise<void>;
  dismissVerificationTask: (task: VerificationTask) => Promise<void>;
  refreshAccountDetail: () => Promise<void>;
  syncAccountContacts: () => Promise<void>;
  queueAccountSyncNow: () => Promise<void>;
  startDirectMessageToContact: (contact: Contact) => void;
  openGroupDetail: (group: Group) => Promise<void>;
  loadCampaignDetail: (campaign: Campaign) => Promise<void>;
  openDraftEdit: (draft: Draft) => void;
  saveDraftEdit: () => Promise<void>;
  avatarUrl: (value: string) => string;
  openAccountProfileEdit: () => void;
  pollVerificationCodes: () => Promise<void>;
  toggleTargetGroup: (groupId: number) => void;
  recommendAccounts: (groupIds?: number[]) => Promise<void>;
  toggleRecommendedAccount: (groupId: number, accountId: number) => void;
  setGroupAccountsSelected: (groupId: number, accountIds: number[]) => void;
  goCampaignAccountStep: () => Promise<void>;
  goCampaignContentStep: () => void;
  createDirectMessageTask: () => Promise<void>;
  saveAccountProfile: () => Promise<void>;
  retryAccountProfileSync: () => Promise<void>;
  login: () => Promise<void>;
  register: () => Promise<void>;
  changePassword: () => Promise<void>;
  submitRedeemCode: () => Promise<void>;
  loadActivationCodes: (filters?: ActivationCodeFilters, page?: number, pageSize?: number) => Promise<void>;
  createActivationCodes: () => Promise<void>;
  disableActivationCode: (code: ActivationCode) => Promise<void>;
  logout: () => void;
  runLogin: (account: Account, method: 'code' | 'qr') => Promise<void>;
  verifyAccount: (account: Account) => Promise<void>;
  submitAccountLoginCode: () => Promise<void>;
  submitAccountLoginPassword: () => Promise<void>;
  resendAccountLoginCode: () => Promise<void>;
  healthCheck: (account: Account) => Promise<void>;
  syncAccountGroups: (account: Account) => Promise<void>;
  createCampaignAndDrafts: () => Promise<void>;
  approveDraft: (draft: Draft) => Promise<void>;
  rejectDraft: (draft: Draft) => Promise<void>;
  approveAllDrafts: () => Promise<void>;
  cancelTask: (task: MessageTask) => Promise<void>;
  dispatchTask: (task: MessageTask) => Promise<void>;
  drainQueue: () => Promise<void>;
  retryTask: (task: MessageTask) => Promise<void>;
  authorizeSelectedGroup: (status: string) => Promise<void>;
  createArchive: () => Promise<void>;
  saveGroupPolicy: () => Promise<void>;
  openArchiveDetail: (archive: ArchiveItem) => Promise<void>;
  exportArchive: (archive: ArchiveItem) => Promise<void>;
  createDeveloperApp: () => Promise<void>;
  openDeveloperAppEdit: (app: DeveloperApp) => void;
  toggleDeveloperApp: (app: DeveloperApp) => Promise<void>;
  checkDeveloperApp: (app: DeveloperApp) => Promise<void>;
  openTenantEdit: (tenant: Tenant) => void;
  saveTenantQuota: () => Promise<void>;
  createAiProvider: () => Promise<void>;
  openAiProviderEdit: (provider: AiProvider) => void;
  toggleAiProvider: (provider: AiProvider) => Promise<void>;
  checkAiProvider: (provider: AiProvider) => Promise<void>;
  saveTenantAiSetting: () => Promise<void>;
  saveSchedulingSetting: () => Promise<void>;
  createPromptTemplate: () => Promise<void>;
  createMaterial: () => Promise<void>;
  toggleMaterial: (materialId: number) => void;
  accountName: (accountId: number | null | undefined) => string;
  groupName: (groupId: number | null | undefined) => string;
  choosePoolSendAccount: (detail: AccountPoolDetail) => Account | undefined;
}
