import { ApiError } from '../../shared/api/client';
import type { Account, AccountPool, OperationTarget, RuleSet, SchedulingSetting, TaskCenterAction, TaskCenterDetail, TaskCenterTaskType } from '../types';
import { formatBeijingDateTime, toBeijingDateTimeLocalValue } from '../time';
export { runtimeStage, runtimeStageLabel, statusLabel } from './taskRuntimeStage';

export const TASK_TYPES: Array<{ value: TaskCenterTaskType; label: string }> = [
  { value: 'group_ai_chat', label: 'AI 活跃群' },
  { value: 'group_relay', label: '转发监听群' },
  { value: 'channel_view', label: '频道消息浏览' },
  { value: 'channel_like', label: '频道消息点赞' },
  { value: 'channel_comment', label: '频道消息评论/回复' },
];

export const TYPE_LABEL: Record<string, string> = Object.fromEntries(TASK_TYPES.map((item) => [item.value, item.label]));
TYPE_LABEL.account_profile_init = '资料初始化批次';

export const CREATE_ENDPOINT: Record<TaskCenterTaskType, string> = {
  group_ai_chat: '/tasks/group-ai-chat',
  group_relay: '/tasks/group-relay',
  channel_view: '/tasks/channel-view',
  channel_like: '/tasks/channel-like',
  channel_comment: '/tasks/channel-comment',
};

export const CREATE_AND_START_ENDPOINT: Record<TaskCenterTaskType, string> = {
  group_ai_chat: '/tasks/group-ai-chat/create-and-start',
  group_relay: '/tasks/group-relay/create-and-start',
  channel_view: '/tasks/channel-view/create-and-start',
  channel_like: '/tasks/channel-like/create-and-start',
  channel_comment: '/tasks/channel-comment/create-and-start',
};

export const WIZARD_STEPS = ['基础信息', '目标来源', '任务配置', '账号与节奏', '预检确认'];

export const OPERATION_PROFILE_TEMPLATES = [
  { value: 'natural_full_day', label: '全天自然活跃', curve: [10, 8, 5, 5, 0, 0, 8, 15, 35, 45, 55, 60, 45, 40, 55, 65, 70, 75, 80, 85, 70, 50, 25, 15] },
  { value: 'evening_peak', label: '晚间高峰', curve: [5, 5, 3, 3, 0, 0, 5, 10, 20, 25, 30, 35, 35, 30, 35, 40, 45, 55, 65, 85, 90, 90, 75, 45] },
  { value: 'workday_double_peak', label: '工作日双峰', curve: [5, 5, 3, 3, 0, 0, 8, 18, 30, 45, 75, 80, 45, 35, 45, 75, 85, 80, 55, 35, 25, 18, 10, 8] },
  { value: 'event_warmup', label: '活动预热', curve: [5, 5, 3, 3, 0, 0, 8, 12, 20, 25, 30, 35, 40, 45, 55, 65, 75, 85, 95, 100, 85, 65, 35, 15] },
  { value: 'conservative', label: '低打扰保守', curve: [5, 5, 3, 3, 0, 0, 5, 8, 12, 18, 22, 25, 20, 18, 22, 25, 28, 30, 28, 25, 18, 12, 8, 5] },
];

export type OperationProfileTemplateId = typeof OPERATION_PROFILE_TEMPLATES[number]['value'];

export const ACTION_LABEL: Record<string, string> = {
  send_message: '发送消息',
  like_message: '点赞',
  post_comment: '评论',
  view_message: '浏览',
};

const PRECHECK_REASON_LABELS: Record<string, string> = {
  account_blocked: '账号不可用',
  account_limited: '账号受限',
  account_limit: '账号容量已达上限',
  account_login_required: '账号需要重新登录',
  account_missing: '账号不存在或不可见',
  no_available_account: '没有可用账号',
  target_warning: '目标权限或授权存在风险',
  content_warning: '内容命中风控提示',
  proxy_missing: '代理未配置',
  proxy_alert_active: '代理告警',
  proxy_disabled: '代理禁用',
  proxy_unreachable: '代理不可达',
  proxy_timeout: '代理超时',
  proxy_auth_failed: '代理认证失败',
};

