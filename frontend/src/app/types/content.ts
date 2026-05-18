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
