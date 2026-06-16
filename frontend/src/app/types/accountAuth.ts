export type AccountAuthorizationSummary = {
  primary_status: string;
  primary_source: string;
  standby_count: number;
  target_standby_count: number;
  has_standby: boolean;
  is_blocking: boolean;
  risk_hint: string;
};

export type AccountLatestLoginFlow = {
  method: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  trace_id: string;
  created_at: string | null;
};
