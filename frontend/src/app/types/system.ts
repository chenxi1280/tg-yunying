export type Overview = {
  totals: Record<string, number>;
  rates: Record<string, number>;
  queue: Record<string, number>;
  risks: Array<{ level: string; title: string; detail: string }>;
  operation_center?: {
    tenant_id: number;
    open_issue_count: number;
    affected_target_count: number;
    running_task_count: number;
    failed_action_count: number;
    affected_account_count: number;
    latest_updated_at: string | null;
    stale: boolean;
  } | null;
  activity_24h?: Array<{
    hour: string;
    sent_messages: number;
    likes: number;
    comments: number;
    success: number;
    failed: number;
    total: number;
    success_rate: number;
    failure_rate: number;
  }>;
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
  ai_group_bot_enabled?: boolean;
  telegram_bot_webhook_status?: string;
  telegram_bot_last_error?: string;
  group_rescue_enabled: boolean;
  group_rescue_admin_account_id: number | null;
  created_at: string;
};

export type TenantBotSettings = {
  tenant_id: number;
  admin_chat_id: string;
  telegram_bot_configured: boolean;
  telegram_bot_token_configured: boolean;
  telegram_bot_token_preview: string;
  telegram_bot_token: null;
  ai_group_bot_enabled: boolean;
  telegram_bot_webhook_secret: string;
  telegram_bot_webhook_url: string;
  telegram_bot_webhook_current_url: string;
  telegram_bot_webhook_last_checked_at: string | null;
  telegram_bot_webhook_status: string;
  telegram_bot_last_error: string;
  notify_ai_failures_enabled: boolean;
};

export type AiAccountVoiceProfile = {
  account_id: number;
  display_name: string;
  username: string;
  phone_masked: string;
  account_status: string;
  profile_status: string;
  version: number;
  mask_name: string;
  audience_archetype: string;
  identity_frame: string;
  preference_tags: string[];
  age_band: string;
  persona_experiences: string[];
  consumption_experiences: string[];
  sentence_length: string;
  interaction_habits: string[];
  tone_strength: string;
  lexical_preferences: string[];
  emoji_policy: string;
  forbidden_expressions: string[];
  short_prompt_summary: string;
  quality_status: string;
  similarity_score: number | null;
  updated_by: string;
  updated_at: string | null;
};

export type AiAccountVoiceProfileBatchRebuildOut = {
  created: number;
  skipped: number;
  items: AiAccountVoiceProfileBatchResultItem[];
};

export type AiAccountVoiceProfileBatchResultItem = {
  account_id: number;
  status: string;
  version: number;
  similarity_score: number | null;
  failure_reason: string;
  skipped_reason: string;
};

export type AiAccountVoiceProfileBatchStatusOut = {
  updated: number;
  skipped: number;
};

export type AiAccountVoiceProfileVersion = {
  version: number;
  status: string;
  source: string;
  mask_name: string;
  audience_archetype: string;
  identity_frame: string;
  preference_tags: string[];
  age_band: string;
  sentence_length: string;
  tone_strength: string;
  emoji_policy: string;
  short_prompt_summary: string;
  quality_status: string;
  similarity_score: number | null;
  updated_by: string;
  updated_at: string | null;
};

export type AiAccountVoiceProfileAudit = {
  id: number;
  actor: string;
  action: string;
  detail: string;
  created_at: string | null;
};

export type ProxyAirportSubscription = {
  id: number | null;
  tenant_id: number;
  name: string;
  subscription_url_configured: boolean;
  subscription_url_preview: string;
  provider_type: string;
  priority: number;
  enabled: boolean;
  failover_policy: string;
  auto_failback_enabled: boolean;
  failback_cooldown_minutes: number;
  all_subscriptions_down_policy: string;
  notify_admin_on_all_subscriptions_down: boolean;
  sync_status: string;
  node_count: number;
  healthy_node_count: number;
  last_sync_at: string | null;
  last_error: string;
  updated_at: string | null;
};

export type AccountEnvironmentBinding = {
  id: string | null;
  account_id: number;
  account_display_name: string;
  account_username: string;
  phone_masked: string;
  account_status: string;
  developer_app_id: number | null;
  developer_app_name: string;
  developer_app_api_id_snapshot: number;
  authorization_id: number | null;
  session_role: string;
  authorization_status: string;
  proxy_id: number | null;
  proxy_name: string;
  proxy_status: string;
  device_model: string;
  system_version: string;
  app_version: string;
  platform: string;
  observed_device_model: string;
  observed_system_version: string;
  observed_app_version: string;
  observed_api_id: number;
  observed_missing_fields: string[];
  lang_code: string;
  system_lang_code: string;
  lang_pack: string;
  region_code: string;
  client_identity_key: string;
  consistency_status: string;
  effect_boundary: string;
  updated_at: string | null;
};

export type AccountEnvironmentProxyBatchBindResult = {
  success_count: number;
  failed_count: number;
  skipped_accounts: Array<{ account_id: number; reason: string }>;
  affected_account_ids: number[];
  trace_id: string;
};

export type ProxyAirportNode = {
  id: number;
  tenant_id: number;
  subscription_id: number;
  node_name: string;
  protocol: string;
  proxy_host: string;
  proxy_port: number;
  status: string;
  observed_exit_ip: string;
  observed_exit_country: string;
  observed_exit_asn: string;
  observed_exit_isp: string;
  updated_at: string | null;
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
  password: string;
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
