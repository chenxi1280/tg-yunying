export type Overview = {
  totals: Record<string, number>;
  rates: Record<string, number>;
  queue: Record<string, number>;
  risks: Array<{ level: string; title: string; detail: string }>;
};

export type RuntimeConfig = {
  app_env: string;
  queue_backend: string;
  tg_gateway_mode: string;
  telethon_configured: boolean;
  sync_dispatch_fallback: boolean;
  code_ttl_seconds: number;
  developer_app_pool_enabled: boolean;
  developer_app_count: number;
  developer_app_healthy_count: number;
  can_create_tg_account: boolean;
  has_ai_provider: boolean;
  ai_enabled: boolean;
  ai_provider_count: number;
  healthy_ai_provider_count: number;
  mock_ai_fallback_enabled: boolean;
  avatar_max_bytes: number;
  avatar_allowed_types: string[];
  show_advanced_debug: boolean;
};

export type CurrentUser = {
  id: number;
  tenant_id: number | null;
  name: string;
  role: string;
  email: string;
  phone: string | null;
  tenant_name: string | null;
  can_use_core_features: boolean;
  subscription_status?: string;
  subscription_started_at?: string | null;
  subscription_expires_at?: string | null;
  subscription_days_remaining?: number;
  token_balance?: number;
  token_quota_total?: number;
  menu_permissions?: string[];
};

export type Tenant = {
  id: number;
  name: string;
  plan_name: string;
  account_quota: number;
  task_quota: number;
  admin_chat_id: string;
  notify_ai_failures_enabled: boolean;
  telegram_bot_configured: boolean;
  created_at: string;
};

export type CaptchaChallenge = {
  challenge_id: string;
  image_data_url: string;
  expires_at: string;
};

export type CaptchaVerifyResponse = {
  captcha_token: string;
  expires_at: string;
};

export type ActivationCode = {
  id: number;
  code: string;
  plan_id: number | null;
  plan_type: string;
  plan_name: string;
  duration_days: number;
  token_quota: number;
  status: string;
  batch_no: string;
  serial_prefix: string;
  created_by: string;
  created_at: string;
  redeemed_by_user_id: number | null;
  redeemed_user_name: string | null;
  redeemed_user_email: string | null;
  redeemed_at: string | null;
  subscription_start_at: string | null;
  subscription_end_at: string | null;
  note: string;
};

export type ActivationCodePage = {
  items: ActivationCode[];
  total: number;
  page: number;
  page_size: number;
};

export type ActivationCodeFilters = {
  search: string;
  status: string;
  plan_type: string;
  batch_no: string;
  start_at: string;
  end_at: string;
};

export type ActivationCodeCreateForm = {
  plan_type: string;
  plan_id: number | '';
  quantity: number;
  batch_no: string;
  serial_prefix: string;
  note: string;
};

export type SubscriptionPlan = {
  id: number;
  plan_type: string;
  name: string;
  duration_days: number;
  token_quota: number;
  is_active: boolean;
  note: string;
  created_at: string;
  updated_at: string;
};

export type SubscriptionPlanForm = {
  id: number | null;
  plan_type: string;
  name: string;
  duration_days: number;
  token_quota: number;
  is_active: boolean;
  note: string;
};

export type AdminUser = {
  id: number;
  tenant_id: number | null;
  tenant_name: string | null;
  name: string;
  role: string;
  email: string;
  phone: string | null;
  subscription_status: string;
  subscription_started_at: string | null;
  subscription_expires_at: string | null;
  subscription_days_remaining: number;
  token_balance: number;
  token_quota_total: number;
  menu_permissions: string[];
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
};

export type TokenLedger = {
  id: number;
  tenant_id: number | null;
  user_id: number;
  change_type: string;
  delta_tokens: number;
  balance_after: number;
  related_activation_code_id: number | null;
  related_ai_usage_ledger_id: number | null;
  reason: string;
  actor: string;
  created_at: string;
};

