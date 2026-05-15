import React from 'react';
import { Alert, Button, Card, Checkbox, Collapse, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Steps, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, RefreshCcw } from 'lucide-react';
import { api, ApiError } from '../../shared/api/client';
import type { Account, AccountPool, ChannelCapacityCheck, ChannelMessage, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, SchedulingSetting, TaskCenterAction, TaskCenterDetail, TaskCenterPrefill, TaskCenterTask, TaskCenterTaskType } from '../types';
import { DetailModal, StatusBadge, StatCard, useAntdTableControls } from '../components/shared';
import { formatBeijingDateTime, fromBeijingDateTimeLocalValue, toBeijingDateTimeLocalValue } from '../time';

const TASK_TYPES: Array<{ value: TaskCenterTaskType; label: string }> = [
  { value: 'group_ai_chat', label: 'AI 活跃群' },
  { value: 'group_relay', label: '转发监听群' },
  { value: 'channel_view', label: '频道消息浏览' },
  { value: 'channel_like', label: '频道消息点赞' },
  { value: 'channel_comment', label: '频道消息评论/回复' },
];

const TYPE_LABEL: Record<string, string> = Object.fromEntries(TASK_TYPES.map((item) => [item.value, item.label]));

const CREATE_ENDPOINT: Record<TaskCenterTaskType, string> = {
  group_ai_chat: '/tasks/group-ai-chat',
  group_relay: '/tasks/group-relay',
  channel_view: '/tasks/channel-view',
  channel_like: '/tasks/channel-like',
  channel_comment: '/tasks/channel-comment',
};

const CREATE_AND_START_ENDPOINT: Record<TaskCenterTaskType, string> = {
  group_ai_chat: '/tasks/group-ai-chat/create-and-start',
  group_relay: '/tasks/group-relay/create-and-start',
  channel_view: '/tasks/channel-view/create-and-start',
  channel_like: '/tasks/channel-like/create-and-start',
  channel_comment: '/tasks/channel-comment/create-and-start',
};

const WIZARD_STEPS = ['基础信息', '目标来源', '任务配置', '账号选择', '确认提交'];

const ACTION_LABEL: Record<string, string> = {
  send_message: '发送消息',
  like_message: '点赞',
  post_comment: '评论',
  view_message: '浏览',
};

function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
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

function words(value?: string): string[] {
  return (value ?? '').split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
}

function parseKeyValueMap(value?: string | Record<string, string>): Record<string, string> {
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

function formatKeyValueMap(value?: Record<string, string>): string {
  return value ? Object.entries(value).map(([key, role]) => `${key}=${role}`).join('\n') : '';
}

function normalizePromptTemplateType(value?: string): string {
  return (value ?? '').replace(/\s+/g, '');
}

function csvNumbers(value?: Array<number | string> | string | null): number[] {
  if (Array.isArray(value)) return value.map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0);
  if (typeof value === 'string') {
    return value
      .split(/[,，\n\s]+/)
      .map((item) => Number(item.trim()))
      .filter((item) => Number.isFinite(item) && item > 0);
  }
  return [];
}

function formatDateTime(value?: string | null): string {
  return formatBeijingDateTime(value);
}

function toDateTimeLocal(value?: string | null): string | undefined {
  return toBeijingDateTimeLocalValue(value);
}

function actionLabel(value: string): string {
  return ACTION_LABEL[value] ?? value;
}

function statusLabel(value?: string | null): string {
  if (['running', 'executing', 'pending'].includes(value ?? '')) return '运行中';
  if (['draft', 'paused'].includes(value ?? '')) return '未运行';
  if (value === 'target_reached') return '已达标';
  if (value === 'wrapping_up') return '收尾中';
  if (value === 'stopped') return '人工停止';
  if (value === 'deleted') return '已删除';
  if (['completed', 'success', 'skipped', 'approved'].includes(value ?? '')) return '已完成';
  if (['failed', 'rejected', 'expired'].includes(value ?? '')) return '失败';
  return value || '未运行';
}

function TaskStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={statusLabel(status)} />;
}

function accountDisplay(detail: TaskCenterDetail | null, accountId?: number | null): string {
  if (!accountId) return '-';
  const account = detail?.accounts.find((item) => item.id === accountId);
  if (!account) return `账号 #${accountId}`;
  return account.username ? `${account.display_name} / @${account.username}` : account.display_name;
}

function actionTarget(action: TaskCenterAction): string {
  return action.payload?.target_display ?? action.payload?.channel_id ?? action.payload?.chat_id ?? '-';
}

function actionContent(action: TaskCenterAction): string {
  return action.payload?.message_text ?? action.payload?.comment_text ?? action.payload?.message_content ?? action.payload?.reaction_emoji ?? '-';
}

function actionResult(action: TaskCenterAction): string {
  if (action.result?.error_message) {
    const prefix = action.result?.auto_check ? `自动校验：${action.result.auto_check} / ` : '';
    return `${prefix}${action.result.error_message}`;
  }
  if (action.result?.telegram_msg_id) return `消息ID ${action.result.telegram_msg_id}`;
  if (action.result?.success === true) return '成功';
  if (action.result?.success === false) return '失败';
  return '-';
}

function isPlannedAction(action: TaskCenterAction): boolean {
  return ['pending', 'executing'].includes(action.status) && !action.executed_at;
}

function commonInitialValues(setting?: SchedulingSetting | null) {
  return {
    priority: 3,
    timezone: 'Asia/Shanghai',
    selection_mode: 'all',
    max_concurrent: 20,
    cooldown_per_account_minutes: 5,
    ban_policy: 'skip',
    pacing_mode: 'template',
    template: 'moderate_6h',
    jitter_percent: setting ? Math.min(100, Math.round(((setting.jitter_min_seconds + setting.jitter_max_seconds) / 2 / Math.max(setting.batch_interval_seconds, 1)) * 100)) : 30,
    quiet_enabled: Boolean(setting?.quiet_hours_enabled),
    quiet_start: setting?.quiet_start ?? '02:00',
    quiet_end: setting?.quiet_end ?? '08:00',
    max_retries: setting?.default_max_retries ?? 3,
    retry_delay_seconds: setting?.default_retry_delay_seconds ?? 60,
    retry_backoff: setting?.default_retry_backoff ?? 'exponential',
    on_account_banned: setting?.default_on_account_banned ?? 'skip_account',
    on_api_rate_limit: setting?.default_on_api_rate_limit ?? 'wait_and_retry',
    on_content_rejected: setting?.default_on_content_rejected ?? 'skip_message',
  };
}

function typeInitialValues(type: TaskCenterTaskType, setting?: SchedulingSetting | null) {
  if (type === 'group_ai_chat') {
    return {
      participation_rate: 0.6,
      participation_jitter: 0.5,
      chat_history_depth: 50,
      messages_per_round_mode: 'auto',
      messages_per_round: 1,
      silent_mode_enabled: setting?.quiet_hours_enabled ?? true,
      silent_start: setting?.quiet_start ?? '23:00',
      silent_end: setting?.quiet_end ?? '08:00',
      silent_max_accounts: 5,
      silent_messages_per_round: 1,
      ramp_up_minutes: 60,
      ramp_start_ratio: 0.3,
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
      dedup_window_minutes: 60,
      dedup_method: 'hash',
    };
  }
  if (type === 'channel_view') {
    return {
      message_scope: 'dynamic_new',
      message_count: 10,
      target_views_per_message: 50,
      view_count_jitter: 0.2,
      execution_mode: 'distribute',
    };
  }
  if (type === 'channel_like') {
    return {
      message_scope: 'dynamic_new',
      message_count: 10,
      target_likes_per_message: 50,
      like_count_jitter: 0.3,
      reaction_type: 'random',
      allowed_reactions: '👍,❤️,🔥',
      max_likes_per_account_per_hour: 10,
    };
  }
  return {
    message_scope: 'dynamic_new',
    message_count: 10,
    target_comments_per_message: 10,
    comment_count_jitter: 0.3,
    comment_mode: 'comment',
    reply_to_message_ids: '',
    language: 'zh-CN',
    comment_style: 'mixed',
    max_comments_per_account_per_hour: 3,
  };
}

function initialValuesForType(type: TaskCenterTaskType, setting?: SchedulingSetting | null) {
  return { ...commonInitialValues(setting), ...typeInitialValues(type, setting) };
}

function defaultRuleSelection(ruleSets: RuleSet[], taskType: TaskCenterTaskType): { rule_set_id: number; rule_set_version_id: number } | null {
  const ruleSet = ruleSets.find((item) => (item.task_types ?? []).includes(taskType))
    ?? (taskType === 'group_relay' ? ruleSets.find((item) => item.name === '默认运营规则集' || item.name === '默认转发监听过滤规则') : null)
    ?? ruleSets[0];
  if (!ruleSet) return null;
  const version = ruleSet.versions.find((item) => item.id === ruleSet.active_version_id && item.status === 'published')
    ?? ruleSet.versions.find((item) => item.status === 'published');
  if (!version) return null;
  return { rule_set_id: ruleSet.id, rule_set_version_id: version.id };
}

