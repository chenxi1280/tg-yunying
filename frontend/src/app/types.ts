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
  role_template: string;
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
  permissions?: string[];
  permission_version?: number;
  is_active?: boolean;
  is_super_admin?: boolean;
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

export type AdminUser = {
  id: number;
  tenant_id: number | null;
  tenant_name: string | null;
  name: string;
  role: string;
  role_template: string;
  email: string;
  phone: string | null;
  subscription_status: string;
  subscription_started_at: string | null;
  subscription_expires_at: string | null;
  subscription_days_remaining: number;
  token_balance: number;
  token_quota_total: number;
  menu_permissions: string[];
  permissions: string[];
  permission_version: number;
  is_super_admin: boolean;
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
  role_template: string;
  subscription_status: string;
  menu_permissions: string[];
  permissions: string[];
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
  proxy_id: number | null;
  proxy_name: string | null;
  proxy_local_address: string | null;
  proxy_status: string | null;
  proxy_alert_status: string | null;
};

export type RiskControlMetric = {
  key: string;
  label: string;
  value: number | string;
  detail: string;
  status: string;
};

export type RiskControlAccountScore = {
  account_id: number;
  display_name: string;
  username: string | null;
  phone_masked: string;
  pool_name: string;
  login_status: string;
  health_score: number;
  risk_level: string;
  current_policy: string;
  hour_usage: number;
  hour_limit: number;
  day_usage: number;
  day_limit: number;
  cooldown_until: string | null;
  recent_risk: string;
  blocked_reason: string;
  score_reasons: string[];
  proxy_id: number | null;
  proxy_name: string | null;
  proxy_local_address: string | null;
  proxy_status: string | null;
  proxy_alert_status: string | null;
  proxy_risk_reason: string;
  can_join_task: boolean;
};

export type RiskDispositionItem = {
  key: string;
  item_type: string;
  severity: string;
  account_id: number | null;
  account_name: string;
  target: string;
  reason: string;
  suggested_action: string;
  occurred_at: string | null;
  status: string;
};

export type RiskHitRecord = {
  key: string;
  source: string;
  severity: string;
  account_id: number | null;
  account_name: string;
  task_id: string;
  target: string;
  policy: string;
  action: string;
  detail: string;
  occurred_at: string | null;
};

export type RiskProxyAlert = {
  id: number | null;
  proxy_id: number | null;
  name: string;
  local_address: string;
  alert_status: string;
  severity: string;
  alert_type: string;
  reason_code: string;
  bound_accounts: number;
  last_error: string;
  suggested_action: string;
  occurred_at: string | null;
};

export type AccountProxy = {
  id: number;
  tenant_id: number;
  name: string;
  protocol: string;
  host: string;
  port: number;
  username: string;
  status: string;
  alert_status: string;
  check_interval_seconds: number;
  timeout_ms: number;
  max_bound_accounts: number;
  max_concurrent_sessions: number;
  last_check_at: string | null;
  last_error: string;
  disabled_reason: string;
  notes: string;
  local_address: string;
  bound_account_count: number;
  created_at: string;
  updated_at: string;
  trace_id?: string;
};

export type RiskPreflight = {
  decision: 'allow' | 'warn' | 'block';
  available_accounts: Array<Record<string, any>>;
  limited_accounts: Array<Record<string, any>>;
  blocked_accounts: Array<Record<string, any>>;
  proxy_decisions: Array<Record<string, any>>;
  proxy_warnings: string[];
  proxy_alerts: Array<Record<string, any>>;
  target_warnings: string[];
  content_warnings: string[];
  suggested_actions: string[];
  decision_reasons: string[];
  expires_at: string;
  trace_id: string;
};

export type RiskControlSummary = {
  overview: {
    current_level: string;
    level_detail: string;
    quiet_active: boolean;
    metrics: RiskControlMetric[];
  };
  global_policy: {
    jitter_min_seconds: number;
    jitter_max_seconds: number;
    batch_interval_seconds: number;
    respect_send_window: boolean;
    quiet_hours_enabled: boolean;
    quiet_start: string;
    quiet_end: string;
    quiet_timezone: string;
    default_max_retries: number;
    default_retry_delay_seconds: number;
    default_retry_backoff: string;
    default_on_account_banned: string;
    default_on_api_rate_limit: string;
    default_on_content_rejected: string;
    default_account_hour_limit: number;
    default_account_day_limit: number;
    default_account_cooldown_seconds: number;
    updated_at: string;
  };
  account_scores: RiskControlAccountScore[];
  disposition_queue: RiskDispositionItem[];
  hit_records: RiskHitRecord[];
  proxy_alerts: RiskProxyAlert[];
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
  quiet_hours_enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  quiet_timezone: string;
  default_max_retries: number;
  default_retry_delay_seconds: number;
  default_retry_backoff: 'none' | 'linear' | 'exponential';
  default_on_account_banned: 'skip_account' | 'pause_task' | 'stop_task';
  default_on_api_rate_limit: 'wait_and_retry' | 'skip' | 'pause';
  default_on_content_rejected: 'skip_message' | 'rewrite_and_retry' | 'pause';
  default_account_hour_limit: number;
  default_account_day_limit: number;
  default_account_cooldown_seconds: number;
};

