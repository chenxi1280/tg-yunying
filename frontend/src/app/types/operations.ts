import type { TaskCenterTaskType } from './taskCenter';

export type OperationTarget = {
  id: number;
  tenant_id: number;
  target_type: 'group' | 'channel';
  tg_peer_id: string;
  title: string;
  username: string;
  member_count: number;
  can_send: boolean;
  auth_status: string;
  linked_group_id: number | null;
  can_listen: boolean;
  can_archive: boolean;
  can_task: boolean;
  task_capabilities: string[];
  available_send_account_count: number;
  listener_account_count: number;
  last_sync_at: string | null;
  created_at: string;
  updated_at: string;
};

export type OperationPlanTarget = {
  id: number;
  tenant_id: number;
  plan_id: number;
  target_id: number;
  status: string;
  strategy_config: Record<string, any>;
  created_at: string;
  updated_at: string;
};

export type OperationPlanTaskLink = {
  id: number;
  tenant_id: number;
  plan_id: number;
  target_id: number | null;
  task_id: string;
  relation: string;
  status: string;
  created_at: string;
};

export type OperationPlanGenerationRun = {
  id: string;
  tenant_id: number;
  plan_id: number;
  run_type: string;
  status: string;
  requested_by: string;
  request_payload: Record<string, any>;
  result_payload: Record<string, any>;
  failure_detail: string;
  created_at: string;
  finished_at: string | null;
};

export type OperationPlan = {
  id: number;
  tenant_id: number;
  name: string;
  description: string;
  target_type: string;
  status: string;
  strategy_config: Record<string, any>;
  task_blueprints: Array<Record<string, any>>;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  targets: OperationPlanTarget[];
  task_links: OperationPlanTaskLink[];
  latest_run: OperationPlanGenerationRun | null;
};

export type OperationPlanPreview = {
  plan_id: number;
  target_count: number;
  estimated_task_count: number;
  estimated_target_count: number;
  account_capacity: Record<string, any>;
  admission_actions: Array<Record<string, any>>;
  target_previews: Array<Record<string, any>>;
  planned_tasks: Array<Record<string, any>>;
  blockers: string[];
  warnings: string[];
  run: OperationPlanGenerationRun;
};

export type OperationPlanGenerateResult = {
  plan_id: number;
  created_task_ids: string[];
  linked_task_count: number;
  run: OperationPlanGenerationRun;
};

export type OperationPlanApplyResult = {
  plan_id: number;
  created_task_ids: string[];
  linked_task_count: number;
  applied_task_ids: string[];
  requires_confirmation: boolean;
  impact_preview: Record<string, any>;
  run: OperationPlanGenerationRun;
};

export type TargetRuntimeSummary = {
  id: string;
  tenant_id: number;
  target_id: number;
  status: string;
  open_issue_count: number;
  failed_action_count: number;
  affected_task_count: number;
  latest_failure_at: string | null;
  summary: Record<string, any>;
  updated_at: string;
};

export type OperationCenterSummary = {
  tenant_id: number;
  open_issue_count: number;
  affected_target_count: number;
  running_task_count: number;
  failed_action_count: number;
  affected_account_count: number;
  latest_updated_at: string | null;
  stale: boolean;
};

export type OperationIssue = {
  id: string;
  tenant_id: number;
  target_id: number | null;
  issue_type: string;
  severity: string;
  source_task_id: string;
  representative_action_id: string;
  affected_task_count: number;
  affected_account_count: number;
  affected_account_ids: number[];
  failure_type: string;
  failure_reason: string;
  suggested_action: string;
  handling_mode: 'modal' | 'drawer' | 'deep_link' | string;
  return_to: Record<string, any>;
  claimed_by: string;
  claimed_at: string | null;
  status: string;
  summary: Record<string, any>;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  updated_at: string;
};

export type OperationIssueDetail = {
  issue: OperationIssue;
  target: Record<string, any> | null;
  source_task: Record<string, any> | null;
  related_task_summary: Record<string, any> | null;
  sources: Array<Record<string, any>>;
  issue_accounts: Array<Record<string, any>>;
  affected_accounts: Array<Record<string, any>>;
  recent_failed_actions: Array<Record<string, any>>;
};

export type ChannelMessage = {
  id: number;
  tenant_id: number;
  channel_target_id: number;
  message_id: number;
  message_url: string;
  content_preview: string;
  published_at: string | null;
  created_at: string;
};

export type ChannelMessageComment = {
  id: number;
  tenant_id: number;
  channel_target_id: number;
  channel_message_id: number;
  comment_message_id: number;
  parent_comment_message_id: number | null;
  author_peer_id: string;
  author_name: string;
  content_preview: string;
  reply_count: number;
  published_at: string | null;
  created_at: string;
};

