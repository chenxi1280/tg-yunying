export type AccountBatchLoginCapability = {
  mode: 'off' | 'reconcile_only' | 'enabled';
  post_login_init_mode: 'off' | 'reconcile_only' | 'enabled';
  max_lines: number;
  item_deadline_seconds: number;
  code_wait_seconds: number;
  poll_interval_seconds: number;
  worker_concurrency: number;
  post_login_init_worker_concurrency: number;
  readiness: boolean;
  blockers: string[];
};

export type AccountBatchLoginPreviewItem = {
  line_no: number;
  phone_masked: string;
  route_hint: 'create' | 'new_account' | 'existing_probe_required';
  account_id: number | null;
  current_pool_id: number | null;
  code_source_note: string;
  binding_action: 'bind' | 'keep' | 'replace_required';
  current_binding_note: string;
  current_binding_version: number;
};

export type AccountBatchLoginPreview = {
  preview_token: string;
  preview_fingerprint: string;
  expires_at: string;
  total_count: number;
  create_count: number;
  existing_probe_required_count: number;
  migrate_count: number;
  queue_position: number;
  estimated_seconds: number;
  worst_case_seconds: number;
  credential_expires_at: string;
  items: AccountBatchLoginPreviewItem[];
};

export type AccountBatchLoginItem = {
  id: number;
  batch_id: number;
  line_no: number;
  phone_masked: string;
  code_source_note: string;
  route_hint: string;
  route: string;
  account_id: number | null;
  initialization_policy: string;
  authorization_status: string;
  post_initialization_id: number | null;
  post_initialization_status: string;
  post_initialization_failure_type: string;
  status: string;
  phase: string;
  failure_type: string;
  failure_detail: string;
  warning_detail: string;
  current_attempt_id: number | null;
  current_attempt_state_version: number;
  reconcile_status: string;
  reconcile_attempted: boolean;
  account_binding_version: number;
  execution_generation: number;
  retry_count: number;
  state_version: number;
  next_retry_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type AccountBatchLogin = {
  id: number;
  tenant_id: number;
  pool_id: number;
  created_by: string;
  status: string;
  state_version: number;
  execution_generation: number;
  resolution_version: number;
  total_count: number;
  success_count: number;
  failed_count: number;
  unresolved_count: number;
  warning_count: number;
  skipped_count: number;
  authorized_count: number;
  fully_initialized_count: number;
  post_init_waiting_count: number;
  manual_required_count: number;
  initialization_policy: string;
  reason: string;
  trace_id: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  items?: AccountBatchLoginItem[];
};

export type AccountPostLoginInitialization = {
  id: number;
  account_id: number;
  generation: number;
  predecessor_initialization_id: number | null;
  target_pool_id: number;
  policy_version: string;
  status: string;
  stage: string;
  source_two_fa_kind: string;
  two_fa_status: string;
  two_fa_call_state: string;
  two_fa_evidence_present: boolean;
  profile_status: string;
  profile_batch_id: number | null;
  profile_action_types: string[];
  profile_evidence_present: boolean;
  abc_status: string;
  abc_batch_id: string;
  abc_evidence_present: boolean;
  abc_request_id: number | null;
  abc_request_status: string;
  failure_type: string;
  failure_detail: string;
  execution_owner: string;
  version: number;
  next_retry_at: string | null;
  two_fa_next_retry_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
};

export type PostLoginAbcRequest = {
  id: number;
  tenant_id: number;
  account_id: number;
  full_initialization_id: number;
  status: string;
  request_version: number;
  requested_by: string;
  approved_by: string;
  approval_ref: string;
  deployed_release_sha: string;
  preview_fingerprint: string;
  abc_batch_id: string;
  failure_type: string;
  failure_detail: string;
  created_at: string;
  approved_at: string | null;
  finished_at: string | null;
};

export type PostLoginAbcPreview = {
  request_id: number;
  request_version: number;
  account_id: number;
  deployed_release_sha: string;
  fingerprint: string;
  classification_counts: Record<string, number>;
};

export type AccountBatchLoginNoticeItem = {
  line_no: number;
  phone_masked: string;
  reason: string;
};

export type AccountBatchLoginNotification = {
  id: number;
  batch_id: number;
  execution_generation: number;
  resolution_version: number;
  event_type: 'initial' | 'correction';
  channel: 'platform';
  summary: {
    batch_id: number;
    status: string;
    counts: Record<string, number>;
    failed: AccountBatchLoginNoticeItem[];
    unresolved: AccountBatchLoginNoticeItem[];
    warning: AccountBatchLoginNoticeItem[];
    corrections?: Array<{
      line_no: number;
      phone_masked: string;
      from_status: string;
      to_status: string;
      reason: string;
    }>;
    tg_bot_delivery?: 'dead_letter';
  };
  delivery_status: string;
  acknowledged_at: string | null;
  state_version: number;
  created_at: string;
};