export type Material = {
  id: number;
  tenant_id: number;
  title: string;
  material_type: string;
  content: string;
  tags: string;
  review_status: string;
  source_kind: string;
  asset_fingerprint: string;
  asset_version_id: number;
  delivery_mode: string;
  emoji_asset_kind: string;
  gateway_type: string;
  cache_ready_status: string;
  last_cache_flood_wait_until: string | null;
  tg_cache_account_id: number | null;
  tg_cache_peer_id: string;
  tg_cache_message_id: string;
  tg_ref_version_id: number;
  file_name: string;
  mime_type: string;
  file_size: number;
  width: number;
  height: number;
  caption: string;
  last_cache_error: string;
  usage_count: number;
  last_used_at: string | null;
};

export type MaterialCacheStatusCount = {
  status: string;
  count: number;
};

export type MaterialCacheErrorItem = {
  scope: string;
  id: string;
  title: string;
  status: string;
  reason: string;
};

export type MaterialCacheHealth = {
  material_cache_peer_configured: boolean;
  source_media_cache_peer_configured: boolean;
  active_cache_account_count: number;
  material_status_counts: MaterialCacheStatusCount[];
  source_media_status_counts: MaterialCacheStatusCount[];
  material_oldest_pending_at: string | null;
  source_media_oldest_pending_at: string | null;
  flood_wait_count: number;
  cache_failed_count: number;
  waiting_action_count: number;
  recent_errors: MaterialCacheErrorItem[];
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
  tg_peer_id: string;
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
  media_sent: boolean | null;
  media_failure_reason: string;
  sent_at: string | null;
};

export type MessageType = '文本' | '图片' | '表情包' | '文件' | '链接' | '组合消息';

export type MessageSendTaskCreate = {
  account_id: number;
  target_type: 'private' | 'group' | 'channel';
  target_peer_id?: string | null;
  target_display?: string;
  group_id?: number | null;
  operation_target_id?: number | null;
  content: string;
  message_type: MessageType;
  material_id?: number | null;
  jitter_min_seconds: number;
  jitter_max_seconds: number;
  dispatch_now: boolean;
  scheduled_at?: string | null;
};

export type MessageSendTarget = {
  target_type: 'private' | 'group' | 'channel';
  target_peer_id?: string | null;
  target_display?: string;
  group_id?: number | null;
  operation_target_id?: number | null;
};

export type MessageSendBatchCreate = {
  account_id: number;
  targets: MessageSendTarget[];
  content: string;
  message_type: MessageType;
  material_id?: number | null;
  dispatch_now: boolean;
  scheduled_at?: string | null;
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
  tenant_id: number | null;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: string;
  ip_address: string;
  created_at: string;
};