export default function TaskCenterView({
  accounts,
  accountPools,
  promptTemplates,
  prefill,
}: {
  accounts: Account[];
  accountPools: AccountPool[];
  promptTemplates: PromptTemplate[];
  prefill?: TaskCenterPrefill | null;
}) {
  const [tasks, setTasks] = React.useState<TaskCenterTask[]>([]);
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [messages, setMessages] = React.useState<ChannelMessage[]>([]);
  const [comments, setComments] = React.useState<ChannelMessageComment[]>([]);
  const [ruleSets, setRuleSets] = React.useState<RuleSet[]>([]);
  const [schedulingSetting, setSchedulingSetting] = React.useState<SchedulingSetting | null>(null);
  const [detail, setDetail] = React.useState<TaskCenterDetail | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [supportLoading, setSupportLoading] = React.useState(false);
  const [busyId, setBusyId] = React.useState('');
  const [modalOpen, setModalOpen] = React.useState(false);
  const [editOpen, setEditOpen] = React.useState(false);
  const [editSaving, setEditSaving] = React.useState(false);
  const [actionError, setActionError] = React.useState('');
  const [actionWarning, setActionWarning] = React.useState('');
  const [wizardStep, setWizardStep] = React.useState(0);
  const [taskType, setTaskType] = React.useState<TaskCenterTaskType>('group_ai_chat');
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const appliedPrefillNonce = React.useRef<number | null>(null);
  const accountMode = Form.useWatch('selection_mode', form) ?? 'all';
  const pacingMode = Form.useWatch('pacing_mode', form) ?? 'template';
  const editAccountMode = Form.useWatch('selection_mode', editForm) ?? 'all';
  const editPacingMode = Form.useWatch('pacing_mode', editForm) ?? 'template';
  const editMessageScope = Form.useWatch('message_scope', editForm) ?? 'latest_n';
  const editTargetChannelId = Form.useWatch('target_channel_id', editForm);
  const messageIds = Form.useWatch('message_ids', form);
  const editMessageIds = Form.useWatch('message_ids', editForm);
  const messageScope = Form.useWatch('message_scope', form) ?? 'latest_n';
  const targetChannelId = Form.useWatch('target_channel_id', form);
  const channelTargets = targets.filter((target) => target.target_type === 'channel');
  const groupTargets = targets.filter((target) => target.target_type === 'group' && target.linked_group_id);
  const slangTemplates = promptTemplates.filter((template) => normalizePromptTemplateType(template.template_type) === 'AI黑话词表' && template.is_active);
  const defaultSlangTemplateId = slangTemplates[0]?.id ?? null;

  async function load() {
    setLoading(true);
    try {
      const [taskData, schedulingData] = await Promise.all([
        api<TaskCenterTask[]>('/tasks'),
        api<SchedulingSetting>('/scheduling-settings'),
      ]);
      setTasks(taskData);
      setSchedulingSetting(schedulingData);
    } finally {
      setLoading(false);
    }
  }

  async function ensureTargets() {
    if (targets.length) return targets;
    const targetData = await api<OperationTarget[]>('/operation-targets');
    setTargets(targetData);
    return targetData;
  }

  async function ensureMessages() {
    if (messages.length) return messages;
    const messageData = await api<ChannelMessage[]>('/channel-messages');
    setMessages(messageData);
    return messageData;
  }

  async function ensureComments() {
    if (comments.length) return comments;
    const commentData = await api<ChannelMessageComment[]>('/channel-comments');
    setComments(commentData);
    return commentData;
  }

  async function ensureRuleSets() {
    if (ruleSets.length) return ruleSets;
    const ruleSetData = await api<RuleSet[]>('/rule-sets');
    setRuleSets(ruleSetData);
    return ruleSetData;
  }

  function applyDefaultRuleSet(loadedRuleSets: RuleSet[], type: TaskCenterTaskType = taskType) {
    const current = form.getFieldsValue(['rule_set_id', 'rule_set_version_id']);
    if (current.rule_set_id || current.rule_set_version_id) return;
    const selection = defaultRuleSelection(loadedRuleSets, type);
    if (selection) form.setFieldsValue(selection);
  }

  async function ensureTaskFormData(type: TaskCenterTaskType) {
    setSupportLoading(true);
    try {
      const requests: Array<Promise<unknown>> = [ensureTargets()];
      if (['group_relay', 'group_ai_chat', 'channel_comment'].includes(type)) requests.push(ensureRuleSets());
      if (type.startsWith('channel_')) requests.push(ensureMessages());
      if (type === 'channel_comment') requests.push(ensureComments());
      await Promise.all(requests);
    } finally {
      setSupportLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(timer);
  }, []);

  React.useEffect(() => {
    if (!modalOpen && !editOpen) return;
    if (['group_relay', 'group_ai_chat', 'channel_comment'].includes(taskType)) void ensureRuleSets();
    if (taskType.startsWith('channel_')) void ensureMessages();
    if (taskType === 'channel_comment') void ensureComments();
  }, [editOpen, modalOpen, messageScope, taskType]);

  React.useEffect(() => {
    if (!prefill || appliedPrefillNonce.current === prefill.nonce) return;
    if (!targets.length) {
      void ensureTargets();
      return;
    }
    if (prefill.message) {
      setMessages((current) => current.some((message) => message.id === prefill.message?.id) ? current : [prefill.message!, ...current]);
    }

    const nextType = prefill.taskType;
    const nextValues: Record<string, any> = {
      ...initialValuesForType(nextType, schedulingSetting),
      name: `${prefill.target.title} ${TYPE_LABEL[nextType] ?? '任务'}`,
    };
    if (prefill.target.target_type === 'group') {
      nextValues.target_operation_target_id = prefill.target.id;
      if (nextType === 'group_relay') {
        nextValues.source_operation_target_ids = [prefill.target.id];
        nextValues.source_groups = [{ operation_target_id: prefill.target.id, group_name: prefill.target.title, is_active: true }];
      }
    } else {
      nextValues.target_channel_id = prefill.target.id;
      if (prefill.message) {
        nextValues.message_scope = 'specific';
        nextValues.message_ids = [prefill.message.id];
      }
    }
    setActionError('');
    setActionWarning('');
    setTaskType(nextType);
    form.resetFields();
    form.setFieldsValue(nextValues);
    if (['group_relay', 'group_ai_chat', 'channel_comment'].includes(nextType)) void ensureRuleSets().then((loaded) => applyDefaultRuleSet(loaded, nextType));
    setWizardStep(2);
    setModalOpen(true);
    appliedPrefillNonce.current = prefill.nonce;
  }, [form, messages, prefill, schedulingSetting, targets]);

  async function loadDetail(task: TaskCenterTask) {
    setDetail(await api<TaskCenterDetail>(`/tasks/${task.id}`));
  }

  async function openCreateTask() {
    setActionError('');
    setActionWarning('');
    setTaskType('group_ai_chat');
    form.resetFields();
    form.setFieldsValue(initialValuesForType('group_ai_chat', schedulingSetting));
    if (defaultSlangTemplateId) form.setFieldsValue({ slang_prompt_template_id: defaultSlangTemplateId });
    setWizardStep(0);
    await ensureTaskFormData('group_ai_chat');
    setModalOpen(true);
  }

  function editValuesFromTask(task: TaskCenterTask): Record<string, any> {
    const config = task.type_config || {};
    const account = task.account_config || {};
    const pacing = task.pacing_config || {};
    const failure = task.failure_policy || {};
    const filters = config.filters || {};
    return {
      ...initialValuesForType(task.type, schedulingSetting),
      name: task.name,
      priority: task.priority,
      timezone: task.timezone,
      scheduled_start: toDateTimeLocal(task.scheduled_start),
      scheduled_end: toDateTimeLocal(task.scheduled_end),
      selection_mode: account.selection_mode ?? 'all',
      account_group_id: account.account_group_id ?? null,
      account_ids: account.account_ids ?? [],
      max_concurrent: account.max_concurrent ?? 20,
      cooldown_per_account_minutes: account.cooldown_per_account_minutes ?? 5,
      ban_policy: account.ban_policy ?? 'skip',
      pacing_mode: pacing.mode ?? 'template',
      interval_seconds_min: pacing.interval_seconds_min ?? null,
      interval_seconds_max: pacing.interval_seconds_max ?? null,
      curve_type: pacing.curve_type ?? null,
      curve_duration_hours: pacing.curve_duration_hours ?? null,
      template: pacing.template ?? 'moderate_6h',
      jitter_percent: pacing.jitter_percent ?? 30,
      max_actions_per_hour: pacing.max_actions_per_hour ?? null,
      max_actions_per_day: pacing.max_actions_per_day ?? null,
      max_retries: failure.max_retries ?? 3,
      retry_delay_seconds: failure.retry_delay_seconds ?? 60,
      retry_backoff: failure.retry_backoff ?? 'exponential',
      on_account_banned: failure.on_account_banned ?? 'skip_account',
      on_api_rate_limit: failure.on_api_rate_limit ?? 'wait_and_retry',
      on_content_rejected: failure.on_content_rejected ?? 'skip_message',
      ...config,
      source_operation_target_ids: Array.isArray(config.source_groups)
        ? config.source_groups.map((item: any) => item?.operation_target_id).filter(Boolean)
        : [],
      account_personas: formatKeyValueMap(config.account_personas),
      slang_terms: formatKeyValueMap(config.slang_terms),
      slang_prompt_template_id: task.type === 'group_ai_chat' ? (config.slang_prompt_template_id ?? defaultSlangTemplateId) : (config.slang_prompt_template_id ?? null),
      allowed_reactions: Array.isArray(config.allowed_reactions) ? config.allowed_reactions.join(',') : config.allowed_reactions,
      reply_to_message_ids: Array.isArray(config.reply_to_message_ids) ? config.reply_to_message_ids : csvNumbers(config.reply_to_message_ids),
      monitor_account_ids: config.monitor_account_ids ?? [],
      keyword_whitelist: Array.isArray(filters.keyword_whitelist) ? filters.keyword_whitelist.join(',') : '',
      keyword_blacklist: Array.isArray(filters.keyword_blacklist) ? filters.keyword_blacklist.join(',') : '',
      min_message_length: filters.min_message_length ?? null,
      max_message_length: filters.max_message_length ?? config.max_message_length ?? null,
      allowed_media_types: Array.isArray(filters.allowed_media_types) ? filters.allowed_media_types.join(',') : '',
      blocked_user_ids: Array.isArray(filters.blocked_user_ids) ? filters.blocked_user_ids.join(',') : '',
      only_with_media: Boolean(filters.only_with_media),
      only_text: Boolean(filters.only_text),
      language_filter: filters.language_filter ?? null,
    };
  }

  async function openEditTask(task: TaskCenterTask) {
    setActionError('');
    setActionWarning('');
    setTaskType(task.type);
    await ensureTaskFormData(task.type);
    editForm.resetFields();
    editForm.setFieldsValue(editValuesFromTask(task));
    setEditOpen(true);
  }

  function accountConfig(values: any) {
    return {
      selection_mode: values.selection_mode ?? 'all',
      account_group_id: values.selection_mode === 'group' ? values.account_group_id : null,
      account_ids: values.selection_mode === 'manual' ? csvNumbers(values.account_ids) : [],
      max_concurrent: values.max_concurrent ?? 20,
      cooldown_per_account_minutes: values.cooldown_per_account_minutes ?? 5,
      ban_policy: values.ban_policy ?? 'skip',
    };
  }

  function pacingConfig(values: any) {
    return {
      mode: values.pacing_mode ?? 'template',
      interval_seconds_min: values.interval_seconds_min ?? null,
      interval_seconds_max: values.interval_seconds_max ?? null,
      curve_type: values.curve_type ?? null,
      curve_duration_hours: values.curve_duration_hours ?? null,
      template: values.template ?? 'moderate_6h',
      jitter_percent: values.jitter_percent ?? 30,
      max_actions_per_hour: values.max_actions_per_hour ?? null,
      max_actions_per_day: values.max_actions_per_day ?? null,
      quiet_hours: values.quiet_enabled ? { start: values.quiet_start ?? '02:00', end: values.quiet_end ?? '08:00', timezone: values.timezone ?? 'Asia/Shanghai' } : null,
    };
  }

  function failurePolicy(values: any) {
    return {
      max_retries: values.max_retries ?? 3,
      retry_delay_seconds: values.retry_delay_seconds ?? 60,
      retry_backoff: values.retry_backoff ?? 'exponential',
      on_account_banned: values.on_account_banned ?? 'skip_account',
      on_api_rate_limit: values.on_api_rate_limit ?? 'wait_and_retry',
      on_content_rejected: values.on_content_rejected ?? 'skip_message',
      alert_on_failure: false,
      alert_webhook: null,
    };
  }

  function commonPayload(values: any) {
    return {
      name: values.name,
      priority: 3,
      timezone: values.timezone ?? 'Asia/Shanghai',
      scheduled_start: null,
      scheduled_end: fromBeijingDateTimeLocalValue(values.scheduled_end),
      max_duration_hours: null,
      account_config: accountConfig(values),
      pacing_config: pacingConfig(values),
      failure_policy: failurePolicy(values),
    };
  }

  function channelScopePayload(values: any) {
    const channel = channelTargets.find((item) => item.id === values.target_channel_id);
    return {
      target_channel_id: values.target_channel_id,
      target_channel_name: channel?.title ?? '',
      message_scope: values.message_scope ?? 'latest_n',
      message_count: ['latest_n', 'dynamic_new'].includes(values.message_scope) ? values.message_count ?? 10 : null,
      date_from: fromBeijingDateTimeLocalValue(values.date_from),
      date_to: fromBeijingDateTimeLocalValue(values.date_to),
      message_ids: values.message_scope === 'specific' ? csvNumbers(values.message_ids) : [],
    };
  }

  function createPayload(values: any): Record<string, any> {
    const base = commonPayload(values);
    if (taskType === 'group_ai_chat') {
      const target = groupTargets.find((item) => item.id === values.target_operation_target_id);
      return {
        ...base,
        target_operation_target_id: values.target_operation_target_id,
        rule_set_id: values.rule_set_id ?? null,
        rule_set_version_id: values.rule_set_version_id ?? null,
        target_group_name: target?.title ?? '',
        topic_hint: values.topic_hint ?? '',
        chat_history_depth: values.chat_history_depth ?? 50,
        ai_model: values.ai_model ?? '',
        system_prompt_override: values.system_prompt_override ?? '',
        slang_prompt_template_id: values.slang_prompt_template_id ?? null,
        slang_terms: parseKeyValueMap(values.slang_terms),
        tone: values.tone ?? 'auto',
        language: values.language ?? 'zh-CN',
        max_message_length: values.max_message_length ?? null,
        participation_rate: values.participation_rate ?? 0.6,
        participation_jitter: values.participation_jitter ?? 0.5,
        allow_account_repeat: values.allow_account_repeat ?? true,
        repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2,
        account_personas: parseKeyValueMap(values.account_personas),
        account_memory_depth: values.account_memory_depth ?? 3,
        messages_per_round_mode: values.messages_per_round_mode ?? 'auto',
        messages_per_round: values.messages_per_round ?? 1,
        history_fetch_account_id: values.history_fetch_account_id ?? null,
        silent_mode_enabled: values.silent_mode_enabled ?? true,
        silent_start: values.silent_start ?? '23:00',
        silent_end: values.silent_end ?? '08:00',
        silent_max_accounts: values.silent_max_accounts ?? 5,
        silent_messages_per_round: values.silent_messages_per_round ?? 1,
        ramp_up_minutes: values.ramp_up_minutes ?? 60,
        ramp_start_ratio: values.ramp_start_ratio ?? 0.3,
        context_expire_after_messages: values.context_expire_after_messages ?? 10,
        idle_continuation_enabled: values.idle_continuation_enabled ?? true,
        idle_continuation_seconds: values.idle_continuation_seconds ?? 300,
      };
    }
    if (taskType === 'group_relay') {
      const sourceTargetIds = csvNumbers(values.source_operation_target_ids);
      const targetOperationIds = csvNumbers(values.target_operation_target_ids);
      return {
        ...base,
        source_groups: sourceTargetIds.map((id) => {
          const target = groupTargets.find((item) => item.id === id);
          return { operation_target_id: id, group_name: target?.title ?? '', is_active: true };
        }),
        rule_set_id: values.rule_set_id ?? null,
        rule_set_version_id: values.rule_set_version_id ?? null,
        monitor_account_ids: csvNumbers(values.monitor_account_ids),
        filters: {
          keyword_whitelist: words(values.keyword_whitelist),
          keyword_blacklist: words(values.keyword_blacklist),
          min_message_length: values.min_message_length ?? null,
          max_message_length: values.max_message_length ?? null,
          allowed_media_types: words(values.allowed_media_types),
          blocked_user_ids: words(values.blocked_user_ids),
          only_with_media: Boolean(values.only_with_media),
          only_text: Boolean(values.only_text),
          language_filter: values.language_filter ?? null,
        },
        target_operation_target_id: values.target_operation_target_id,
        target_operation_target_ids: targetOperationIds,
        send_account_ids: [],
        content_mode: values.content_mode ?? 'light_rewrite',
        rewrite_prompt: values.rewrite_prompt ?? '',
        preserve_media: Boolean(values.preserve_media),
        add_source_attribution: Boolean(values.add_source_attribution),
        dedup_window_minutes: values.dedup_window_minutes ?? 60,
        dedup_method: values.dedup_method ?? 'hash',
        require_review: false,
      };
    }
    if (taskType === 'channel_view') {
      return { ...base, ...channelScopePayload(values), target_views_per_message: values.target_views_per_message ?? 50, view_count_jitter: values.view_count_jitter ?? 0.2, execution_mode: values.execution_mode ?? 'distribute' };
    }
    if (taskType === 'channel_like') {
      return { ...base, ...channelScopePayload(values), target_likes_per_message: values.target_likes_per_message ?? 50, like_count_jitter: values.like_count_jitter ?? 0.3, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return { ...base, ...channelScopePayload(values), target_comments_per_message: values.target_comments_per_message ?? 10, comment_count_jitter: values.comment_count_jitter ?? 0.3, comment_mode: values.comment_mode ?? 'comment', reply_to_message_ids: csvNumbers(values.reply_to_message_ids), rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, ai_model: values.ai_model ?? '', comment_style: values.comment_style ?? 'mixed', topic_hint: values.topic_hint ?? '', system_prompt_override: values.system_prompt_override ?? '', language: values.language ?? 'zh-CN', max_comment_length: values.max_comment_length ?? null, max_comments_per_account_per_hour: values.max_comments_per_account_per_hour ?? 3, require_review: false };
  }

  function settingsPayload(type: TaskCenterTaskType, values: any): Record<string, any> {
    const base = {
      name: values.name,
      priority: values.priority ?? 3,
      timezone: values.timezone ?? 'Asia/Shanghai',
      scheduled_start: fromBeijingDateTimeLocalValue(values.scheduled_start),
      scheduled_end: fromBeijingDateTimeLocalValue(values.scheduled_end),
      account_config: accountConfig(values),
      pacing_config: pacingConfig(values),
      failure_policy: failurePolicy(values),
    };
    if (type === 'group_ai_chat') {
      const target = groupTargets.find((item) => item.id === values.target_operation_target_id);
      return { ...base, target_operation_target_id: values.target_operation_target_id ?? null, rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, target_group_name: target?.title ?? '', topic_hint: values.topic_hint ?? '', chat_history_depth: values.chat_history_depth ?? 50, ai_model: values.ai_model ?? '', system_prompt_override: values.system_prompt_override ?? '', slang_prompt_template_id: values.slang_prompt_template_id ?? null, slang_terms: parseKeyValueMap(values.slang_terms), tone: values.tone ?? 'auto', language: values.language ?? 'zh-CN', max_message_length: values.max_message_length ?? null, participation_rate: values.participation_rate ?? 0.6, participation_jitter: values.participation_jitter ?? 0.5, allow_account_repeat: values.allow_account_repeat ?? true, repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2, account_personas: parseKeyValueMap(values.account_personas), account_memory_depth: values.account_memory_depth ?? 3, messages_per_round_mode: values.messages_per_round_mode ?? 'auto', messages_per_round: values.messages_per_round ?? 1, history_fetch_account_id: values.history_fetch_account_id ?? null, idle_continuation_enabled: values.idle_continuation_enabled ?? true, idle_continuation_seconds: values.idle_continuation_seconds ?? 300, silent_mode_enabled: values.silent_mode_enabled ?? true, silent_start: values.silent_start ?? '23:00', silent_end: values.silent_end ?? '08:00', silent_max_accounts: values.silent_max_accounts ?? 5, silent_messages_per_round: values.silent_messages_per_round ?? 1, ramp_up_minutes: values.ramp_up_minutes ?? 60, ramp_start_ratio: values.ramp_start_ratio ?? 0.3, context_expire_after_messages: values.context_expire_after_messages ?? 10 };
    }
    if (type === 'group_relay') {
      const sourceTargetIds = csvNumbers(values.source_operation_target_ids);
      const targetOperationIds = csvNumbers(values.target_operation_target_ids);
      return { ...base, source_groups: sourceTargetIds.length ? sourceTargetIds.map((id) => {
        const target = groupTargets.find((item) => item.id === id);
        return { operation_target_id: id, group_name: target?.title ?? '', is_active: true };
      }) : values.source_groups ?? [], target_operation_target_id: values.target_operation_target_id ?? null, target_operation_target_ids: targetOperationIds, rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, monitor_account_ids: csvNumbers(values.monitor_account_ids), filters: { keyword_whitelist: words(values.keyword_whitelist), keyword_blacklist: words(values.keyword_blacklist), min_message_length: values.min_message_length ?? null, max_message_length: values.max_message_length ?? null, allowed_media_types: words(values.allowed_media_types), blocked_user_ids: words(values.blocked_user_ids), only_with_media: Boolean(values.only_with_media), only_text: Boolean(values.only_text), language_filter: values.language_filter ?? null }, content_mode: values.content_mode ?? 'light_rewrite', rewrite_prompt: values.rewrite_prompt ?? '', preserve_media: Boolean(values.preserve_media), add_source_attribution: Boolean(values.add_source_attribution), dedup_window_minutes: values.dedup_window_minutes ?? 60, dedup_method: values.dedup_method ?? 'hash', require_review: false };
    }
    if (type === 'channel_view') {
      return { ...base, target_views_per_message: values.target_views_per_message ?? 50, view_count_jitter: values.view_count_jitter ?? 0.2, execution_mode: values.execution_mode ?? 'distribute' };
    }
    if (type === 'channel_like') {
      return { ...base, target_likes_per_message: values.target_likes_per_message ?? 50, like_count_jitter: values.like_count_jitter ?? 0.3, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return { ...base, target_comments_per_message: values.target_comments_per_message ?? 10, comment_count_jitter: values.comment_count_jitter ?? 0.3, comment_mode: values.comment_mode ?? 'comment', reply_to_message_ids: csvNumbers(values.reply_to_message_ids), rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, ai_model: values.ai_model ?? '', comment_style: values.comment_style ?? 'mixed', topic_hint: values.topic_hint ?? '', system_prompt_override: values.system_prompt_override ?? '', language: values.language ?? 'zh-CN', max_comment_length: values.max_comment_length ?? null, max_comments_per_account_per_hour: values.max_comments_per_account_per_hour ?? 3, require_review: false };
  }

  function capacityCheckPayload(values: any) {
    if (taskType === 'channel_view') {
      return {
        task_type: taskType,
        account_config: accountConfig(values),
        target_per_message: values.target_views_per_message ?? 50,
        ...channelScopePayload(values),
      };
    }
    if (taskType === 'channel_like') {
      return {
        task_type: taskType,
        account_config: accountConfig(values),
        target_per_message: values.target_likes_per_message ?? 50,
        ...channelScopePayload(values),
      };
    }
    if (taskType === 'channel_comment') {
      return {
        task_type: taskType,
        account_config: accountConfig(values),
        target_per_message: values.target_comments_per_message ?? 10,
        ...channelScopePayload(values),
      };
    }
    return null;
  }

  async function createTask(options: { start?: boolean; skipCapacityCheck?: boolean } = {}) {
    const start = options.start ?? true;
    setActionError('');
    setActionWarning('');
    try {
      await form.validateFields(fieldsForSubmit(taskType, messageScope, accountMode, pacingMode));
      const values = form.getFieldsValue(true);
      const checkPayload = capacityCheckPayload(values);
      if (!options.skipCapacityCheck && checkPayload) {
        const capacity = await api<ChannelCapacityCheck>('/tasks/channel-capacity-check', { method: 'POST', body: JSON.stringify(checkPayload) });
        if (capacity.will_shortfall) {
          setActionWarning(capacity.warning_message || `当前参与账号 ${capacity.max_effective_per_message} 个，任务会继续运行。`);
        }
      }
      await api<TaskCenterTask>((start ? CREATE_AND_START_ENDPOINT : CREATE_ENDPOINT)[taskType], { method: 'POST', body: JSON.stringify(createPayload(values)) });
      form.resetFields();
      setTaskType('group_ai_chat');
      form.setFieldsValue(initialValuesForType('group_ai_chat', schedulingSetting));
      setWizardStep(0);
      setModalOpen(false);
      await load();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function saveTaskSettings() {
    if (!detail) return;
    setEditSaving(true);
    setActionError('');
    setActionWarning('');
    try {
      await editForm.validateFields(editFieldsForSubmit(detail.task.type, editAccountMode, editPacingMode));
      const values = editForm.getFieldsValue(true);
      const updated = await api<TaskCenterTask>(`/tasks/${detail.task.id}/settings`, { method: 'PATCH', body: JSON.stringify(settingsPayload(detail.task.type, values)) });
      setEditOpen(false);
      setActionWarning(updated.status === 'running' ? '已保存，下一轮会按新配置重新规划未执行计划。' : '已保存任务配置。');
      await load();
      await loadDetail(updated);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setEditSaving(false);
    }
  }

  async function taskAction(task: TaskCenterTask, name: 'start' | 'pause' | 'resume' | 'stop' | 'retry' | 'reset') {
    setBusyId(`${task.id}:${name}`);
    setActionError('');
    try {
      await api<TaskCenterTask>(`/tasks/${task.id}/${name}`, { method: 'POST', body: name === 'retry' ? JSON.stringify({ failed_only: true }) : undefined });
      await load();
      if (detail?.task.id === task.id) await loadDetail(task);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyId('');
    }
  }

  async function deleteTask(task: TaskCenterTask) {
    setBusyId(`${task.id}:delete`);
    setActionError('');
    try {
      await api(`/tasks/${task.id}`, { method: 'DELETE' });
      if (detail?.task.id === task.id) setDetail(null);
      await load();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyId('');
    }
  }

  function confirmResetTask(task: TaskCenterTask) {
    Modal.confirm({
      title: '重置并重新规划任务',
      content: '会清空这个任务旧的执行计划和执行记录，并重新拉取消息、重新生成计划。',
      okText: '重置',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => taskAction(task, 'reset'),
    });
  }

  function confirmDeleteTask(task: TaskCenterTask) {
    Modal.confirm({
      title: '删除任务',
      content: `确认删除“${task.name}”？任务会停止并从任务中心隐藏，历史执行记录保留。`,
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => deleteTask(task),
    });
  }

  async function nextStep() {
    setActionError('');
    try {
      await form.validateFields(fieldsForStep(wizardStep, taskType, messageScope, accountMode));
      if (wizardStep === 0) {
        await ensureTaskFormData(taskType);
        if (['group_relay', 'group_ai_chat', 'channel_comment'].includes(taskType)) applyDefaultRuleSet(await ensureRuleSets(), taskType);
      }
      setWizardStep((value) => Math.min(value + 1, WIZARD_STEPS.length - 1));
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  function resetTypeFields(nextType: TaskCenterTaskType) {
    setTaskType(nextType);
    form.resetFields();
    form.setFieldsValue(initialValuesForType(nextType, schedulingSetting));
    if (nextType === 'group_ai_chat' && defaultSlangTemplateId) form.setFieldsValue({ slang_prompt_template_id: defaultSlangTemplateId });
    setWizardStep(0);
    void ensureTaskFormData(nextType).then(async () => {
      if (['group_relay', 'group_ai_chat', 'channel_comment'].includes(nextType)) applyDefaultRuleSet(await ensureRuleSets(), nextType);
    });
  }

  const table = useAntdTableControls<TaskCenterTask>({
    rows: tasks,
    placeholder: '搜索任务 / 频道 / 消息 / 状态',
    search: [(task) => [task.id, task.name, TYPE_LABEL[task.type], statusLabel(task.status), task.status, task.target_summary, task.search_text, task.last_error]],
  });

  const columns: ColumnsType<TaskCenterTask> = [
    {
      title: '任务',
      key: 'task',
      width: 340,
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{task.name}</Typography.Text>
          <Typography.Text type="secondary">{TYPE_LABEL[task.type]}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '执行统计', key: 'stats', width: 180, render: (_, task) => `${task.stats?.success_count ?? 0}/${task.stats?.total_actions ?? 0} 成功，${task.stats?.failure_count ?? 0} 失败` },
    { title: '下次运行', dataIndex: 'next_run_at', width: 180, render: (value) => formatDateTime(value) },
    { title: '错误', dataIndex: 'last_error', width: 220, render: (value) => value || '无' },
    {
      title: '操作',
      key: 'actions',
      width: 420,
      fixed: 'right',
      render: (_, task) => (
        <Space className="task-action-bar" size={6}>
          <Button size="small" loading={busyId === `${task.id}:${task.status === 'paused' ? 'resume' : 'start'}`} onClick={() => taskAction(task, task.status === 'paused' ? 'resume' : 'start')}>{task.status === 'paused' ? '恢复' : '启动'}</Button>
          <Button size="small" disabled={task.status !== 'running'} loading={busyId === `${task.id}:pause`} onClick={() => taskAction(task, 'pause')}>暂停</Button>
          <Button size="small" loading={busyId === `${task.id}:retry`} onClick={() => taskAction(task, 'retry')}>重试</Button>
          <Button size="small" danger loading={busyId === `${task.id}:reset`} onClick={() => confirmResetTask(task)}>重置</Button>
          <Button size="small" danger loading={busyId === `${task.id}:stop`} onClick={() => taskAction(task, 'stop')}>停止</Button>
          <Button size="small" danger loading={busyId === `${task.id}:delete`} onClick={() => confirmDeleteTask(task)}>删除</Button>
          <Button size="small" onClick={() => loadDetail(task)}>详情</Button>
        </Space>
      ),
    },
  ];

  const planColumns: ColumnsType<TaskCenterAction> = [
    { title: '计划执行时间', dataIndex: 'scheduled_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '动作', dataIndex: 'action_type', width: 120, render: (value) => actionLabel(value) },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '目标', key: 'target', width: 180, render: (_, action) => actionTarget(action) },
    { title: '内容', key: 'content', ellipsis: true, render: (_, action) => actionContent(action) },
  ];

  const recordColumns: ColumnsType<TaskCenterAction> = [
    { title: '动作', dataIndex: 'action_type', width: 120, render: (value) => actionLabel(value) },
    { title: '计划执行时间', dataIndex: 'scheduled_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '实际执行时间', dataIndex: 'executed_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '目标', key: 'target', width: 180, render: (_, action) => actionTarget(action) },
    { title: '内容', key: 'content', ellipsis: true, render: (_, action) => actionContent(action) },
    { title: '结果', key: 'result', width: 220, render: (_, action) => actionResult(action) },
  ];

  const messageColumns: ColumnsType<TaskCenterDetail['message_groups'][number]> = [
    {
      title: '频道',
      key: 'channel',
      width: 220,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.channel_title || '-'}</Typography.Text>
          {item.channel_username && <Typography.Text type="secondary">@{item.channel_username}</Typography.Text>}
        </Space>
      ),
    },
    {
      title: '消息',
      key: 'message',
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>#{item.message_id ?? '-'}</Typography.Text>
          <Typography.Text type="secondary" ellipsis>{item.content_preview || item.message_url || '-'}</Typography.Text>
        </Space>
      ),
    },
    { title: '动作', dataIndex: 'action_label', width: 100 },
    { title: '目标', dataIndex: 'target_count', width: 80 },
    { title: '完成', dataIndex: 'completed_count', width: 80 },
    { title: '失败', dataIndex: 'failed_count', width: 80 },
    { title: '重复', dataIndex: 'duplicate_count', width: 80 },
    { title: '运行中', dataIndex: 'running_count', width: 90 },
    { title: '缺口', dataIndex: 'capacity_shortfall', width: 80 },
    { title: '状态', dataIndex: 'subtask_status', width: 100, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '账号', key: 'accounts', width: 220, render: (_, item) => Array.from(new Set(item.actions.map((action) => accountDisplay(detail, action.account_id)).filter(Boolean))).join('、') || '-' },
    { title: '最近错误', key: 'last_error', width: 220, render: (_, item) => item.stats.last_error || '-' },
  ];

  const aiCycleColumns: ColumnsType<TaskCenterDetail['ai_cycles'][number]> = [
    { title: 'Cycle', dataIndex: 'cycle_id', width: 260 },
    { title: '上下文消息', key: 'context', width: 120, render: (_, item) => item.context_message_ids.length },
    { title: 'Turn', key: 'turns', width: 90, render: (_, item) => item.stats.total ?? item.turns.length },
    { title: '成功', key: 'success', width: 80, render: (_, item) => item.stats.success ?? 0 },
    { title: '失败', key: 'failed', width: 80, render: (_, item) => item.stats.failed ?? 0 },
    { title: '运行中', key: 'pending', width: 90, render: (_, item) => (item.stats.pending ?? 0) + (item.stats.executing ?? 0) },
  ];

  const aiTurnColumns: ColumnsType<TaskCenterDetail['ai_cycles'][number]['turns'][number]> = [
    { title: 'Turn', dataIndex: 'turn_index', width: 80 },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '角色', dataIndex: 'account_role', width: 140 },
    { title: '账号记忆', dataIndex: 'account_memory', width: 260, ellipsis: true, render: (value) => value || '-' },
    { title: '长期画像', dataIndex: 'account_profile', width: 260, ellipsis: true, render: (value) => value || '-' },
    { title: '话题脉络', dataIndex: 'topic_thread', width: 280, ellipsis: true, render: (value) => value || '-' },
    { title: '话题计划', dataIndex: 'topic_plan', width: 280, ellipsis: true, render: (value) => value || '-' },
    { title: '意图', dataIndex: 'intent', width: 140 },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '内容', dataIndex: 'content', ellipsis: true },
    { title: '结果', key: 'result', width: 220, render: (_, turn) => turn.result?.error_message || (turn.result?.success === true ? '成功' : '-') },
  ];

  const aiGenerationColumns: ColumnsType<TaskCenterDetail['ai_generation_records'][number]> = [
    { title: '生成记录', dataIndex: 'generation_id', width: 260 },
    { title: '生成状态', dataIndex: 'status', width: 110, render: (value) => <TaskStatusBadge status={value || 'success'} /> },
    { title: '生成条数', dataIndex: 'generated_count', width: 100 },
    { title: 'Token', dataIndex: 'token_count', width: 100 },
    { title: '上下文', dataIndex: 'context_message_count', width: 90 },
    { title: '账号记忆', dataIndex: 'account_memory_count', width: 100 },
    { title: '生成时间', dataIndex: 'created_at', width: 190, render: (value) => formatDateTime(value) },
  ];

  const aiAccountProfileColumns: ColumnsType<TaskCenterDetail['ai_account_profiles'][number]> = [
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '总成功', dataIndex: 'total_success_count', width: 90 },
    { title: '当前任务', dataIndex: 'current_task_success_count', width: 90 },
    { title: '跨任务', dataIndex: 'cross_task_success_count', width: 90 },
    { title: '画像摘要', dataIndex: 'profile_summary', ellipsis: true },
  ];

  const relayBatchColumns: ColumnsType<TaskCenterDetail['relay_batches'][number]> = [
    { title: '转发批次', dataIndex: 'relay_batch_id', width: 280 },
    { title: '发送项', key: 'items', width: 90, render: (_, item) => item.stats.total ?? item.items.length },
    { title: '源事件', dataIndex: 'source_event_count', width: 90 },
    { title: '素材', dataIndex: 'material_count', width: 80 },
    { title: '规则版本', dataIndex: 'rule_version_count', width: 100 },
    { title: '成功', key: 'success', width: 80, render: (_, item) => item.stats.success ?? 0 },
    { title: '失败', key: 'failed', width: 80, render: (_, item) => item.stats.failed ?? 0 },
    { title: '运行中', key: 'pending', width: 90, render: (_, item) => (item.stats.pending ?? 0) + (item.stats.executing ?? 0) },
  ];

  const operationTargetDisplay = (targetId?: number | null) => {
    if (!targetId) return '-';
    const target = targets.find((item) => item.id === targetId);
    return target ? `${target.title} #${target.id}` : `#${targetId}`;
  };
  const relayRuleDisplay = (item: TaskCenterDetail['relay_batches'][number]['items'][number]) => {
    if (item.rule_set_name || item.rule_set_version || item.rule_set_version_id) {
      const version = item.rule_set_version ? `v${item.rule_set_version}` : item.rule_set_version_id ? `#${item.rule_set_version_id}` : '';
      return [item.rule_set_name || (item.rule_set_id ? `规则集 #${item.rule_set_id}` : ''), version].filter(Boolean).join(' / ') || '-';
    }
    if (item.rule_set_id) {
      const ruleSet = ruleSets.find((rule) => rule.id === item.rule_set_id);
      return ruleSet ? `${ruleSet.name}${ruleSet.active_version_id ? ` / #${ruleSet.active_version_id}` : ''}` : `#${item.rule_set_id}`;
    }
    return '系统默认';
  };
  const relaySourceDisplay = (item: TaskCenterDetail['relay_batches'][number]['items'][number]) => {
    const source = item.source_group_title || item.source_info?.split(' / ')[0] || (item.source_group_id ? `源群 #${item.source_group_id}` : '-');
    const sender = item.source_sender_name || item.source_info?.split(' / ')[1] || '未知成员';
    return `${source} / ${sender}`;
  };

  const relayItemColumns: ColumnsType<TaskCenterDetail['relay_batches'][number]['items'][number]> = [
    { title: '源群 / 发送人', key: 'source', width: 220, ellipsis: true, render: (_, item) => relaySourceDisplay(item) },
    { title: '发送人ID', dataIndex: 'source_sender_peer_id', width: 130, ellipsis: true, render: (value) => value || '-' },
    { title: '源消息ID', dataIndex: 'source_remote_message_id', width: 120, ellipsis: true, render: (value) => value || '-' },
    { title: '源时间', dataIndex: 'source_sent_at', width: 170, render: (value) => formatDateTime(value) },
    { title: '规则', key: 'rule', width: 180, ellipsis: true, render: (_, item) => relayRuleDisplay(item) },
    { title: '规则命中', key: 'rule_trace', width: 220, ellipsis: true, render: (_, item) => item.rule_trace?.summary || '-' },
    { title: '目标', key: 'target', width: 180, ellipsis: true, render: (_, item) => item.target_display || operationTargetDisplay(item.operation_target_id) },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <TaskStatusBadge status={value} /> },
    { title: '执行时间', dataIndex: 'executed_at', width: 170, render: (value) => formatDateTime(value) },
    { title: '原文', dataIndex: 'original_text', width: 260, ellipsis: true },
    { title: '转换后', dataIndex: 'transformed_text', width: 260, ellipsis: true },
    { title: '重试', dataIndex: 'retry_count', width: 80 },
    { title: '结果', key: 'result', width: 220, render: (_, item) => item.result?.error_message || (item.result?.success === true ? '成功' : '-') },
  ];

  const formValues = Form.useWatch([], form) ?? {};
  const plannedActions = detail?.actions.filter(isPlannedAction) ?? [];
  const executedActions = detail?.actions.filter((action) => !isPlannedAction(action)) ?? [];

  return (
    <>
      <Space className="stats-grid" wrap>
        <StatCard label="任务总数" value={tasks.length} detail="5 类型" icon={<Activity size={20} />} />
        <StatCard label="执行中" value={tasks.filter((task) => task.status === 'running').length} detail="正在调度" icon={<RefreshCcw size={20} />} />
        <StatCard label="失败任务" value={tasks.filter((task) => task.status === 'failed').length} detail="需处理" icon={<Activity size={20} />} />
      </Space>
      <Card className="panel" title="任务中心" extra={<Button type="primary" loading={supportLoading} onClick={() => void openCreateTask()}>创建任务</Button>}>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        {actionWarning && <Alert className="form-alert" type="warning" showIcon message={actionWarning} />}
        <Space className="toolbar-row" wrap>{table.searchInput}<Button loading={loading} onClick={load}>刷新</Button></Space>
        <Table<TaskCenterTask> className="tg-table" rowKey="id" columns={columns} dataSource={table.filteredRows} pagination={table.pagination} scroll={{ x: 1380 }} loading={loading} />
      </Card>

      <Modal className="tg-modal large" title="创建任务" open={modalOpen} width={980} footer={null} destroyOnHidden centered onCancel={() => setModalOpen(false)}>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        {actionWarning && <Alert className="form-alert" type="warning" showIcon message={actionWarning} />}
        <Steps className="wizard-steps" current={wizardStep} items={WIZARD_STEPS.map((title) => ({ title }))} />
        <Form form={form} layout="vertical" initialValues={initialValuesForType(taskType, schedulingSetting)}>
          {wizardStep === 0 && <WizardBasics taskType={taskType} onTypeChange={resetTypeFields} />}
          {wizardStep === 1 && <WizardTarget taskType={taskType} groupTargets={groupTargets} channelTargets={channelTargets} messages={messages} messageScope={messageScope} targetChannelId={targetChannelId} onTargetChannelChange={() => form.setFieldsValue({ message_ids: [], reply_to_message_ids: [] })} />}
          {wizardStep === 2 && <WizardTypeConfig taskType={taskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} targetChannelId={targetChannelId} messageScope={messageScope} messageIds={messageIds} />}
          {wizardStep === 3 && <WizardAccounts accountMode={accountMode} accounts={accounts} accountPools={accountPools} includeAdvanced pacingMode={pacingMode} />}
          {wizardStep === 4 && <WizardReview taskType={taskType} values={formValues} />}
          <Space className="modal-actions">
            <Button onClick={() => setModalOpen(false)}>取消</Button>
            <Button disabled={wizardStep === 0} onClick={() => setWizardStep((value) => Math.max(value - 1, 0))}>上一步</Button>
            {wizardStep < WIZARD_STEPS.length - 1 ? (
              <Button type="primary" onClick={nextStep}>下一步</Button>
            ) : (
              <>
                <Button onClick={() => createTask({ start: false })}>保存草稿</Button>
                <Button type="primary" onClick={() => createTask({ start: true })}>创建并启动</Button>
              </>
            )}
          </Space>
        </Form>
      </Modal>

      <Modal className="tg-modal large" title="编辑任务" open={editOpen} width={980} confirmLoading={editSaving} okText="保存并重新规划" cancelText="取消" onOk={saveTaskSettings} onCancel={() => setEditOpen(false)} destroyOnHidden centered>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        <Form form={editForm} layout="vertical">
          <EditBasics />
          {detail && ['group_ai_chat', 'group_relay'].includes(detail.task.type) && (
            <>
              <Typography.Title level={5}>目标来源</Typography.Title>
              <WizardTarget taskType={detail.task.type} groupTargets={groupTargets} channelTargets={channelTargets} messages={messages} messageScope={editMessageScope} targetChannelId={editTargetChannelId} onTargetChannelChange={() => editForm.setFieldsValue({ message_ids: [], reply_to_message_ids: [] })} />
            </>
          )}
          <Typography.Title level={5}>类型参数</Typography.Title>
          <WizardTypeConfig taskType={detail?.task.type ?? taskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} targetChannelId={editTargetChannelId} messageScope={editMessageScope} messageIds={editMessageIds} />
          <Typography.Title level={5}>账号选择</Typography.Title>
          <WizardAccounts accountMode={editAccountMode} accounts={accounts} accountPools={accountPools} />
          <Typography.Title level={5}>节奏策略</Typography.Title>
          <WizardPacing pacingMode={editPacingMode} />
        </Form>
      </Modal>

      <DetailModal title={detail?.task.name ?? '任务详情'} open={Boolean(detail)} size="wide" extra={detail && <Space><Button loading={supportLoading} onClick={() => void openEditTask(detail.task)}>编辑任务</Button><Button onClick={() => loadDetail(detail.task)}>刷新</Button></Space>} onClose={() => setDetail(null)}>
        {detail && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              column={3}
              size="small"
              items={[
                { key: 'type', label: '类型', children: TYPE_LABEL[detail.task.type] },
                { key: 'status', label: '状态', children: <TaskStatusBadge status={detail.task.status} /> },
                { key: 'target', label: '目标', children: detail.task.target_summary || '-' },
                { key: 'planned', label: '计划中', children: plannedActions.filter((action) => action.status === 'pending').length },
                { key: 'executing', label: '执行中', children: detail.actions.filter((action) => action.status === 'executing').length },
                { key: 'success', label: '已成功', children: detail.stats.success_count ?? 0 },
                { key: 'failure', label: '失败', children: detail.stats.failure_count ?? 0 },
                { key: 'skipped', label: '跳过', children: detail.stats.skipped_count ?? 0 },
                { key: 'total', label: '总动作', children: detail.stats.total_actions ?? 0 },
                { key: 'next', label: '下次运行', children: formatDateTime(detail.task.next_run_at) },
                ...(detail.task.type === 'group_ai_chat' ? [
                  { key: 'idle-enabled', label: '无人发言续聊', children: detail.task.type_config?.idle_continuation_enabled === false ? '关闭' : '开启' },
                  { key: 'idle-seconds', label: '续聊间隔', children: `${detail.task.type_config?.idle_continuation_seconds ?? 300} 秒` },
                  { key: 'context-mode', label: '上下文状态', children: detail.task.stats?.context_mode || '-' },
                ] : []),
                { key: 'capacity', label: '容量提示', span: 3, children: detail.stats.capacity_warning ? `${detail.stats.capacity_warning} 该提示不会停止任务。` : '无' },
                  { key: 'mode', label: '执行口径', span: 3, children: detail.task.type === 'group_ai_chat' || detail.task.type === 'group_relay' ? '自动校验通过后自动发送，无需人工确认。' : '按频道消息生成动作子任务，执行项按账号留痕。' },
                  { key: 'error', label: '错误', span: 3, children: detail.task.last_error || '无' },
              ]}
            />
            {detail.ai_cycles.length > 0 && (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Title level={5} style={{ margin: 0 }}>AI 活跃循环 Cycle / Turn</Typography.Title>
                {detail.ai_generation_records.length > 0 && (
                  <Table<TaskCenterDetail['ai_generation_records'][number]>
                    rowKey="generation_id"
                    columns={aiGenerationColumns}
                    dataSource={detail.ai_generation_records}
                    pagination={false}
                    size="small"
                    scroll={{ x: 950 }}
                  />
                )}
                {detail.ai_account_profiles.length > 0 && (
                  <Table<TaskCenterDetail['ai_account_profiles'][number]>
                    rowKey="account_id"
                    columns={aiAccountProfileColumns}
                    dataSource={detail.ai_account_profiles}
                    pagination={false}
                    size="small"
                    scroll={{ x: 900 }}
                  />
                )}
                <Table<TaskCenterDetail['ai_cycles'][number]>
                  rowKey="cycle_id"
                  columns={aiCycleColumns}
                  dataSource={detail.ai_cycles}
                  pagination={false}
                  scroll={{ x: 820 }}
                  expandable={{
                    expandedRowRender: (item) => (
                      <Table<TaskCenterDetail['ai_cycles'][number]['turns'][number]>
                        rowKey="action_id"
                        columns={aiTurnColumns}
                        dataSource={item.turns}
                        pagination={false}
                        size="small"
                        scroll={{ x: 1540 }}
                      />
                    ),
                  }}
                />
              </Space>
            )}
            {detail.relay_batches.length > 0 && (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Title level={5} style={{ margin: 0 }}>转发监听批次 / 源事件</Typography.Title>
                <Table<TaskCenterDetail['relay_batches'][number]>
                  rowKey="relay_batch_id"
                  columns={relayBatchColumns}
                  dataSource={detail.relay_batches}
                  pagination={false}
                  scroll={{ x: 820 }}
                  expandable={{
                    expandedRowRender: (item) => (
                      <Table<TaskCenterDetail['relay_batches'][number]['items'][number]>
                        rowKey="action_id"
                        columns={relayItemColumns}
                        dataSource={item.items}
                        pagination={false}
                        size="small"
                        scroll={{ x: 2200 }}
                      />
                    ),
                  }}
                />
              </Space>
            )}
            {detail.message_groups.length > 0 && (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Typography.Title level={5} style={{ margin: 0 }}>频道消息执行明细</Typography.Title>
                <Table<TaskCenterDetail['message_groups'][number]>
                  rowKey={(item) => `${item.channel_target_id ?? 'channel'}:${item.message_id ?? 'message'}`}
                  columns={messageColumns}
                  dataSource={detail.message_groups}
                  pagination={{ pageSize: 6 }}
                  scroll={{ x: 1180 }}
                  expandable={{
                    expandedRowRender: (item) => (
                      <Table<TaskCenterAction>
                        rowKey="id"
                        columns={recordColumns}
                        dataSource={item.actions}
                        pagination={false}
                        size="small"
                        scroll={{ x: 1180 }}
                      />
                    ),
                  }}
                />
              </Space>
            )}
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>执行计划</Typography.Title>
              <Table<TaskCenterAction>
                rowKey="id"
                columns={planColumns}
                dataSource={plannedActions}
                pagination={{ pageSize: 8 }}
                scroll={{ x: 980 }}
                locale={{ emptyText: detail.task.last_error ? `暂未生成执行计划：${detail.task.last_error}` : `暂未生成执行计划，下次运行：${formatDateTime(detail.task.next_run_at)}` }}
              />
            </Space>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>执行记录</Typography.Title>
              <Table<TaskCenterAction>
                rowKey="id"
                columns={recordColumns}
                dataSource={executedActions}
                pagination={{ pageSize: 8 }}
                scroll={{ x: 1180 }}
                locale={{ emptyText: '暂无已执行记录' }}
              />
            </Space>
          </Space>
        )}
      </DetailModal>
    </>
  );
}

function fieldsForStep(step: number, taskType: TaskCenterTaskType, messageScope: string, accountMode: string): string[] {
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

function accountSelectionFields(accountMode: string): string[] {
  const fields = ['selection_mode'];
  if (accountMode === 'group') fields.push('account_group_id');
  if (accountMode === 'manual') fields.push('account_ids');
  return fields;
}

function accountFields(accountMode: string): string[] {
  const fields = ['selection_mode', 'max_concurrent', 'cooldown_per_account_minutes', 'ban_policy'];
  if (accountMode === 'group') fields.push('account_group_id');
  if (accountMode === 'manual') fields.push('account_ids');
  return fields;
}

function pacingFields(pacingMode: string): string[] {
  const fields = ['pacing_mode', 'jitter_percent', 'max_actions_per_hour', 'max_actions_per_day', 'quiet_enabled', 'quiet_start', 'quiet_end', 'max_retries', 'retry_delay_seconds', 'retry_backoff', 'on_account_banned', 'on_api_rate_limit', 'on_content_rejected'];
  if (pacingMode === 'template') fields.push('template');
  if (pacingMode === 'fixed') fields.push('interval_seconds_min', 'interval_seconds_max');
  if (pacingMode === 'curve') fields.push('curve_type', 'curve_duration_hours');
  return fields;
}

function channelScopeFields(messageScope: string): string[] {
  const fields = ['target_channel_id', 'message_scope'];
  if (['latest_n', 'dynamic_new'].includes(messageScope)) fields.push('message_count');
  if (messageScope === 'specific') fields.push('message_ids');
  if (messageScope === 'date_range') fields.push('date_from', 'date_to');
  return fields;
}

function fieldsForSubmit(taskType: TaskCenterTaskType, messageScope: string, accountMode: string, pacingMode: string): string[] {
  const commonFields = ['name', 'scheduled_end'];
  const baseFields = [...commonFields, ...accountFields(accountMode), ...pacingFields(pacingMode)];
  if (taskType === 'group_ai_chat') {
    return [
      ...baseFields,
      'target_operation_target_id',
      'rule_set_id',
      'rule_set_version_id',
      'topic_hint',
      'chat_history_depth',
      'ai_model',
      'system_prompt_override',
      'slang_prompt_template_id',
      'slang_terms',
      'tone',
      'language',
      'max_message_length',
      'participation_rate',
      'participation_jitter',
      'allow_account_repeat',
      'repeat_cooldown_rounds',
      'account_personas',
      'account_memory_depth',
      'messages_per_round_mode',
      'messages_per_round',
      'history_fetch_account_id',
      'idle_continuation_enabled',
      'idle_continuation_seconds',
      'silent_mode_enabled',
      'silent_start',
      'silent_end',
      'silent_max_accounts',
      'silent_messages_per_round',
      'ramp_up_minutes',
      'ramp_start_ratio',
      'context_expire_after_messages',
    ];
  }
  if (taskType === 'group_relay') {
    return [
      ...baseFields,
      'source_operation_target_ids',
      'rule_set_id',
      'rule_set_version_id',
      'monitor_account_ids',
      'target_operation_target_id',
      'target_operation_target_ids',
      'content_mode',
      'rewrite_prompt',
      'keyword_whitelist',
      'keyword_blacklist',
      'min_message_length',
      'max_message_length',
      'allowed_media_types',
      'blocked_user_ids',
      'only_with_media',
      'only_text',
      'language_filter',
      'preserve_media',
      'add_source_attribution',
      'dedup_window_minutes',
      'dedup_method',
    ];
  }
  if (taskType === 'channel_view') {
    return [...baseFields, ...channelScopeFields(messageScope), 'target_views_per_message', 'view_count_jitter', 'execution_mode'];
  }
  if (taskType === 'channel_like') {
    return [...baseFields, ...channelScopeFields(messageScope), 'target_likes_per_message', 'like_count_jitter', 'reaction_type', 'allowed_reactions', 'max_likes_per_account_per_hour'];
  }
  return [...baseFields, ...channelScopeFields(messageScope), 'target_comments_per_message', 'comment_count_jitter', 'comment_mode', 'reply_to_message_ids', 'rule_set_id', 'rule_set_version_id', 'ai_model', 'comment_style', 'topic_hint', 'system_prompt_override', 'language', 'max_comment_length', 'max_comments_per_account_per_hour'];
}

function editFieldsForSubmit(taskType: TaskCenterTaskType, accountMode: string, pacingMode: string): string[] {
  const baseFields = ['name', 'scheduled_end', ...accountFields(accountMode), ...pacingFields(pacingMode)];
  if (taskType === 'group_ai_chat') {
    return [...baseFields, 'target_operation_target_id', 'rule_set_id', 'rule_set_version_id', 'topic_hint', 'chat_history_depth', 'ai_model', 'system_prompt_override', 'slang_prompt_template_id', 'slang_terms', 'tone', 'language', 'max_message_length', 'participation_rate', 'participation_jitter', 'allow_account_repeat', 'repeat_cooldown_rounds', 'account_personas', 'account_memory_depth', 'messages_per_round_mode', 'messages_per_round', 'history_fetch_account_id', 'idle_continuation_enabled', 'idle_continuation_seconds', 'silent_mode_enabled', 'silent_start', 'silent_end', 'silent_max_accounts', 'silent_messages_per_round', 'ramp_up_minutes', 'ramp_start_ratio', 'context_expire_after_messages'];
  }
  if (taskType === 'group_relay') {
    return [...baseFields, 'source_operation_target_ids', 'source_groups', 'target_operation_target_id', 'target_operation_target_ids', 'rule_set_id', 'rule_set_version_id', 'monitor_account_ids', 'content_mode', 'rewrite_prompt', 'keyword_whitelist', 'keyword_blacklist', 'min_message_length', 'max_message_length', 'allowed_media_types', 'blocked_user_ids', 'only_with_media', 'only_text', 'language_filter', 'preserve_media', 'add_source_attribution', 'dedup_window_minutes', 'dedup_method'];
  }
  if (taskType === 'channel_view') {
    return [...baseFields, 'target_views_per_message', 'view_count_jitter', 'execution_mode'];
  }
  if (taskType === 'channel_like') {
    return [...baseFields, 'target_likes_per_message', 'like_count_jitter', 'reaction_type', 'allowed_reactions', 'max_likes_per_account_per_hour'];
  }
  return [...baseFields, 'target_comments_per_message', 'comment_count_jitter', 'comment_mode', 'reply_to_message_ids', 'rule_set_id', 'rule_set_version_id', 'ai_model', 'comment_style', 'topic_hint', 'system_prompt_override', 'language', 'max_comment_length', 'max_comments_per_account_per_hour'];
}

function EditBasics() {
  return (
    <>
      <Typography.Title level={5}>基础信息</Typography.Title>
      <div className="form-grid">
        <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>
      </div>
      <Form.Item name="priority" hidden><InputNumber /></Form.Item>
      <Form.Item name="timezone" hidden><Input /></Form.Item>
      <Form.Item name="scheduled_start" hidden><Input /></Form.Item>
    </>
  );
}

function WizardBasics({ taskType, onTypeChange }: { taskType: TaskCenterTaskType; onTypeChange: (type: TaskCenterTaskType) => void }) {
  return (
    <div className="form-grid">
      <Form.Item label="任务类型">
        <Select options={TASK_TYPES} value={taskType} onChange={onTypeChange} />
      </Form.Item>
      <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input /></Form.Item>
      <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>
    </div>
  );
}

function WizardTarget({ taskType, groupTargets, channelTargets, messages, messageScope, targetChannelId, onTargetChannelChange }: { taskType: TaskCenterTaskType; groupTargets: OperationTarget[]; channelTargets: OperationTarget[]; messages: ChannelMessage[]; messageScope: string; targetChannelId?: number; onTargetChannelChange: () => void }) {
  const groupTargetOptions = groupTargets
    .filter((target) => target.auth_status === '已授权运营')
    .map((target) => ({
      value: target.id,
      label: `${target.title} / 可发账号 ${target.available_send_account_count} / 监听账号 ${target.listener_account_count}`,
    }));
  const sendableGroupTargetOptions = groupTargetOptions.filter((option) => groupTargets.find((target) => target.id === option.value)?.can_send);
  if (taskType === 'group_ai_chat') {
    return <div className="form-grid"><Form.Item name="target_operation_target_id" label="运营目标群" rules={[{ required: true }]}><Select options={sendableGroupTargetOptions} /></Form.Item></div>;
  }
  if (taskType === 'group_relay') {
    return (
      <div className="form-grid">
        <Form.Item name="source_operation_target_ids" label="源群运营目标" rules={[{ required: true }]}><Select mode="multiple" options={groupTargetOptions} /></Form.Item>
        <Form.Item name="target_operation_target_id" label="默认目标群" rules={[{ required: true }]}><Select options={sendableGroupTargetOptions} /></Form.Item>
        <Form.Item name="target_operation_target_ids" label="附加目标群"><Select mode="multiple" allowClear options={sendableGroupTargetOptions} /></Form.Item>
      </div>
    );
  }
  const scopedMessages = messages.filter((message) => !targetChannelId || message.channel_target_id === targetChannelId);
  return (
    <div className="form-grid">
      <Form.Item name="target_channel_id" label="目标频道" rules={[{ required: true }]}><Select options={channelTargets.map((target) => ({ value: target.id, label: target.title }))} onChange={onTargetChannelChange} /></Form.Item>
      <Form.Item name="message_scope" label="消息范围"><Select options={[{ value: 'dynamic_new', label: '持续监听新消息' }, { value: 'latest_n', label: '最新 N 条' }, { value: 'all', label: '所有消息' }, { value: 'date_range', label: '日期范围' }, { value: 'specific', label: '指定消息' }]} /></Form.Item>
      {['latest_n', 'dynamic_new'].includes(messageScope) && <Form.Item name="message_count" label={messageScope === 'dynamic_new' ? '每轮采集上限' : '消息数量'} rules={[{ required: true }]}><InputNumber min={1} max={500} /></Form.Item>}
      {messageScope === 'specific' && <Form.Item name="message_ids" label="频道消息" rules={[{ required: true }]}><Select mode="multiple" options={scopedMessages.map((message) => ({ value: message.id, label: `#${message.message_id} / ${message.content_preview || message.message_url || message.id}` }))} /></Form.Item>}
      {messageScope === 'date_range' && <><Form.Item name="date_from" label="开始时间"><Input type="datetime-local" /></Form.Item><Form.Item name="date_to" label="结束时间"><Input type="datetime-local" /></Form.Item></>}
    </div>
  );
}

function WizardTypeConfig({
  taskType,
  ruleSets = [],
  slangTemplates = [],
  comments = [],
  targetChannelId,
  messageScope = 'latest_n',
  messageIds,
}: {
  taskType: TaskCenterTaskType;
  ruleSets?: RuleSet[];
  slangTemplates?: PromptTemplate[];
  comments?: ChannelMessageComment[];
  targetChannelId?: number;
  messageScope?: string;
  messageIds?: Array<number | string> | string | null;
}) {
  const versionOptions = ruleSets.flatMap((ruleSet) => ruleSet.versions.filter((version) => version.status !== 'draft').map((version) => ({
    value: version.id,
    label: `${ruleSet.name} / v${version.version} / ${version.status === 'published' ? '已发布' : '历史版本'}`,
  })));
  const ruleFields = (
    <div className="form-grid">
      <Form.Item name="rule_set_id" label="规则集">
        <Select allowClear options={ruleSets.map((ruleSet) => ({ value: ruleSet.id, label: ruleSet.name }))} />
      </Form.Item>
      <Form.Item name="rule_set_version_id" label="规则版本">
        <Select allowClear options={versionOptions} />
      </Form.Item>
    </div>
  );
  const slangOptions = slangTemplates.map((template) => ({
    value: template.id,
    label: `${template.name} / v${template.version}`,
  }));
  if (taskType === 'group_ai_chat') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="info" showIcon message="AI 回复会按绑定规则集先过滤输入上下文，再逐条校验候选回复。" />
        {ruleFields}
        <div className="form-grid">
          <Form.Item name="topic_hint" label="话题方向（可选）"><Input.TextArea rows={2} placeholder="不填时系统会按群目标方向或自然开场自动起聊" /></Form.Item>
          <Form.Item name="slang_prompt_template_id" label="AI 黑话配置">
            <Select allowClear options={slangOptions} placeholder="选择系统设置里的 AI 黑话词表" />
          </Form.Item>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: 'advanced',
              label: '高级设置',
              children: (
                <div className="form-grid">
                  <Form.Item name="messages_per_round_mode" label="每轮发言"><Select options={[{ value: 'auto', label: '系统自动判定' }, { value: 'manual', label: '手动指定' }]} /></Form.Item>
                  <Form.Item name="messages_per_round" label="手动每轮发言数"><InputNumber min={1} max={10} /></Form.Item>
                  <Form.Item name="tone" label="语气"><Select options={[{ value: 'auto', label: '自动' }, { value: 'casual', label: '口语' }, { value: 'professional', label: '正式' }, { value: 'mixed', label: '混合' }]} /></Form.Item>
                  <Form.Item name="chat_history_depth" label="历史条数"><InputNumber min={1} max={200} /></Form.Item>
                  <Form.Item name="account_memory_depth" label="账号记忆条数"><InputNumber min={0} max={20} /></Form.Item>
                  <Form.Item name="participation_rate" label="参与率"><InputNumber min={0.01} max={1} step={0.05} /></Form.Item>
                  <Form.Item name="participation_jitter" label="参与抖动"><InputNumber min={0} max={1} step={0.05} /></Form.Item>
                  <Form.Item name="account_personas" label="账号角色">
                    <Input.TextArea rows={3} placeholder={'101=提问型账号\n102=补充细节账号'} />
                  </Form.Item>
                  <Form.Item name="idle_continuation_enabled" label="无人发言续聊"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="idle_continuation_seconds" label="续聊间隔秒数"><InputNumber min={30} max={86400} /></Form.Item>
                  <Form.Item name="silent_mode_enabled" label="静默期"><Select options={[{ value: true, label: '启用低频模式' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="silent_start" label="静默开始"><Input /></Form.Item>
                  <Form.Item name="silent_end" label="静默结束"><Input /></Form.Item>
                  <Form.Item name="silent_max_accounts" label="静默最多账号"><InputNumber min={1} max={50} /></Form.Item>
                  <Form.Item name="silent_messages_per_round" label="静默每轮发言"><InputNumber min={1} max={10} /></Form.Item>
                  <Form.Item name="ramp_up_minutes" label="爬坡分钟"><InputNumber min={0} max={1440} /></Form.Item>
                  <Form.Item name="ramp_start_ratio" label="启动比例"><InputNumber min={0.01} max={1} step={0.05} /></Form.Item>
                  <Form.Item name="context_expire_after_messages" label="上下文过期消息数"><InputNumber min={0} max={500} /></Form.Item>
                </div>
              ),
            },
          ]}
        />
      </Space>
    );
  }
  if (taskType === 'group_relay') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={ruleSets.length ? '已加载默认运营规则集，可直接绑定任务使用。' : '正在初始化默认运营规则集。'}
        />
        <Collapse
          defaultActiveKey={['rules', 'filters', 'rewrite']}
          items={[
            {
              key: 'rules',
              label: '规则集',
              children: (
                ruleFields
              ),
            },
            {
              key: 'filters',
              label: '关键词与内容过滤',
              children: (
                <div className="form-grid">
                  <Form.Item name="keyword_whitelist" label="关键词白名单">
                    <Input.TextArea rows={2} placeholder="逗号或换行分隔；为空表示不限制" />
                  </Form.Item>
                  <Form.Item name="keyword_blacklist" label="关键词黑名单">
                    <Input.TextArea rows={2} placeholder="逗号或换行分隔；命中后跳过" />
                  </Form.Item>
                  <Form.Item name="min_message_length" label="最小长度"><InputNumber min={0} /></Form.Item>
                  <Form.Item name="max_message_length" label="最大长度"><InputNumber min={1} /></Form.Item>
                  <Form.Item name="allowed_media_types" label="允许媒体类型"><Input placeholder="photo, video, text" /></Form.Item>
                  <Form.Item name="blocked_user_ids" label="屏蔽用户 ID"><Input placeholder="逗号或换行分隔" /></Form.Item>
                  <Form.Item name="language_filter" label="语言过滤"><Input placeholder="如 zh-CN；为空不限制" /></Form.Item>
                  <Form.Item name="only_text" valuePropName="checked"><Checkbox>只转发文本消息</Checkbox></Form.Item>
                  <Form.Item name="only_with_media" valuePropName="checked"><Checkbox>只转发带媒体消息</Checkbox></Form.Item>
                </div>
              ),
            },
            {
              key: 'rewrite',
              label: '去重与改写',
              children: (
                <div className="form-grid">
                  <Form.Item name="content_mode" label="内容处理">
                    <Select options={[{ value: 'raw', label: '原文' }, { value: 'light_rewrite', label: '轻量改写' }, { value: 'ai_rewrite', label: 'AI 改写' }, { value: 'summary', label: '摘要' }]} />
                  </Form.Item>
                  <Form.Item name="dedup_window_minutes" label="去重窗口分钟"><InputNumber min={1} /></Form.Item>
                  <Form.Item name="dedup_method" label="去重方式"><Select options={[{ value: 'hash', label: '文本指纹' }, { value: 'semantic', label: '语义近似' }, { value: 'both', label: '文本+语义' }]} /></Form.Item>
                  <Form.Item name="preserve_media" valuePropName="checked"><Checkbox>保留原媒体</Checkbox></Form.Item>
                  <Form.Item name="add_source_attribution" valuePropName="checked"><Checkbox>附加来源标识</Checkbox></Form.Item>
                  <Form.Item name="rewrite_prompt" label="改写提示词">
                    <Input.TextArea rows={3} placeholder="仅在 AI 改写或摘要模式下使用；为空则使用系统默认改写策略" />
                  </Form.Item>
                </div>
              ),
            },
          ]}
        />
      </Space>
    );
  }
  if (taskType === 'channel_view') {
    return <div className="form-grid"><Form.Item name="target_views_per_message" label="每条目标浏览"><InputNumber min={1} /></Form.Item><Form.Item name="view_count_jitter" label="浏览抖动"><InputNumber min={0} max={1} step={0.05} /></Form.Item><Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'distribute', label: '均匀分配' }, { value: 'burst', label: '尽快完成' }]} /></Form.Item></div>;
  }
  if (taskType === 'channel_like') {
    return <div className="form-grid"><Form.Item name="target_likes_per_message" label="每条目标点赞"><InputNumber min={1} /></Form.Item><Form.Item name="like_count_jitter" label="点赞抖动"><InputNumber min={0} max={1} step={0.05} /></Form.Item><Form.Item name="allowed_reactions" label="Emoji"><Input /></Form.Item><Form.Item name="max_likes_per_account_per_hour" label="每号每小时点赞上限"><InputNumber min={1} /></Form.Item></div>;
  }
  const selectedMessageIds = new Set(csvNumbers(messageIds));
  const commentOptions = comments
    .filter((comment) => {
      if (targetChannelId && comment.channel_target_id !== targetChannelId) return false;
      if (messageScope === 'specific') return selectedMessageIds.size > 0 && selectedMessageIds.has(comment.channel_message_id);
      return true;
    })
    .map((comment) => ({
      value: comment.comment_message_id,
      label: `消息#${comment.channel_message_id} / 评论#${comment.comment_message_id} / ${comment.author_name || '未知用户'} / ${comment.content_preview || '无内容预览'}`,
    }));
  return (
    <div className="form-grid">
      <div style={{ gridColumn: '1 / -1' }}>
        <Alert type="info" showIcon message="AI 评论会按绑定规则集逐条做输出校验，单条失败不会废弃整批评论。" />
      </div>
      <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
      <Form.Item name="target_comments_per_message" label="每条目标评论/回复"><InputNumber min={1} /></Form.Item>
      <Form.Item name="comment_count_jitter" label="评论抖动"><InputNumber min={0} max={1} step={0.05} /></Form.Item>
      <Form.Item name="comment_mode" label="互动方式"><Select options={[{ value: 'comment', label: '评论频道消息' }, { value: 'reply', label: '回复指定评论' }, { value: 'mixed', label: '评论+回复' }]} /></Form.Item>
      <Form.Item name="reply_to_message_ids" label="回复对象">
        <Select
          mode="multiple"
          allowClear
          showSearch
          placeholder="选择当前频道消息下已采集评论"
          options={commentOptions}
        />
      </Form.Item>
      <Form.Item name="comment_style" label="评论风格"><Select options={[{ value: 'mixed', label: '混合' }, { value: 'relevant', label: '相关' }, { value: 'question', label: '提问' }, { value: 'praise', label: '正向' }, { value: 'discussion', label: '讨论' }]} /></Form.Item>
      <Form.Item name="topic_hint" label="主题指导"><Input /></Form.Item>
      <Form.Item name="max_comments_per_account_per_hour" label="每号每小时评论上限"><InputNumber min={1} /></Form.Item>
    </div>
  );
}

