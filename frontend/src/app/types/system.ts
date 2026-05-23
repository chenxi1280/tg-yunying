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