export type AuditFilters = {
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  keyword: string;
  account_id: string;
  operation_target_id: string;
  task_id: string;
  status: string;
  start_at: string;
  end_at: string;
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
  tenant_id: number;
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
  issue_scope: 'account' | 'target' | string;
  issue_category: 'account_restricted' | 'group_restriction' | 'verification' | string;
  can_auto_resolve: boolean;
  requires_target_recheck: boolean;
  resolution_entry_label: string;
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
  linked_group_id: number | null;
  can_listen: boolean;
  can_archive: boolean;
  can_task: boolean;
  task_capabilities: string[];
  available_send_account_count: number;
  listener_account_count: number;
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

export type ChannelMessageComment = {
  id: number;
  tenant_id: number;
  channel_target_id: number;
  channel_message_id: number;
  comment_message_id: number;
  parent_comment_message_id: number | null;
  author_peer_id: string;
  author_name: string;
  content_preview: string;
  reply_count: number;
  published_at: string | null;
  created_at: string;
};

export type ChannelMessageCommentSync = {
  inserted: number;
  comments: ChannelMessageComment[];
  sync_error: string;
};

export type OperationTargetDetail = {
  target: OperationTarget;
  linked_group: {
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
    banned_words: string;
    link_whitelist: string;
    require_review: boolean;
    listener_enabled: boolean;
    listener_context_limit: number;
    listener_last_error: string;
  } | null;
  accounts: Array<{
    id: number;
    display_name: string;
    username: string | null;
    status: string;
    health_score: number;
    permission_label: string;
    can_send: boolean;
    is_listener: boolean;
    last_sent_at: string | null;
  }>;
  group_messages: Array<{
    id: number;
    listener_account_id: number;
    sender_name: string;
    content: string;
    message_type: string;
    sent_at: string | null;
    used_for_ai: boolean;
  }>;
  channel_messages: ChannelMessage[];
  channel_comments: ChannelMessageComment[];
  task_history: Array<{
    id: string;
    name: string;
    type: TaskCenterTaskType;
    status: string;
    success_count: number;
    failure_count: number;
    updated_at: string;
  }>;
  send_records: Array<{
    id: number;
    content: string;
    status: string;
    account_id: number | null;
    failure_detail: string;
    sent_at: string | null;
    created_at: string;
  }>;
  archive_records: Array<{
    id: number;
    title: string;
    status: string;
    message_count: number;
    member_count: number;
    failure_detail: string;
    created_at: string;
  }>;
  risk: {
    level: string;
    messages: string[];
  };
  sync_error: string;
  stats: Record<string, number>;
};

export type OperationTargetMessageSync = {
  inserted: number;
  detail: OperationTargetDetail;
};

export type OperationTargetsSync = {
  synced_accounts: number;
  failed_accounts: Array<{
    account_id: number;
    display_name: string;
    error: string;
  }>;
  target_count: number;
  targets: OperationTarget[];
};

export type MessageSendingPrefill = {
  target: OperationTarget;
  nonce: number;
};

export type TaskCenterPrefill = {
  taskType: Extract<TaskCenterTaskType, 'group_ai_chat' | 'group_relay' | 'channel_view' | 'channel_like' | 'channel_comment'>;
  target: OperationTarget;
  message?: ChannelMessage;
  nonce: number;
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

export type TaskCenterTaskType = 'group_ai_chat' | 'group_relay' | 'channel_view' | 'channel_like' | 'channel_comment';

export type TaskCenterTask = {
  id: string;
  tenant_id: number;
  name: string;
  type: TaskCenterTaskType;
  status: string;
  priority: number;
  timezone: string;
  scheduled_start: string | null;
  scheduled_end: string | null;
  max_duration_hours: number | null;
  next_run_at: string | null;
  last_error: string;
  account_config: Record<string, any>;
  pacing_config: Record<string, any>;
  failure_policy: Record<string, any>;
  type_config: Record<string, any>;
  stats: Record<string, any>;
  target_summary?: string;
  search_text?: string;
  created_at: string;
  updated_at: string;
};

export type TaskCenterAction = {
  id: string;
  tenant_id: number;
  task_id: string;
  task_type: string;
  action_type: string;
  account_id: number | null;
  scheduled_at: string;
  executed_at: string | null;
  status: string;
  payload: Record<string, any>;
  result: Record<string, any>;
  retry_count: number;
  created_at: string;
};

export type RuleSetVersion = {
  id: number;
  tenant_id: number;
  rule_set_id: number;
  version: number;
  status: string;
  version_note: string;
  filters: Record<string, any>;
  output_checks: Record<string, any>;
  transforms: Record<string, any>;
  routing: Record<string, any>;
  account_strategy: Record<string, any>;
  rate_limits: Record<string, any>;
  retry_policy: Record<string, any>;
  created_by: string;
  published_by: string;
  published_at: string | null;
  created_at: string;
  updated_at: string;
};

export type RuleSet = {
  id: number;
  tenant_id: number;
  name: string;
  description: string;
  status: string;
  task_types: string[];
  default_policy: Record<string, any>;
  active_version_id: number | null;
  versions: RuleSetVersion[];
  created_at: string;
  updated_at: string;
};

export type RuleSetBoundTask = {
  id: string;
  name: string;
  type: string;
  status: string;
  binding_mode: string;
  rule_set_id: number | null;
  rule_set_version_id: number | null;
  resolved_rule_set_version_id: number | null;
  created_at: string;
  updated_at: string;
};

export type TaskCenterDetail = {
  task: TaskCenterTask;
  actions: TaskCenterAction[];
  stats: Record<string, any>;
  accounts: Array<{ id: number; display_name: string; username: string | null; status: string }>;
  message_groups: Array<{
    channel_target_id: number | null;
    channel_title: string;
    channel_username: string;
    message_id: number | null;
    action_type: string;
    action_label: string;
    message_url: string;
    content_preview: string;
    target_count: number;
    completed_count: number;
    failed_count: number;
    running_count: number;
    skipped_count: number;
    duplicate_count: number;
    capacity_shortfall: number;
    subtask_status: string;
    stats: Record<string, any>;
    actions: TaskCenterAction[];
  }>;
  ai_cycles: Array<{
    cycle_id: string;
    context_message_ids: number[];
    stats: Record<string, any>;
    turns: Array<{
      action_id: string;
      turn_index: number;
      account_id: number | null;
      account_role: string;
      account_memory: string;
      account_profile: string;
      topic_thread: string;
      topic_plan: string;
      intent: string;
      content: string;
      status: string;
      scheduled_at: string;
      executed_at: string | null;
      result: Record<string, any>;
    }>;
  }>;
  ai_generation_records: Array<{
    generation_id: string;
    cycle_id: string;
    status: string;
    generated_count: number;
    token_count: number;
    context_message_count: number;
    account_memory_count: number;
    scheduled_at: string | null;
    created_at: string | null;
  }>;
  ai_account_profiles: Array<{
    account_id: number;
    display_name: string;
    username: string | null;
    status: string;
    total_success_count: number;
    current_task_success_count: number;
    cross_task_success_count: number;
    profile_summary: string;
  }>;
  relay_batches: Array<{
    relay_batch_id: string;
    stats: Record<string, any>;
    source_event_count: number;
    material_count: number;
    rule_version_count: number;
    items: Array<{
      action_id: string;
      relay_event_id: string;
      source_event_key: string;
      source_group_id: number | null;
      source_operation_target_id: number | null;
      operation_target_id: number | null;
      source_info: string;
      source_group_title: string;
      source_sender_name: string;
      source_sender_peer_id: string;
      source_remote_message_id: string;
      source_message_type: string;
      source_sent_at: string | null;
      target_display: string;
      original_text: string;
      transformed_text: string;
      material_fingerprint: string;
      rule_set_id: number | null;
      rule_set_name: string;
      rule_set_version_id: number | null;
      resolved_rule_set_version_id?: number | null;
      rule_set_version: number | null;
      rule_binding_mode?: string;
      rule_trace: Record<string, any>;
      account_id: number | null;
      status: string;
      retry_count: number;
      scheduled_at: string;
      executed_at: string | null;
      result: Record<string, any>;
    }>;
  }>;
};

export type MetricBucket = {
  key: string;
  label: string;
  value: number | string;
  detail: string;
  status: string;
};

export type OperationMetricDetail = {
  key: string;
  title: string;
  category: string;
  status: string;
  detail: string;
  related_id: string;
  occurred_at: string | null;
};

export type OperationMetricsSummary = {
  accounts: MetricBucket[];
  targets: MetricBucket[];
  messages: MetricBucket[];
  channel_interactions: MetricBucket[];
  ai_activity: MetricBucket[];
  relay: MetricBucket[];
  archives: MetricBucket[];
  ai_usage: MetricBucket[];
  failures: MetricBucket[];
  risk_control: MetricBucket[];
  account_details: OperationMetricDetail[];
  target_details: OperationMetricDetail[];
  task_details: OperationMetricDetail[];
  failure_details: OperationMetricDetail[];
  risk_details: OperationMetricDetail[];
};

export type ChannelCapacityCheck = {
  effective_account_count: number;
  target_per_message: number;
  max_effective_per_message: number;
  will_shortfall: boolean;
  warning_message: string;
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
  sync_due: boolean;
  sync_status_text: string;
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
  recent_campaigns: Array<Record<string, unknown>>;
  recent_archives: ArchiveItem[];
  verification_tasks: VerificationTask[];
  stats: Record<string, number>;
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
  | { type: 'adminUserEdit' }
  | { type: 'aiProviderCreate' }
  | { type: 'aiProviderEdit' }
  | { type: 'promptTemplateCreate' }
  | { type: 'promptTemplateEdit' }
  | { type: 'materialCreate' }
  | { type: 'materialEdit' }
  | { type: 'keywordRuleCreate' }
  | { type: 'keywordRuleEdit' }
  | { type: 'tenantAiEdit' }
  | { type: 'changePassword' }
  | { type: 'groupPolicyEdit' }
  | { type: 'accountDetail' }
  | { type: 'groupDetail' }
  | { type: 'draftEdit' }
  | { type: 'accountProfileEdit' }
  | null;

export type BadgeTone = 'positive' | 'warning' | 'danger' | 'neutral' | 'muted';
