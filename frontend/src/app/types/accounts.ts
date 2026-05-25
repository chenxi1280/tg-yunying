import type { ArchiveItem } from './archives';
import type { Contact, Group, MessageTask } from './messaging';
import type { OperationTarget, OperationTaskAttempt } from './operations';

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

export type AccountAvailabilitySummary = {
  id: string;
  tenant_id: number;
  account_id: number;
  send_available: boolean;
  listen_available: boolean;
  join_available: boolean;
  comment_available: boolean;
  profile_available: boolean;
  code_read_available: boolean;
  remaining_capacity: number;
  unavailable_reason: string;
  next_retry_at: string | null;
  failure_trend: Record<string, number>;
  health_score: number;
  risk_level: string;
  score_reasons: string[];
  non_score_reasons: string[];
  updated_at: string;
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

export type AccountSecurityPreviewItem = {
  account_id: number;
  account_name: string;
  phone_masked: string;
  phone_number?: string | null;
  session_status: string;
  trusted_session_status: string;
  external_authorization_count: number;
  two_fa_status: string;
  profile_status: string;
  generated_display_name: string;
  generated_first_name: string;
  generated_last_name: string;
  generated_bio: string;
  username_candidates: string[];
  avatar_source: string;
  precheck_status: string;
  blockers: string[];
  warnings: string[];
  suggested_actions: string[];
};

export type AccountSecurityPrecheck = {
  batch_preview_id: string;
  summary: Record<string, number>;
  items: AccountSecurityPreviewItem[];
  action_types: string[];
  trace_id: string;
};

export type AccountSecurityBatchItem = {
  id: number;
  batch_id: number;
  tenant_id: number;
  account_id: number;
  status: string;
  precheck_status: string;
  cleanup_status: string;
  two_fa_status: string;
  profile_status: string;
  username_status: string;
  avatar_status: string;
  external_devices_before: number;
  external_devices_after: number;
  generated_display_name: string;
  generated_first_name: string;
  generated_last_name: string;
  generated_bio: string;
  generated_username: string;
  username_candidates: string[];
  avatar_source: string;
  skipped_reason: string;
  failure_type: string;
  failure_detail: string;
  next_retry_at: string | null;
  trace_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type AccountSecurityBatch = {
  id: number;
  tenant_id: number;
  action_types: string[];
  status: string;
  total_count: number;
  success_count: number;
  skipped_count: number;
  failed_count: number;
  created_by: string;
  confirmed_by: string;
  confirm_text: string;
  password_strategy: string;
  profile_strategy: Record<string, unknown>;
  username_strategy: Record<string, unknown>;
  avatar_strategy: Record<string, unknown>;
  overwrite_existing_profile: boolean;
  reason: string;
  trace_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  items: AccountSecurityBatchItem[];
};

export type AccountAuthorizationSnapshot = {
  id: number;
  account_id: number;
  batch_id: number | null;
  authorization_hash_ciphertext: string;
  is_platform_trusted: boolean;
  is_current_session: boolean;
  device_model: string;
  platform: string;
  system_version: string;
  api_id: number;
  app_name: string;
  app_version: string;
  ip_masked: string;
  country: string;
  region: string;
  date_created: string | null;
  date_active: string | null;
  status: string;
  scanned_at: string;
};

export type AccountSecuritySnapshot = {
  id: number;
  account_id: number;
  trusted_session_status: string;
  two_fa_status: string;
  external_authorization_count: number;
  last_device_scan_at: string | null;
  last_2fa_check_at: string | null;
  profile_status: string;
  profile_last_updated_at: string | null;
  trusted_device_label: string;
  last_hardened_at: string | null;
  last_error: string;
  trace_id: string;
  created_at: string;
  updated_at: string;
};

export type AccountSecurityDetail = {
  account_id: number;
  snapshot: AccountSecuritySnapshot;
  authorizations: AccountAuthorizationSnapshot[];
  recent_batches: AccountSecurityBatch[];
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