function WizardAccounts({ accountMode, accounts, accountPools, includeAdvanced = false, pacingMode = 'template' }: { accountMode: string; accounts: Account[]; accountPools: AccountPool[]; includeAdvanced?: boolean; pacingMode?: string }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div className="form-grid">
        <Form.Item name="selection_mode" label="账号选择"><Select options={[{ value: 'all', label: '全部账号' }, { value: 'group', label: '账号分组' }, { value: 'manual', label: '手动选择' }]} /></Form.Item>
        {accountMode === 'group' && <Form.Item name="account_group_id" label="账号分组" rules={[{ required: true }]}><Select options={accountPools.map((pool) => ({ value: pool.id, label: `${pool.name} (${pool.account_count})` }))} /></Form.Item>}
        {accountMode === 'manual' && <Form.Item name="account_ids" label="账号" rules={[{ required: true }]}><Select mode="multiple" options={accounts.map((account) => ({ value: account.id, label: `${account.display_name} / ${account.status}` }))} /></Form.Item>}
      </div>
      {includeAdvanced && (
        <Collapse
          ghost
          items={[
            {
              key: 'advanced',
              label: '高级设置',
              children: (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <div className="form-grid">
                    <Form.Item name="max_concurrent" label="最大并发"><InputNumber min={1} max={500} /></Form.Item>
                    <Form.Item name="cooldown_per_account_minutes" label="账号冷却(分钟)"><InputNumber min={0} /></Form.Item>
                    <Form.Item name="ban_policy" label="异常账号处理"><Select options={[{ value: 'skip', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'alert', label: '只告警' }]} /></Form.Item>
                  </div>
                  <WizardPacing pacingMode={pacingMode} />
                </Space>
              ),
            },
          ]}
        />
      )}
    </Space>
  );
}