export function precheckReasonLabel(reason: string) {
  return PRECHECK_REASON_LABELS[reason] ?? reason;
}

export function formatPrecheckReasons(reasons: string[], limit = 5) {
  return reasons.filter(Boolean).slice(0, limit).map(precheckReasonLabel).join('；');
}

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 408) {
      return '请求超时，服务可能仍在处理，已刷新任务列表，请确认是否已创建后再重试。';
    }
    try {
      const parsed = JSON.parse(error.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
      if (parsed.detail && typeof parsed.detail === 'object' && !Array.isArray(parsed.detail)) {
        const detail = parsed.detail as Record<string, unknown>;
        const message = String(detail.message ?? detail.failure_detail ?? error.body);
        const traceId = String(detail.trace_id ?? '');
        return traceId ? `${message}（trace_id: ${traceId}）` : message;
      }
      if (Array.isArray(parsed.detail)) {
        return parsed.detail.map((item: any) => {
          const path = Array.isArray(item.loc) ? item.loc.join('.') : String(item.loc ?? '');
          const message = item.msg ?? JSON.stringify(item);
          return path ? `${path}: ${message}` : message;
        }).join('；');
      }
    } catch {
      return error.body || error.message;
    }
    return error.body || error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

export function words(value?: string): string[] {
  return (value ?? '').split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
}

export function parseKeyValueMap(value?: string | Record<string, string>): Record<string, string> {
  if (!value) return {};
  if (typeof value === 'object') return value;
  return value
    .split(/\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .reduce<Record<string, string>>((result, line) => {
      const [rawKey, ...rawValue] = line.split(/[:：=]/);
      const key = rawKey?.trim();
      const role = rawValue.join('=').trim();
      if (key && role) result[key] = role;
      return result;
    }, {});
}

export function formatKeyValueMap(value?: Record<string, string>): string {
  return value ? Object.entries(value).map(([key, role]) => `${key}=${role}`).join('\n') : '';
}

export function normalizePromptTemplateType(value?: string): string {
  return (value ?? '').replace(/\s+/g, '');
}

export function csvNumbers(value?: Array<number | string> | string | null): number[] {
  if (Array.isArray(value)) return value.map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0);
  if (typeof value === 'string') {
    return value
      .split(/[,，\n\s]+/)
      .map((item) => Number(item.trim()))
      .filter((item) => Number.isFinite(item) && item > 0);
  }
  return [];
}

export function curveNumbers(value?: Array<number | string> | string | null): number[] {
  const raw = Array.isArray(value) ? value : String(value ?? '').split(/[,，\n\s]+/);
  const curve = raw
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item))
    .slice(0, 24)
    .map((item) => Math.min(100, Math.max(0, Math.round(item))));
  const fallback = OPERATION_PROFILE_TEMPLATES[0].curve;
  return Array.from({ length: 24 }, (_, index) => curve[index] ?? fallback[index] ?? 0);
}

export function curveText(curve: number[]): string {
  return curve.join(',');
}

export function operationTemplate(id?: string | null) {
  return OPERATION_PROFILE_TEMPLATES.find((item) => item.value === id) ?? OPERATION_PROFILE_TEMPLATES[0];
}

export function operationProfileFromValues(values: any) {
  const template = operationTemplate(values.operation_template_id);
  const curve = curveNumbers(values.hourly_activity_curve ?? template.curve);
  return {
    template_id: template.value,
    source: values.operation_profile_manual_override ? 'manual' : 'built_in_default',
    hourly_activity_curve: curve,
    quiet_threshold: values.quiet_threshold ?? 20,
    peak_threshold: values.peak_threshold ?? 70,
    manual_override: Boolean(values.operation_profile_manual_override),
  };
}

