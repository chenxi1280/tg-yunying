export type AccountBatchLoginCapability = {
  mode: 'off' | 'reconcile_only' | 'enabled';
  max_lines: number;
  item_deadline_seconds: number;
  code_wait_seconds: number;
  poll_interval_seconds: number;
  worker_concurrency: number;
  readiness: boolean;
  blockers: string[];
};

export type AccountBatchLoginPreviewItem = {
  line_no: number;
  phone_masked: string;
  route_hint: 'create' | 'existing_probe_required';
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
  reason: string;
  trace_id: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  items?: AccountBatchLoginItem[];
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