function WizardPacing({ pacingMode }: { pacingMode: string }) {
  return (
    <div className="form-grid">
      <Form.Item name="pacing_mode" label="节奏模式"><Select options={[{ value: 'template', label: '预设模板' }, { value: 'fixed', label: '固定间隔' }, { value: 'curve', label: '曲线' }]} /></Form.Item>
      {pacingMode === 'template' && <Form.Item name="template" label="模板"><Select options={[{ value: 'aggressive_1h', label: '激进 1h' }, { value: 'moderate_6h', label: '中等 6h' }, { value: 'gentle_24h', label: '温和 24h' }, { value: 'burst_30min', label: '爆发 30min' }]} /></Form.Item>}
      {pacingMode === 'fixed' && <><Form.Item name="interval_seconds_min" label="最小间隔秒"><InputNumber min={0} /></Form.Item><Form.Item name="interval_seconds_max" label="最大间隔秒"><InputNumber min={0} /></Form.Item></>}
      {pacingMode === 'curve' && <><Form.Item name="curve_type" label="曲线"><Select options={[{ value: 'front_heavy', label: '前密后疏' }, { value: 'back_heavy', label: '前疏后密' }, { value: 'random_burst', label: '随机突发' }, { value: 'steady', label: '稳定均匀' }]} /></Form.Item><Form.Item name="curve_duration_hours" label="曲线时长"><InputNumber min={1} /></Form.Item></>}
      <Form.Item name="jitter_percent" label="抖动百分比"><InputNumber min={0} max={100} /></Form.Item>
      <Form.Item name="max_actions_per_hour" label="每小时上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="max_actions_per_day" label="每日上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="quiet_enabled" label="全局静默"><Select options={[{ value: true, label: '启用' }, { value: false, label: '关闭' }]} /></Form.Item>
      <Form.Item name="quiet_start" label="静默开始"><Input /></Form.Item>
      <Form.Item name="quiet_end" label="静默结束"><Input /></Form.Item>
      <Form.Item name="max_retries" label="重试次数"><InputNumber min={0} max={10} /></Form.Item>
      <Form.Item name="retry_delay_seconds" label="重试间隔秒"><InputNumber min={0} /></Form.Item>
      <Form.Item name="retry_backoff" label="退避"><Select options={[{ value: 'none', label: '固定' }, { value: 'linear', label: '线性' }, { value: 'exponential', label: '指数' }]} /></Form.Item>
      <Form.Item name="on_account_banned" label="账号异常"><Select options={[{ value: 'skip_account', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'stop_task', label: '停止任务' }]} /></Form.Item>
      <Form.Item name="on_api_rate_limit" label="API 限流"><Select options={[{ value: 'wait_and_retry', label: '等待重试' }, { value: 'skip', label: '跳过' }, { value: 'pause', label: '暂停' }]} /></Form.Item>
      <Form.Item name="on_content_rejected" label="内容拦截"><Select options={[{ value: 'skip_message', label: '跳过消息' }, { value: 'rewrite_and_retry', label: '改写重试' }, { value: 'pause', label: '暂停' }]} /></Form.Item>
    </div>
  );
}