export function operationProfileSummary(values: Record<string, any>): string {
  const curve = curveNumbers(values.hourly_activity_curve ?? operationTemplate(values.operation_template_id).curve);
  const quietThreshold = values.quiet_threshold ?? 20;
  const peakThreshold = values.peak_threshold ?? 70;
  const quietHours = curve.map((value, hour) => (value > 0 && value <= quietThreshold ? hour : null)).filter((item) => item !== null);
  const sleepHours = curve.map((value, hour) => (value === 0 ? hour : null)).filter((item) => item !== null);
  const peakHours = curve.map((value, hour) => (value >= peakThreshold ? hour : null)).filter((item) => item !== null);
  return `高峰 ${peakHours.length || 0} 小时，低频 ${quietHours.length || 0} 小时，休眠 ${sleepHours.length || 0} 小时`;
}

export function currentOperationProfile(values: Record<string, any>) {
  const profile = values.pacing_config?.operation_profile ?? operationProfileFromValues(values);
  const curve = curveNumbers(profile.hourly_activity_curve);
  const hour = new Date().getHours();
  const intensity = curve[hour] ?? 0;
  const quietThreshold = profile.quiet_threshold ?? 20;
  const peakThreshold = profile.peak_threshold ?? 70;
  const mode = intensity <= 0 ? '休眠' : intensity <= quietThreshold ? '低频' : intensity >= peakThreshold ? '高峰' : '常规';
  return { curve, hour, intensity, mode };
}

export function accountPrecheck(values: Record<string, any>, accounts: Account[], accountPools: AccountPool[]) {
  const mode = values.selection_mode ?? values.account_config?.selection_mode ?? 'all';
  const ids = csvNumbers(values.account_ids ?? values.account_config?.account_ids);
  const poolId = values.account_group_id ?? values.account_config?.account_group_id;
  const candidates = mode === 'manual'
    ? accounts.filter((account) => ids.includes(account.id))
    : mode === 'group'
      ? accounts.filter((account) => account.pool_id === poolId)
      : accounts;
  const online = candidates.filter((account) => account.status === '在线');
  const pool = accountPools.find((item) => item.id === poolId);
  return {
    label: mode === 'manual' ? '手动账号' : mode === 'group' ? `账号分组 ${pool?.name ?? poolId ?? '-'}` : '全部账号',
    total: candidates.length,
    online: online.length,
    limited: Math.max(0, candidates.length - online.length),
  };
}

export function ruleSummary(values: Record<string, any>, ruleSets: RuleSet[]) {
  const versionId = Number(values.rule_set_version_id || 0);
  const ruleSetId = Number(values.rule_set_id || 0);
  const ruleSet = ruleSets.find((item) => item.id === ruleSetId || item.versions.some((version) => version.id === versionId));
  const version = ruleSet?.versions.find((item) => item.id === versionId || item.id === ruleSet.active_version_id);
  if (!ruleSet && !versionId) return '系统默认当前发布版本';
  return [ruleSet?.name ?? (ruleSetId ? `规则集 #${ruleSetId}` : ''), version ? `v${version.version} / ${version.status === 'published' ? '已发布' : version.status}` : '当前发布版本'].filter(Boolean).join(' / ') || '-';
}

export function targetName(values: Record<string, any>, targets: OperationTarget[]) {
  const targetId = Number(values.target_operation_target_id || values.target_channel_id || 0);
  const target = targets.find((item) => item.id === targetId);
  return target ? `${target.title} #${target.id}` : targetId ? `#${targetId}` : '-';
}

export function formatDateTime(value?: string | null): string {
  return formatBeijingDateTime(value);
}

export function toDateTimeLocal(value?: string | null): string | undefined {
  return toBeijingDateTimeLocalValue(value);
}

