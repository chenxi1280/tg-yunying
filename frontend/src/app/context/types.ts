import React from 'react';
import type {
  Overview, RuntimeConfig, CurrentUser, CaptchaChallenge, CaptchaVerifyResponse,
  AdminUser, AdminUserForm,
  TokenLedger,
  UsageLedger, UsageSummary, LoginFlow, Account, AccountPool, AccountLoginForm,
  DeveloperApp, AiProvider, PromptTemplate, TenantAiSetting,
  Material, MaterialCacheConfig, MaterialCacheHealth, MaterialImportResult, ContentKeywordRule, Contact, Group, MessageTask, ArchiveItem, ArchiveDetail,
  ArchiveExport, AuditFilters, AuditLog, VerificationCode, AccountSyncRecord, VerificationTask, VerificationChallengeContext,
  AccountCloneItem, AccountClonePlan, AccountGroup, ProfileSyncRecord,
  AccountDetail, AccountPoolDetail, GroupDetail, Tenant,
  ConfirmPayload, MessageSendBatchCreate, MessageSendTaskCreate, ModalState,
} from '../types';

export type TenantForm = {
  id: number | null;
  name: string;
  plan_name: string;
  account_quota: number;
  task_quota: number;
};

export interface AppState {
  // Auth state
  token: string;
  setToken: (token: string) => void;
  currentUser: CurrentUser | null;
  setCurrentUser: (user: CurrentUser | null) => void;
  loginEmail: string;
  setLoginEmail: (email: string) => void;
  loginPassword: string;
  setLoginPassword: (password: string) => void;
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
  goToView: (viewId: string, search?: string) => void;

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
  adminUsers: AdminUser[];
  setAdminUsers: (users: AdminUser[]) => void;
  selectedAdminUserId: number | null;
  setSelectedAdminUserId: (id: number | null) => void;
  selectedUserTokenLedgers: TokenLedger[];
  setSelectedUserTokenLedgers: (ledgers: TokenLedger[]) => void;
  adminUserForm: AdminUserForm;
  setAdminUserForm: React.Dispatch<React.SetStateAction<AdminUserForm>>;
  tokenAdjustmentForm: { delta_tokens: number; reason: string };
  setTokenAdjustmentForm: React.Dispatch<React.SetStateAction<{ delta_tokens: number; reason: string }>>;

  // Usage
  usageLedgers: UsageLedger[];
  setUsageLedgers: (ledgers: UsageLedger[]) => void;
  usageSummary: UsageSummary | null;
  setUsageSummary: (summary: UsageSummary | null) => void;
  // AI & Templates
  aiProviders: AiProvider[];
  setAiProviders: (providers: AiProvider[]) => void;
  promptTemplates: PromptTemplate[];
  setPromptTemplates: (templates: PromptTemplate[]) => void;
  tenantAiSetting: TenantAiSetting | null;
  setTenantAiSetting: (setting: TenantAiSetting | null) => void;
  selectedAiProviderId: number | '';
  setSelectedAiProviderId: (id: number | '') => void;
  materials: Material[];
  setMaterials: (materials: Material[]) => void;
  materialCacheHealth: MaterialCacheHealth | null;
  setMaterialCacheHealth: (health: MaterialCacheHealth | null) => void;
  materialCacheConfig: MaterialCacheConfig | null;
  setMaterialCacheConfig: (config: MaterialCacheConfig | null) => void;
  materialImports: MaterialImportResult[];
  setMaterialImports: (imports: MaterialImportResult[]) => void;
  contentKeywordRules: ContentKeywordRule[];
  setContentKeywordRules: (rules: ContentKeywordRule[]) => void;

  // Groups & tasks
  groups: Group[];
  setGroups: (groups: Group[]) => void;
  tasks: MessageTask[];
  setTasks: (tasks: MessageTask[]) => void;
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
  auditFilters: AuditFilters;
  setAuditFilters: React.Dispatch<React.SetStateAction<AuditFilters>>;

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