function WizardReview({ taskType, values }: { taskType: TaskCenterTaskType; values: Record<string, any> }) {
  const targetSummary = taskType === 'group_relay'
    ? values.target_operation_target_ids?.length
      ? `运营目标 #${values.target_operation_target_id || '-'} + ${values.target_operation_target_ids.length} 个附加目标`
      : `运营目标 #${values.target_operation_target_id || '-'}`
    : values.target_operation_target_id
      ? `运营目标 #${values.target_operation_target_id}`
      : values.target_channel_id
        ? `频道 #${values.target_channel_id}`
        : '-';
  return (
    <Descriptions bordered column={2} size="small" items={[
      { key: 'type', label: '任务类型', children: TYPE_LABEL[taskType] },
      { key: 'name', label: '任务名称', children: values.name || '-' },
      { key: 'end', label: '结束时间', children: values.scheduled_end ? formatDateTime(values.scheduled_end) : '不限制' },
      { key: 'target', label: '目标', children: targetSummary },
      { key: 'account', label: '账号方式', children: values.selection_mode || 'all' },
      { key: 'pacing', label: '节奏', children: values.pacing_mode || 'template' },
      { key: 'rule', label: '规则版本', children: taskType === 'group_relay' ? (values.rule_set_version_id || values.rule_set_id || '系统默认') : '-' },
      { key: 'mode', label: '执行方式', children: '自动校验后自动执行' },
    ]} />
  );
}
