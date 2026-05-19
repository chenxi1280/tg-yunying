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
  trusted_session_status: string;
  two_fa_status: string;
  external_authorization_count: number;
  security_profile_status: string;
  security_risk_reason: string;
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