export function actionLabel(value: string): string {
  return ACTION_LABEL[value] ?? value;
}

export function actionStatusLabel(value?: string | null): string {
  if (value === 'pending') return '待执行';
  if (value === 'claiming') return '认领中';
  if (['running', 'executing'].includes(value ?? '')) return '执行中';
  if (value === 'retryable_failed') return '待重试';
  if (value === 'unknown_after_send') return '结果未知';
  if (value === 'skipped') return '已跳过';
  if (['completed', 'success'].includes(value ?? '')) return '已完成';
  if (['failed', 'rejected', 'expired'].includes(value ?? '')) return '失败';
  return value || '待执行';
}

export function accountDisplay(detail: TaskCenterDetail | null, accountId?: number | null): string {
  if (!accountId) return '-';
  const account = detail?.accounts.find((item) => item.id === accountId);
  if (!account) return `账号 #${accountId}`;
  return account.username ? `${account.display_name} / @${account.username}` : account.display_name;
}

export function actionTarget(action: TaskCenterAction): string {
  return action.payload?.target_display ?? action.payload?.channel_id ?? action.payload?.chat_id ?? '-';
}

export function actionContent(action: TaskCenterAction): string {
  return action.payload?.message_text ?? action.payload?.comment_text ?? action.payload?.message_content ?? action.payload?.reaction_emoji ?? '-';
}

export function actionResult(action: TaskCenterAction): string {
  if (action.result?.error_message) {
    const prefix = action.result?.auto_check ? `自动校验：${action.result.auto_check} / ` : '';
    return `${prefix}${action.result.error_message}`;
  }
  if (action.result?.telegram_msg_id) return `消息ID ${action.result.telegram_msg_id}`;
  if (action.result?.success === true) return '成功';
  if (action.result?.success === false) return '失败';
  return '-';
}

export function isPlannedAction(action: TaskCenterAction): boolean {
  return ['pending', 'executing'].includes(action.status) && !action.executed_at;
}

export function commonInitialValues(setting?: SchedulingSetting | null) {
  const template = operationTemplate('natural_full_day');
  return {
    priority: 3,
    timezone: 'Asia/Shanghai',
    selection_mode: 'all',
    max_concurrent: 20,
    cooldown_per_account_minutes: 5,
    ban_policy: 'skip',
    pacing_mode: 'template',
    template: 'gentle_24h',
    operation_template_id: template.value,
    hourly_activity_curve: curveText(template.curve),
    operation_profile_manual_override: false,
    quiet_threshold: 20,
    peak_threshold: 70,
    max_retries: setting?.default_max_retries ?? 3,
    retry_delay_seconds: setting?.default_retry_delay_seconds ?? 60,
    retry_backoff: setting?.default_retry_backoff ?? 'exponential',
    on_account_banned: setting?.default_on_account_banned ?? 'skip_account',
    on_api_rate_limit: setting?.default_on_api_rate_limit ?? 'wait_and_retry',
    on_content_rejected: setting?.default_on_content_rejected ?? 'skip_message',
  };
}