export type AdminUserForm = {
  id: number | null;
  name: string;
  email: string;
  phone: string;
  role: string;
  subscription_status: string;
  menu_permissions: string[];
  is_active: boolean;
};

export type UsageLedger = {
  id: number;
  tenant_id: number;
  user_id: number;
  campaign_id: number | null;
  group_id: number | null;
  provider_id: number | null;
  provider_name: string;
  model_name: string;
  prompt_template_id: number | null;
  request_type: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  input_unit_price: number;
  output_unit_price: number;
  total_cost: number;
  currency: string;
  billable: boolean;
  request_status: string;
  error_detail: string;
  created_at: string;
};

export type UsageSummary = {
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  billable_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost: number;
  currency: string;
  by_user: Array<{
    user_id: number;
    user_name: string;
    tenant_id: number;
    requests: number;
    total_tokens: number;
    total_cost: number;
    currency: string;
  }>;
};

export type LoginFlow = {
  id: number;
  account_id: number;
  method: string;
  status: string;
  code_preview: string | null;
  code_expires_at: string | null;
  qr_payload: string | null;
  created_at: string;
};

export type AccountLoginForm = {
  account: Account | null;
  step: 'method' | 'code' | 'qr' | 'password';
  method: 'code' | 'qr';
  code: string;
  password_2fa: string;
  flow: LoginFlow | null;
  error: string;
};

export type Account = {
  id: number;
  pool_id: number | null;
  pool_name: string;
  display_name: string;
  username: string | null;
  tg_first_name: string;
  tg_last_name: string;
  tg_bio: string;
  avatar_object_key: string;
  avatar_preview_url: string;
  profile_sync_status: string;
  profile_sync_error: string;
  profile_synced_at: string | null;
  phone_masked: string;
  phone_number: string | null;
  status: string;
  health_score: number;
  last_active_at: string | null;
  deleted_at?: string | null;
  deleted_by?: string;
  delete_reason?: string;
  developer_app_id: number | null;
  developer_app_name: string | null;
  developer_api_id: number | null;
  developer_app_health_status: string | null;
  developer_app_version: number;
};

export type AccountPool = {
  id: number;
  tenant_id: number;
  name: string;
  description: string;
  is_default: boolean;
  account_count: number;
};

