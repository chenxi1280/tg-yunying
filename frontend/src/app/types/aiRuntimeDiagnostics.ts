export interface AIRuntimeDiagnostics {
  generation_outcomes?: {
    scope: { task_lifecycle_epoch: number; since: string | null; unit: string };
    outcome_counts: Record<string, number>;
    latest_job_state_stage_counts: Record<string, number>;
    work_count: number;
  };
  original_deadline_queue?: {
    status: string;
    ledger_id?: string;
    original_deadline_at?: string;
    state_counts: Record<string, number>;
  };
}
