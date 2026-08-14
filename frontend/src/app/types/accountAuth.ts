export type AccountAuthorizationSummary = {
  primary_status: string;
  primary_source: string;
  standby_count: number;
  target_standby_count: number;
  has_standby: boolean;
  is_blocking: boolean;
  risk_hint: string;
  slot_statuses: Record<string, string>;
  aggregate_status: string;
  healthy_slot_count: number;
  can_rescue: boolean;
};

export type AccountLatestLoginFlow = {
  id: number | null;
  method: string;
  status: string;
  authorization_status: string;
  post_login_sync_status: string;
  pool_transition_status: string;
  security_post_login_status: string;
  failure_type: string;
  failure_detail: string;
  trace_id: string;
  created_at: string | null;
};