export type DeveloperApp = {
  id: number;
  app_name: string;
  api_id: number;
  is_active: boolean;
  health_status: string;
  max_accounts: number;
  assigned_accounts: number;
  credentials_version: number;
  last_assigned_at: string | null;
  last_check_at: string | null;
  last_error: string;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type AiProvider = {
  id: number;
  provider_name: string;
  provider_type: string;
  base_url: string;
  model_name: string;
  api_key_header: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  currency: string;
  is_billable: boolean;
  is_active: boolean;
  health_status: string;
  last_check_at: string | null;
  last_error: string;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type PromptTemplate = {
  id: number;
  tenant_id: number | null;
  template_type: string;
  name: string;
  content: string;
  version: number;
  is_active: boolean;
};

export type TenantAiSetting = {
  id: number;
  tenant_id: number;
  default_provider_id: number | null;
  ai_enabled: boolean;
  fallback_to_mock: boolean;
  temperature: number;
  max_tokens: number;
};

export type SchedulingSetting = {
  id: number;
  tenant_id: number | null;
  jitter_min_seconds: number;
  jitter_max_seconds: number;
  batch_interval_seconds: number;
  respect_send_window: boolean;
};

export type Material = {
  id: number;
  title: string;
  material_type: string;
  content: string;
  tags: string;
  review_status: string;
  usage_count: number;
};

export type ContentKeywordRule = {
  id: number;
  tenant_id: number;
  keyword: string;
  match_type: string;
  is_active: boolean;
  note: string;
  created_at: string;
  updated_at: string;
};

export type Contact = {
  id: number;
  account_id: number;
  peer_id: string;
  display_name: string;
  username: string | null;
  phone_masked: string;
  contact_type: string;
  is_mutual: boolean;
  last_message_at: string | null;
  last_synced_at: string;
};

export type Group = {
  id: number;
  title: string;
  group_type: string;
  member_count: number;
  auth_status: string;
  can_send: boolean;
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
  listener_last_polled_at: string | null;
  listener_last_reply_at: string | null;
  listener_last_error: string;
  listener_account_ids: number[];
};

export type Campaign = {
  id: number;
  group_id: number;
  title: string;
  campaign_type: string;
  topic: string;
  execution_mode: string;
  send_window: string;
  intensity: string;
  ai_provider_id: number | null;
  prompt_template_id: number | null;
  jitter_min_seconds: number | null;
  jitter_max_seconds: number | null;
  batch_interval_seconds: number | null;
  respect_send_window: boolean | null;
  material_ids: string;
  target_group_ids: string;
  source_group_ids: string;
  selected_account_ids_by_group: string;
  run_interval_seconds: number;
  ends_at: string | null;
  max_ai_tokens: number | null;
  used_ai_tokens: number;
  last_run_at: string | null;
  next_run_at: string | null;
  consecutive_failure_count: number;
  last_error: string;
  participation_min_ratio: number;
  participation_max_ratio: number;
  max_messages_per_account: number;
  max_drafts_per_batch: number;
  filtered_count: number;
  status: string;
};

export type Draft = {
  id: number;
  campaign_id: number;
  group_id: number;
  persona: string;
  content: string;
  risk_level: string;
  provider_name: string;
  model_name: string;
  prompt_template_name: string;
  material_id: number | null;
  suggested_account_id: number | null;
  suggested_account_name?: string | null;
  sequence_index: number;
  reply_to_draft_id: number | null;
  generation_source: string;
  generation_error: string;
  status: string;
};

export type MessageTask = {
  id: number;
  campaign_id: number | null;
  group_id: number | null;
  account_id: number | null;
  draft_id: number | null;
  content: string;
  message_type: string;
  material_id: number | null;
  target_type: string;
  target_peer_id: string | null;
  target_display: string;
  preferred_account_id: number | null;
  preferred_account_name?: string | null;
  actual_account_changed: boolean;
  planned_delay_seconds: number;
  scheduled_at: string;
  status: string;
  failure_type: string | null;
  failure_detail: string | null;
  sent_at: string | null;
};

export type MessageSendTaskCreate = {
  account_id: number;
  target_type: 'private' | 'group' | 'channel';
  target_peer_id?: string | null;
  target_display?: string;
  group_id?: number | null;
  operation_target_id?: number | null;
  content: string;
  message_type: '文本' | '图片' | '表情包';
  material_id?: number | null;
  jitter_min_seconds: number;
  jitter_max_seconds: number;
  dispatch_now: boolean;
};

export type ArchiveItem = {
  id: number;
  group_id: number;
  collection_account_id?: number | null;
  title: string;
  status: string;
  sync_mode: string;
  failure_detail: string;
  message_count: number;
  member_count: number;
  summary: string;
  new_group_plan: string;
  started_at?: string | null;
  finished_at?: string | null;
  last_synced_at?: string | null;
};

export type ArchiveDetail = {
  archive: ArchiveItem;
  messages: Array<{ id: number; sender_name: string; sender_peer_id?: string; remote_message_id?: string; content: string; message_type: string; sent_at: string }>;
  members: Array<{ id: number; display_name: string; username: string | null; peer_id?: string; activity_score: number; tags: string; last_seen_at?: string | null }>;
  invite_candidates: Array<{ id: number; display_name: string; username: string | null; peer_id?: string; activity_score: number; tags: string; last_seen_at?: string | null }>;
};

export type ArchiveExport = ArchiveDetail & {
  export_format: string;
  generated_at: string;
  message_count: number;
  member_count: number;
};

export type AuditLog = {
  id: number;
  actor: string;
  action: string;
  target_type: string;
  detail: string;
  created_at: string;
};

export type VerificationCode = {
  id: number;
  account_id: number;
  source: string;
  code_preview: string | null;
  expires_at: string | null;
  viewed_by: string;
  viewed_at: string | null;
  status: string;
  raw_hint: string;
  created_at: string;
};

export type AccountSyncRecord = {
  id: number;
  account_id: number;
  sync_type: string;
  trigger_source: string;
  status: string;
  result_count: number;
  failure_type: string;
  failure_detail: string;
  scheduled_at: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type VerificationTask = {
  id: number;
  account_id: number | null;
  group_id: number | null;
  message_task_id: number | null;
  verification_type: string;
  detected_reason: string;
  suggested_action: string;
  target_peer_id: string;
  target_display: string;
  requires_user_confirm: boolean;
  status: string;
  failure_detail: string;
  created_at: string;
  handled_at: string | null;
};

export type AccountCloneItem = {
  id: number;
  plan_id: number;
  source_account_id: number;
  target_account_id: number;
  target_type: string;
  target_peer_id: string;
  target_display: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  created_at: string;
  executed_at: string | null;
};

export type AccountClonePlan = {
  id: number;
  source_account_id: number;
  target_account_id: number | null;
  target_account_ids: number[];
  target_accounts_summary: Array<{ id: number; display_name: string; status: string; items_total: number; items_done: number; items_failed: number }>;
  clone_scope: string;
  status: string;
  items_total: number;
  items_done: number;
  items_failed: number;
  failure_detail: string;
  created_by: string;
  created_at: string;
  confirmed_at: string | null;
  items: AccountCloneItem[];
  items_by_target: Record<string, AccountCloneItem[]>;
};

export type AccountGroup = Group & {
  permission_label: string;
  account_can_send: boolean;
  last_sent_at: string | null;
};

export type OperationTarget = {
  id: number;
  tenant_id: number;
  target_type: 'group' | 'channel';
  tg_peer_id: string;
  title: string;
  username: string;
  member_count: number;
  can_send: boolean;
  auth_status: string;
  last_sync_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ChannelMessage = {
  id: number;
  tenant_id: number;
  channel_target_id: number;
  message_id: number;
  message_url: string;
  content_preview: string;
  published_at: string | null;
  created_at: string;
};

export type OperationTask = {
  id: number;
  tenant_id: number;
  task_type: 'MESSAGE_SEND' | 'CHANNEL_VIEW' | 'CHANNEL_REACTION' | 'CHANNEL_REPLY';
  target_id: number | null;
  channel_message_id: number | null;
  title: string;
  content: string;
  reaction: string;
  account_ids: string;
  quantity: number;
  actual_quantity: number;
  quantity_jitter_ratio: number;
  content_mode: 'literal' | 'ai';
  completed_count: number;
  interval_seconds: number;
  status: string;
  failure_type: string;
  failure_detail: string;
  scheduled_at: string;
  executed_at: string | null;
  created_at: string;
};

export type OperationTaskAttempt = {
  id: number;
  tenant_id: number;
  task_id: number;
  account_id: number | null;
  action_type: string;
  content: string;
  reaction: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  remote_message_id: string;
  idempotency_key: string;
  planned_delay_seconds: number;
  scheduled_at: string;
  executed_at: string | null;
};

export type AccountRiskDiagnostic = {
  level: string;
  code: string;
  title: string;
  detail: string;
  source: string;
  action: string;
  occurred_at: string | null;
};

export type ManualOperationRecord = {
  id: number;
  tenant_id: number;
  account_id: number;
  target_id: number | null;
  operation_type: string;
  content: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  remote_message_id: string;
  actor: string;
  created_at: string;
};

export type ProfileSyncRecord = {
  id: number;
  account_id: number;
  actor: string;
  before_snapshot: string;
  after_snapshot: string;
  avatar_object_key: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  remote_detail: string;
  created_at: string;
  synced_at: string | null;
};

export type AccountDetail = {
  account: Account;
  risk_diagnostics: AccountRiskDiagnostic[];
  login_flows: LoginFlow[];
  verification_codes: VerificationCode[];
  profile_sync_records: ProfileSyncRecord[];
  sync_records: AccountSyncRecord[];
  next_sync_at: string | null;
  contacts: Contact[];
  groups: AccountGroup[];
  operation_targets: OperationTarget[];
  message_records: MessageTask[];
  manual_operation_records: ManualOperationRecord[];
  operation_task_attempts: OperationTaskAttempt[];
  clone_plans: AccountClonePlan[];
  verification_tasks: VerificationTask[];
  stats: Record<string, number>;
};

export type AccountPoolDetail = {
  pool: AccountPool;
  accounts: Account[];
  contacts: Contact[];
  verification_tasks: VerificationTask[];
  clone_plans: AccountClonePlan[];
  message_records: MessageTask[];
  stats: Record<string, number>;
};

export type GroupDetail = {
  group: Group;
  accounts: Array<{
    id: number;
    display_name: string;
    username: string | null;
    status: string;
    health_score: number;
    permission_label: string;
    can_send: boolean;
    is_listener?: boolean;
    last_sent_at: string | null;
  }>;
  listener_accounts: Array<{ id: number; display_name: string; username: string | null; status: string }>;
  recent_context_messages: Array<{
    id: number;
    listener_account_id: number;
    sender_name: string;
    content: string;
    message_type: string;
    sent_at: string | null;
    used_for_ai: boolean;
  }>;
  recent_campaigns: Campaign[];
  recent_archives: ArchiveItem[];
  verification_tasks: VerificationTask[];
  stats: Record<string, number>;
};

export type CampaignDetail = {
  campaign: Campaign;
  target_groups: Group[];
  selected_accounts_by_group: Record<string, Account[]>;
  drafts: Draft[];
  message_tasks: MessageTask[];
  stats: Record<string, number>;
};

export type RecommendedAccount = {
  group_id: number;
  group_title: string;
  account_id: number;
  account_name: string;
  username: string | null;
  health_score: number;
  can_send: boolean;
  is_selectable: boolean;
  unavailable_reason: string | null;
  cooldown_until: string | null;
  recommended: boolean;
  reason: string;
};

export type ConfirmPayload = {
  title: string;
  message: string;
  confirmLabel?: string;
  tone?: 'normal' | 'danger';
  restoreModalType?: 'accountDetail' | 'accountPoolDetail';
  onConfirm: () => void | Promise<void>;
};

export type ModalState =
  | { type: 'accountCreate' }
  | { type: 'accountLogin' }
  | { type: 'accountPoolCreate' }
  | { type: 'accountPoolDetail' }
  | { type: 'accountMovePool' }
  | { type: 'accountCloneCreate' }
  | { type: 'verificationTaskDetail'; payload: VerificationTask }
  | { type: 'developerAppCreate' }
  | { type: 'developerAppEdit' }
  | { type: 'tenantEdit' }
  | { type: 'subscriptionPlanCreate' }
  | { type: 'subscriptionPlanEdit' }
  | { type: 'adminUserEdit' }
  | { type: 'aiProviderCreate' }
  | { type: 'aiProviderEdit' }
  | { type: 'promptTemplateCreate' }
  | { type: 'materialCreate' }
  | { type: 'keywordRuleCreate' }
  | { type: 'keywordRuleEdit' }
  | { type: 'tenantAiEdit' }
  | { type: 'schedulingEdit' }
  | { type: 'changePassword' }
  | { type: 'groupPolicyEdit' }
  | { type: 'campaignCreate' }
  | { type: 'accountDetail' }
  | { type: 'groupDetail' }
  | { type: 'draftEdit' }
  | { type: 'accountProfileEdit' }
  | null;

export type BadgeTone = 'positive' | 'warning' | 'danger' | 'neutral' | 'muted';
