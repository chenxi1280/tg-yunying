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
  method: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  trace_id: string;
  created_at: string | null;
};