  // Account forms
  accountCreateForm: { display_name: string; username: string; phone_number: string; pool_id: number | ''; login_method: 'code' | 'qr' };
  setAccountCreateForm: (form: { display_name: string; username: string; phone_number: string; pool_id: number | ''; login_method: 'code' | 'qr' }) => void;
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

  // Group state
  selectedGroupId: number | null;
  setSelectedGroupId: (id: number | null) => void;
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
  tenantForm: TenantForm;
  setTenantForm: (form: TenantForm) => void;
  aiProviderForm: { id: number | null; provider_name: string; base_url: string; model_name: string; api_key: string; api_key_header: string; notes: string; is_active: boolean };
  setAiProviderForm: (form: { id: number | null; provider_name: string; base_url: string; model_name: string; api_key: string; api_key_header: string; notes: string; is_active: boolean }) => void;
  promptTemplateForm: { id: number | null; name: string; template_type: string; content: string; is_active: boolean };
  setPromptTemplateForm: (form: { id: number | null; name: string; template_type: string; content: string; is_active: boolean }) => void;
  materialForm: {
    id: number | null;
    title: string;
    material_type: string;
    content: string;
    tags: string;
    emoji_asset_kind: string;
    cache_ready_status: string;
    delivery_mode: string;
    source_kind: string;
  };
  materialFile: File[] | null;
  setMaterialFile: (file: File[] | null) => void;
  setMaterialForm: React.Dispatch<React.SetStateAction<{
    id: number | null;
    title: string;
    material_type: string;
    content: string;
    tags: string;
    emoji_asset_kind: string;
    cache_ready_status: string;
    delivery_mode: string;
    source_kind: string;
  }>>;
  keywordRuleForm: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string };
  setKeywordRuleForm: (form: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string }) => void;

  // Modal & Dialog
  modal: ModalState;
  setModal: (modal: ModalState) => void;
  busy: string;
  setBusy: (busy: string) => void;
  pendingActionKeys: string[];
  isActionPending: (key: string) => boolean;
  notice: string;
  setNotice: (notice: string) => void;

  // Direct message
  directMessageForm: { target_peer_id: string; target_display: string; content: string };
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;

  // Computed values
  selectedPool: AccountPool | null;
  selectedGroup: Group | null;
  taskSummary: {
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
  openAccountCreate: (loginNow?: boolean) => void;
  openAccountDetail: (account: Account) => Promise<boolean>;
  openAccountVerificationCodes: (account: Account) => Promise<void>;
  openAccountMovePool: (account: Account) => Promise<void>;
  openAccountPoolDetail: (pool: AccountPool) => Promise<void>;
  refreshAccountPoolDetail: () => Promise<void>;
  createAccount: () => Promise<void>;
  createAccountPool: () => Promise<void>;
  moveCurrentAccountPool: (poolId: number) => Promise<void>;
  createClonePlan: () => Promise<void>;
  confirmClonePlan: (plan: AccountClonePlan) => Promise<void>;
  retryCloneItem: (item: AccountCloneItem) => Promise<void>;
  confirmVerificationTask: (task: VerificationTask) => Promise<void>;
  loadVerificationChallengeContext: (task: VerificationTask) => Promise<VerificationChallengeContext>;
  refreshVerificationChallengeContext: (task: VerificationTask) => Promise<VerificationChallengeContext>;
  resolveGroupRestrictionTask: (task: VerificationTask) => Promise<void>;
  resolveGroupRestrictionBatch: (task: VerificationTask) => Promise<void>;
  submitVerificationTaskResponse: (task: VerificationTask, responseText: string) => Promise<void>;
  dismissVerificationTask: (task: VerificationTask) => Promise<void>;
  refreshAccountDetail: () => Promise<void>;
  syncAccountContacts: () => Promise<void>;
  queueAccountSyncNow: () => Promise<void>;
  startDirectMessageToContact: (contact: Contact) => void;
  openGroupDetail: (group: Group) => Promise<boolean>;
  avatarUrl: (value: string) => string;
  openAccountProfileEdit: () => void;
  pollVerificationCodes: (reason: string) => Promise<void>;
  createDirectMessageTask: () => Promise<void>;
  createMessageSendTask: (payload: MessageSendTaskCreate | MessageSendBatchCreate) => Promise<MessageTask[]>;
  saveAccountProfile: () => Promise<void>;
  retryAccountProfileSync: () => Promise<void>;
  login: () => Promise<void>;
  changePassword: () => Promise<void>;
  logout: () => void;
  runLogin: (account: Account, method: 'code' | 'qr') => Promise<void>;
  verifyAccount: (account: Account) => Promise<void>;
  chooseAccountLoginMethod: (method: 'code' | 'qr') => Promise<void>;
  submitAccountLoginCode: () => Promise<void>;
  submitAccountLoginPassword: () => Promise<void>;
  resendAccountLoginCode: () => Promise<void>;
  checkAccountQrLogin: () => Promise<void>;
  deleteAccount: (account: Account) => Promise<void>;
  healthCheck: (account: Account) => Promise<void>;
  syncAccountGroups: (account: Account) => Promise<void>;
  cancelTask: (task: MessageTask) => Promise<void>;
  dispatchTask: (task: MessageTask) => Promise<void>;
  drainQueue: () => Promise<void>;
  retryTask: (task: MessageTask) => Promise<void>;
  authorizeSelectedGroup: (status: string) => Promise<void>;
  createArchive: () => Promise<void>;
  saveGroupPolicy: () => Promise<void>;
  openArchiveDetail: (archive: ArchiveItem) => Promise<boolean>;
  exportArchive: (archive: ArchiveItem) => Promise<void>;
  rerunArchive: (archive: ArchiveItem) => Promise<void>;
  createDeveloperApp: () => Promise<void>;
  openDeveloperAppEdit: (app: DeveloperApp) => void;
  toggleDeveloperApp: (app: DeveloperApp) => Promise<void>;
  checkDeveloperApp: (app: DeveloperApp) => Promise<void>;
  openTenantEdit: (tenant: Tenant) => void;
  saveTenantQuota: () => Promise<void>;
  saveTenantGroupRescueSettings: (tenantId: number, payload: {
    group_rescue_enabled: boolean;
    group_rescue_admin_account_id: number | null;
  }) => Promise<void>;
  openAdminUserEdit: (user: AdminUser) => void;
  openAdminUserCreate: () => void;
  saveAdminUser: () => Promise<void>;
  resetAdminUserPassword: (user: AdminUser, newPassword: string) => Promise<void>;
  adjustAdminUserTokens: (user: AdminUser) => Promise<void>;
  loadUserTokenLedgers: (userId: number) => Promise<void>;
  createAiProvider: () => Promise<void>;
  openAiProviderEdit: (provider: AiProvider) => void;
  toggleAiProvider: (provider: AiProvider) => Promise<void>;
  checkAiProvider: (provider: AiProvider) => Promise<void>;
  saveTenantAiSetting: () => Promise<void>;
  createPromptTemplate: () => Promise<void>;
  openPromptTemplateEdit: (template: PromptTemplate) => void;
  savePromptTemplate: () => Promise<void>;
  createMaterial: () => Promise<void>;
  disableMaterial: (material: Material) => Promise<void>;
  openMaterialEdit: (material: Material) => void;
  restoreMaterial: (material: Material) => Promise<void>;
  saveMaterial: () => Promise<void>;
  createContentKeywordRule: () => Promise<void>;
  openContentKeywordRuleEdit: (rule: ContentKeywordRule) => void;
  saveContentKeywordRule: () => Promise<void>;
  accountName: (accountId: number | null | undefined) => string;
  groupName: (groupId: number | null | undefined) => string;
  choosePoolSendAccount: (detail: AccountPoolDetail) => Account | undefined;
}