export function typeInitialValues(type: TaskCenterTaskType, setting?: SchedulingSetting | null) {
  if (type === 'group_ai_chat') {
    return {
      participation_rate: 0.6,
      allow_account_repeat: true,
      repeat_cooldown_rounds: 2,
      chat_history_depth: 50,
      messages_per_round_mode: 'auto',
      context_expire_after_messages: 10,
      idle_continuation_enabled: true,
      idle_continuation_seconds: 300,
      account_memory_depth: 3,
      account_personas: '',
      slang_prompt_template_id: null,
      tone: 'auto',
      language: 'zh-CN',
    };
  }
  if (type === 'group_relay') {
    return {
      content_mode: 'light_rewrite',
      filter_bot_messages: true,
      filter_admin_messages: false,
      excluded_sender_peer_ids: [],
      excluded_sender_input: '',
      dedup_window_minutes: 60,
      dedup_method: 'hash',
    };
  }
  if (type === 'channel_view') {
    return {
      message_scope: 'dynamic_new',
      message_count: 10,
      target_views_per_message: 50,
      listen_new_messages: true,
      per_message_daily_view_target: 50,
      per_message_total_view_target: 300,
      message_active_days: 3,
      task_daily_view_safety_cap: 500,
      max_views_per_account_per_day: 20,
      execution_mode: 'distribute',
    };
  }
  if (type === 'channel_like') {
    return {
      message_scope: 'dynamic_new',
      message_count: 10,
      target_likes_per_message: 50,
      reaction_type: 'random',
      allowed_reactions: '👍,❤️,🔥',
      max_likes_per_account_per_hour: 10,
    };
  }
  return {
    message_scope: 'dynamic_new',
    message_count: 10,
    comment_mode: 'comment',
    reply_to_message_ids: '',
    language: 'zh-CN',
    comment_style: 'mixed',
  };
}

export function initialValuesForType(type: TaskCenterTaskType, setting?: SchedulingSetting | null) {
  return { ...commonInitialValues(setting), ...typeInitialValues(type, setting) };
}

export function defaultRuleSelection(ruleSets: RuleSet[], taskType: TaskCenterTaskType): { rule_set_id: number; rule_set_version_id: number } | null {
  const ruleSet = ruleSets.find((item) => (item.task_types ?? []).includes(taskType))
    ?? (taskType === 'group_relay' ? ruleSets.find((item) => item.name === '默认运营规则集' || item.name === '默认转发监听过滤规则') : null)
    ?? ruleSets[0];
  if (!ruleSet) return null;
  const version = ruleSet.versions.find((item) => item.id === ruleSet.active_version_id && item.status === 'published')
    ?? ruleSet.versions.find((item) => item.status === 'published');
  if (!version) return null;
  return { rule_set_id: ruleSet.id, rule_set_version_id: version.id };
}


export function fieldsForStep(step: number, taskType: TaskCenterTaskType, messageScope: string, accountMode: string): string[] {
  if (step === 0) return ['name'];
  if (step === 1) {
    if (taskType === 'group_ai_chat') return ['target_operation_target_id'];
    if (taskType === 'group_relay') return ['source_operation_target_ids', 'target_operation_target_id'];
    const fields = ['target_channel_id', 'message_scope'];
    if (['latest_n', 'dynamic_new'].includes(messageScope)) fields.push('message_count');
    if (messageScope === 'specific') fields.push('message_ids');
    if (messageScope === 'date_range') fields.push('date_from', 'date_to');
    return fields;
  }
  if (step === 3) return accountSelectionFields(accountMode);
  return [];
}

export function accountSelectionFields(accountMode: string): string[] {
  const fields = ['selection_mode'];
  if (accountMode === 'group') fields.push('account_group_id');
  if (accountMode === 'manual') fields.push('account_ids');
  return fields;
}

export function accountFields(accountMode: string): string[] {
  const fields = ['selection_mode', 'max_concurrent', 'cooldown_per_account_minutes', 'ban_policy'];
  if (accountMode === 'group') fields.push('account_group_id');
  if (accountMode === 'manual') fields.push('account_ids');
  return fields;
}

export function channelScopeFields(messageScope: string): string[] {
  const fields = ['target_channel_id', 'message_scope'];
  if (['latest_n', 'dynamic_new'].includes(messageScope)) fields.push('message_count');
  if (messageScope === 'specific') fields.push('message_ids');
  if (messageScope === 'date_range') fields.push('date_from', 'date_to');
  return fields;
}