export type ChannelMessageCommentSync = {
  inserted: number;
  comments: ChannelMessageComment[];
  sync_error: string;
};

export type OperationTargetDetail = {
  target: OperationTarget;
  linked_group: {
    id: number;
    title: string;
    group_type: string;
    member_count: number;
    auth_status: string;
    can_send: boolean;
    active_window: string;
    daily_limit: number;
    account_cooldown_seconds: number;
    group_cooldown_seconds: number;
    banned_words: string;
    link_whitelist: string;
    require_review: boolean;
    listener_enabled: boolean;
    listener_context_limit: number;
    listener_last_error: string;
  } | null;
  accounts: Array<{
    id: number;
    display_name: string;
    username: string | null;
    status: string;
    health_score: number;
    permission_label: string;
    can_send: boolean;
    is_listener: boolean;
    last_sent_at: string | null;
  }>;
  group_messages: Array<{
    id: number;
    listener_account_id: number;
    sender_name: string;
    content: string;
    message_type: string;
    sent_at: string | null;
    used_for_ai: boolean;
  }>;
  channel_messages: ChannelMessage[];
  channel_comments: ChannelMessageComment[];
  task_history: Array<{
    id: string;
    name: string;
    type: TaskCenterTaskType;
    status: string;
    success_count: number;
    failure_count: number;
    updated_at: string;
  }>;
  send_records: Array<{
    id: number;
    content: string;
    status: string;
    account_id: number | null;
    failure_detail: string;
    sent_at: string | null;
    created_at: string;
  }>;
  archive_records: Array<{
    id: number;
    title: string;
    status: string;
    message_count: number;
    member_count: number;
    failure_detail: string;
    created_at: string;
  }>;
  risk: {
    level: string;
    messages: string[];
  };
  sync_error: string;
  stats: Record<string, number>;
};

export type OperationTargetMessageSync = {
  inserted: number;
  detail: OperationTargetDetail;
};

export type OperationTargetsSync = {
  synced_accounts: number;
  failed_accounts: Array<{
    account_id: number;
    display_name: string;
    error: string;
  }>;
  target_count: number;
  targets: OperationTarget[];
};

export type MessageSendingPrefill = {
  target: OperationTarget;
  nonce: number;
};

export type TaskCenterPrefill = {
  taskType: Extract<TaskCenterTaskType, 'group_ai_chat' | 'group_relay' | 'channel_view' | 'channel_like' | 'channel_comment'>;
  target: OperationTarget;
  message?: ChannelMessage;
  nonce: number;
};

export type OperationTask = {
  id: number;
  tenant_id: number;
  task_type: 'MESSAGE_SEND' | 'CHANNEL_VIEW' | 'CHANNEL_REACTION' | 'CHANNEL_REPLY';
  target_id: number | null;
  channel_message_id: number | null;
  title: string;
  content: string;
  reaction: string;
  account_ids: string;
  quantity: number;
  actual_quantity: number;
  quantity_jitter_ratio: number;
  content_mode: 'literal' | 'ai';
  completed_count: number;
  interval_seconds: number;
  status: string;
  failure_type: string;
  failure_detail: string;
  scheduled_at: string;
  executed_at: string | null;
  created_at: string;
};

export type OperationTaskAttempt = {
  id: number;
  tenant_id: number;
  task_id: number;
  account_id: number | null;
  action_type: string;
  content: string;
  reaction: string;
  status: string;
  failure_type: string;
  failure_detail: string;
  remote_message_id: string;
  idempotency_key: string;
  planned_delay_seconds: number;
  scheduled_at: string;
  executed_at: string | null;
};

export type MetricBucket = {
  key: string;
  label: string;
  value: number | string;
  detail: string;
  status: string;
};

export type OperationMetricDetail = {
  key: string;
  title: string;
  category: string;
  status: string;
  detail: string;
  related_id: string;
  occurred_at: string | null;
};

export type OperationMetricsSummary = {
  accounts: MetricBucket[];
  targets: MetricBucket[];
  messages: MetricBucket[];
  channel_interactions: MetricBucket[];
  ai_activity: MetricBucket[];
  relay: MetricBucket[];
  archives: MetricBucket[];
  ai_usage: MetricBucket[];
  failures: MetricBucket[];
  risk_control: MetricBucket[];
  account_details: OperationMetricDetail[];
  target_details: OperationMetricDetail[];
  task_details: OperationMetricDetail[];
  failure_details: OperationMetricDetail[];
  risk_details: OperationMetricDetail[];
};

export type ChannelCapacityCheck = {
  effective_account_count: number;
  target_per_message: number;
  max_effective_per_message: number;
  will_shortfall: boolean;
  warning_message: string;
};