export function fieldsForSubmit(taskType: TaskCenterTaskType, messageScope: string, accountMode: string, pacingMode: string): string[] {
  void pacingMode;
  const commonFields = ['name', 'scheduled_end', 'operation_template_id', 'hourly_activity_curve', 'quiet_threshold', 'peak_threshold'];
  const baseFields = [...commonFields, ...accountSelectionFields(accountMode), 'max_concurrent', 'cooldown_per_account_minutes', 'ban_policy', 'max_actions_per_hour', 'max_retries'];
  if (taskType === 'group_ai_chat') {
    return [
      ...baseFields,
      'target_operation_target_id',
      'rule_set_id',
      'rule_set_version_id',
      'topic_hint',
      'slang_prompt_template_id',
      'tone',
      'idle_continuation_enabled',
    ];
  }
  if (taskType === 'group_relay') {
    return [
      ...baseFields,
      'source_operation_target_ids',
      'rule_set_id',
      'rule_set_version_id',
      'target_operation_target_id',
      'target_operation_target_ids',
      'content_mode',
      'filter_bot_messages',
      'filter_admin_messages',
      'excluded_sender_peer_ids',
      'excluded_sender_input',
    ];
  }
  if (taskType === 'channel_view') {
    return [...baseFields, ...channelScopeFields(messageScope), 'listen_new_messages', 'per_message_daily_view_target', 'per_message_total_view_target', 'message_active_days', 'task_daily_view_safety_cap', 'max_views_per_account_per_day', 'target_views_per_message'];
  }
  if (taskType === 'channel_like') {
    return [...baseFields, ...channelScopeFields(messageScope), 'target_likes_per_message', 'reaction_type', 'allowed_reactions'];
  }
  return [...baseFields, ...channelScopeFields(messageScope), 'target_comments_per_message', 'comment_mode', 'reply_to_message_ids', 'rule_set_id', 'rule_set_version_id', 'comment_style', 'topic_hint'];
}

export function editFieldsForSubmit(taskType: TaskCenterTaskType, accountMode: string, pacingMode: string): string[] {
  void pacingMode;
  const baseFields = ['name', 'scheduled_end', 'operation_template_id', 'hourly_activity_curve', 'quiet_threshold', 'peak_threshold', ...accountFields(accountMode), 'max_actions_per_hour', 'max_retries'];
  if (taskType === 'group_ai_chat') {
    return [...baseFields, 'target_operation_target_id', 'rule_set_id', 'rule_set_version_id', 'topic_hint', 'chat_history_depth', 'ai_model', 'system_prompt_override', 'slang_prompt_template_id', 'slang_terms', 'tone', 'language', 'max_message_length', 'participation_rate', 'allow_account_repeat', 'repeat_cooldown_rounds', 'account_personas', 'account_memory_depth', 'messages_per_round_mode', 'messages_per_round', 'history_fetch_account_id', 'idle_continuation_enabled', 'idle_continuation_seconds', 'context_expire_after_messages'];
  }
  if (taskType === 'group_relay') {
    return [...baseFields, 'source_operation_target_ids', 'source_groups', 'target_operation_target_id', 'target_operation_target_ids', 'rule_set_id', 'rule_set_version_id', 'content_mode', 'filter_bot_messages', 'filter_admin_messages', 'excluded_sender_peer_ids', 'excluded_sender_input'];
  }
  if (taskType === 'channel_view') {
    return [...baseFields, 'listen_new_messages', 'per_message_daily_view_target', 'per_message_total_view_target', 'message_active_days', 'task_daily_view_safety_cap', 'max_views_per_account_per_day', 'target_views_per_message', 'execution_mode'];
  }
  if (taskType === 'channel_like') {
    return [...baseFields, 'target_likes_per_message', 'reaction_type', 'allowed_reactions', 'max_likes_per_account_per_hour'];
  }
  return [...baseFields, 'target_comments_per_message', 'comment_mode', 'reply_to_message_ids', 'rule_set_id', 'rule_set_version_id', 'ai_model', 'comment_style', 'topic_hint', 'system_prompt_override', 'language', 'max_comment_length', 'max_comments_per_account_per_hour'];
}
