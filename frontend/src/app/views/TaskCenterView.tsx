import React from 'react';
import { Alert, Button, Card, Collapse, Form, Input, Modal, Select, Space, Steps, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, CirclePause, CirclePlay, RefreshCcw } from 'lucide-react';
import { api, apiWithMeta, ApiError, API_BASE } from '../../shared/api/client';
import type { Account, AccountPool, ChannelMessage, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, SchedulingSetting, TaskCenterAction, TaskCenterAnyTaskType, TaskCenterDetail, TaskCenterPrefill, TaskCenterTask, TaskCenterTaskType, TaskExecutionAttempt, TaskMembershipItem, TaskPrecheck } from '../types';
import { StatusBadge, StatCard, useAntdTableControls } from '../components/shared';
import { fromBeijingDateTimeLocalValue } from '../time';
import {
  CREATE_AND_START_ENDPOINT,
  CREATE_ENDPOINT,
  GROUP_AI_HARD_HOURLY_MIN_MESSAGES,
  TASK_TYPES,
  TYPE_LABEL,
  WIZARD_STEPS,
  accountDisplay,
  actionContent,
  actionLabel,
  actionResult,
  actionStatusLabel,
  actionTarget,
  csvNumbers,
  currentOperationProfile,
  curveNumbers,
  curveText,
  defaultRuleSelection,
  editFieldsForSubmit,
  errorMessage,
  fieldsForStep,
  fieldsForSubmit,
  formatDateTime,
  formatKeyValueMap,
  formatPrecheckReasons,
  hardHourlyStats,
  hardHourlyStatusColor,
  hardHourlyStatusLabel,
  initialValuesForType,
  isPlannedAction,
  normalizePromptTemplateType,
  operationProfileFromValues,
  operationTemplate,
  parseKeyValueMap,
  runtimeStage,
  runtimeStageLabel,
  statusLabel,
  toDateTimeLocal,
  words,
} from './taskCenterViewModel';
import { buildTaskQuickGroups, filterTasksByQuickGroup } from './taskCenterListGrouping';
import { EditBasics, TaskRuntimeAdvancedFields, WizardAccounts, WizardBasics, WizardOperationProfile, WizardReview, WizardTarget, WizardTypeConfig } from './TaskCenterWizardSections';
import { TaskCenterDetailModal } from './TaskCenterDetailModal';

function TaskStatusBadge({ task, status }: { task?: TaskCenterTask; status?: string | null }) {
  const stage = task ? runtimeStage(task) : null;
  return (
    <span className={`task-status-indicator task-status-${task?.status || status || 'unknown'}`}>
      <StatusBadge status={stage?.stage_label || status} label={stage?.stage_label || statusLabel(status)} />
    </span>
  );
}

function ActionStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={actionStatusLabel(status)} />;
}

function HardHourlyTaskSummary({ task }: { task: TaskCenterTask }) {
  const stats = hardHourlyStats(task);
  if (!stats) return null;
  const goal = Number(stats.hard_hourly_goal ?? task.type_config?.hourly_min_messages ?? 0);
  const success = Number(stats.hard_hourly_success_count ?? 0);
  const deficit = Number(stats.hard_hourly_deficit ?? Math.max(0, goal - success));
  return (
    <Space direction="vertical" size={0}>
      <Typography.Text type="secondary">本小时硬目标 {success} / {goal || '-'}</Typography.Text>
      <Space size={6}>
        <Typography.Text type="secondary">缺口 {deficit}</Typography.Text>
        <Tag color={hardHourlyStatusColor(stats.hard_hourly_status)}>{hardHourlyStatusLabel(stats.hard_hourly_status)}</Tag>
      </Space>
    </Space>
  );
}

function MembershipTaskSummary({ task }: { task: TaskCenterTask }) {
  const stats = task.stats || {};
  const ready = Number(stats.membership_joined_count ?? stats.membership_summary?.joined_account_count ?? 0);
  const pending = Number(stats.membership_need_join_count ?? stats.membership_summary?.need_join_account_count ?? 0);
  const failed = Number(stats.membership_failed_count ?? stats.membership_summary?.failed_account_count ?? 0);
  const windowHours = Number(stats.membership_schedule_window_hours ?? stats.membership_summary?.schedule_window_hours ?? 0);
  if (!ready && !pending && !failed && !windowHours) return null;
  const windowLabel = windowHours > 0 ? `，${windowHours} 小时内排完` : '';
  return (
    <Typography.Text type="secondary">
      加入账号前置任务：已可发 {ready}，待准备 {pending}，失败 {failed}
      {windowLabel}
    </Typography.Text>
  );
}

function hardHourlyEditValues(config: Record<string, any>): Record<string, any> {
  const configured = Number(config.hourly_min_messages);
  const hourlyMin = Number.isFinite(configured)
    ? Math.max(GROUP_AI_HARD_HOURLY_MIN_MESSAGES, configured)
    : GROUP_AI_HARD_HOURLY_MIN_MESSAGES;
  return {
    hard_hourly_target_enabled: true,
    hourly_min_messages: hourlyMin,
    hard_hourly_strategy: 'force_planning',
  };
}

function taskListTitle(task: TaskCenterTask): string {
  if (task.type !== 'group_ai_chat') return task.name;
  return task.target_summary || task.type_config?.target_group_name || task.name;
}

function failureDiagnosis(action: TaskCenterAction) {
  const diagnosis = action.failure_diagnosis ?? {};
  if (!diagnosis.operator_summary && !diagnosis.suggested_action) return null;
  return diagnosis;
}

function actionReplyTarget(action: TaskCenterAction) {
  const payload = action.payload ?? {};
  const isCommentAction = action.task_type === 'channel_comment' || action.action_type === 'post_comment';
  if (!payload.reply_to_message_id) return <Tag>{isCommentAction ? '普通评论' : '普通发言'}</Tag>;
  const label = payload.reply_target_author || payload.reply_target_label || `#${payload.reply_to_message_id}`;
  return <Space size={4}><Tag color="blue">{isCommentAction ? '回复评论' : '引用回复'}</Tag><Typography.Text ellipsis>{label}：{payload.reply_target_preview || '-'}</Typography.Text></Space>;
}

type DangerousTaskAction = 'stop' | 'reset' | 'delete';
type TaskTypeFilter = TaskCenterAnyTaskType | 'all';
type AiLimitRecommendation = NonNullable<TaskPrecheck['capacity_summary']['recommended_limits']>;
type AiLimitRecommendationField = 'max_actions_per_hour' | 'messages_per_round' | 'target_comments_per_message' | 'max_comments_per_account_per_hour' | 'current_hour_rounds' | 'estimated_hourly_capacity';
type AiLimitTaskType = Extract<TaskCenterTaskType, 'group_ai_chat' | 'channel_comment'>;
type MembershipPageState = { current: number; pageSize: number; total: number; loading: boolean };
type MembershipFilters = { phase: string; manualRequired: string };
const TASK_CREATE_TIMEOUT_MS = 120_000;
const MEMBERSHIP_PAGE_SIZE = 20;
const TASK_GROUP_SELECT_WIDTH = 360;
const TASK_GROUP_DROPDOWN_WIDTH = 480;
const DEFAULT_MEMBERSHIP_FILTERS: MembershipFilters = { phase: 'all', manualRequired: 'all' };
const GROUP_AI_RECOMMENDATION_FIELDS: AiLimitRecommendationField[] = ['max_actions_per_hour', 'messages_per_round'];
const COMMENT_AI_RECOMMENDATION_FIELDS: AiLimitRecommendationField[] = ['max_actions_per_hour', 'target_comments_per_message', 'max_comments_per_account_per_hour'];

const TASK_TYPE_FILTER_OPTIONS: Array<{ value: TaskTypeFilter; label: string }> = [
  { value: 'all', label: '全部类型' },
  ...TASK_TYPES,
  { value: 'account_profile_init', label: TYPE_LABEL.account_profile_init },
  { value: 'account_device_cleanup', label: TYPE_LABEL.account_device_cleanup },
  { value: 'account_2fa_setup', label: TYPE_LABEL.account_2fa_setup },
  { value: 'account_standby_session_provision', label: TYPE_LABEL.account_standby_session_provision },
];

function isAiLimitTaskType(type?: TaskCenterTaskType | string | null): type is AiLimitTaskType {
  return type === 'group_ai_chat' || type === 'channel_comment';
}

function aiLimitRecommendationFields(type: AiLimitTaskType) {
  return type === 'group_ai_chat' ? GROUP_AI_RECOMMENDATION_FIELDS : COMMENT_AI_RECOMMENDATION_FIELDS;
}

function recommendedLimitSummary(recommendations?: AiLimitRecommendation | null) {
  if (!recommendations) return '暂无推荐数量';
  const labels: Record<AiLimitRecommendationField, string> = {
    max_actions_per_hour: '每小时',
    messages_per_round: '每轮',
    target_comments_per_message: '每条累计',
    max_comments_per_account_per_hour: '每号每小时',
    current_hour_rounds: '当前轮数',
    estimated_hourly_capacity: '理论小时容量',
  };
  const parts = (Object.keys(labels) as AiLimitRecommendationField[])
    .map((field) => typeof recommendations[field] === 'number' ? `${labels[field]} ${recommendations[field]}` : '')
    .filter(Boolean);
  return parts.length ? parts.join('；') : '暂无推荐数量';
}

interface DangerousTaskState {
  task: TaskCenterTask;
  action: DangerousTaskAction;
  title: string;
  content: string;
  okText: string;
}

export default function TaskCenterView({
  accounts,
  accountPools,
  promptTemplates,
  prefill,
  focusTask,
  onFocusTaskConsumed,
  canManageTasks = false,
  canDispatchControl = false,
  onOpenAccountDetail,
}: {
  accounts: Account[];
  accountPools: AccountPool[];
  promptTemplates: PromptTemplate[];
  prefill?: TaskCenterPrefill | null;
  focusTask?: { taskId: string; nonce: number } | null;
  onFocusTaskConsumed?: () => void;
  canManageTasks?: boolean;
  canDispatchControl?: boolean;
  onOpenAccountDetail?: (accountId: number, tab?: string) => void | Promise<void>;
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
  const [dangerAction, setDangerAction] = React.useState<DangerousTaskState | null>(null);
  const [dangerReason, setDangerReason] = React.useState('');
  const [attemptDetail, setAttemptDetail] = React.useState<{ action: TaskCenterAction; attempts: TaskExecutionAttempt[]; loading: boolean } | null>(null);
  const [precheck, setPrecheck] = React.useState<TaskPrecheck | null>(null);
  const [precheckLoading, setPrecheckLoading] = React.useState(false);
  const [editRecommendation, setEditRecommendation] = React.useState<AiLimitRecommendation | null>(null);
  const [editRecommendationLoading, setEditRecommendationLoading] = React.useState(false);
  const [membershipPage, setMembershipPage] = React.useState<MembershipPageState>({ current: 1, pageSize: MEMBERSHIP_PAGE_SIZE, total: 0, loading: false });
  const [membershipFilters, setMembershipFilters] = React.useState<MembershipFilters>(DEFAULT_MEMBERSHIP_FILTERS);
  const [wizardStep, setWizardStep] = React.useState(0);
  const [taskType, setTaskType] = React.useState<TaskCenterTaskType>('group_ai_chat');
  const [taskTypeFilter, setTaskTypeFilter] = React.useState<TaskTypeFilter>('all');
  const [selectedTaskGroupId, setSelectedTaskGroupId] = React.useState('all');
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const appliedPrefillNonce = React.useRef<number | null>(null);
  const appliedFocusNonce = React.useRef<number | null>(null);
  const accountMode = Form.useWatch('selection_mode', form) ?? 'all';
  const pacingMode = Form.useWatch('pacing_mode', form) ?? 'template';
  const editAccountMode = Form.useWatch('selection_mode', editForm) ?? 'all';
  const editMessageScope = Form.useWatch('message_scope', editForm) ?? 'latest_n';
  const editTargetChannelId = Form.useWatch('target_channel_id', editForm);
  const messageIds = Form.useWatch('message_ids', form);
  const editMessageIds = Form.useWatch('message_ids', editForm);
  const messageScope = Form.useWatch('message_scope', form) ?? 'latest_n';
  const targetChannelId = Form.useWatch('target_channel_id', form);
  const channelTargets = targets.filter((target) => target.target_type === 'channel');
  const groupTargets = targets.filter((target) => target.target_type === 'group');
  const slangTemplates = promptTemplates.filter((template) => normalizePromptTemplateType(template.template_type) === 'AI黑话词表' && template.is_active);
  const defaultSlangTemplateId = slangTemplates[0]?.id ?? null;

  async function load(nextTaskTypeFilter: TaskTypeFilter = taskTypeFilter) {
    const params = new URLSearchParams();
    if (nextTaskTypeFilter !== 'all') params.set('type', nextTaskTypeFilter);
    const query = params.toString();
    setLoading(true);
    try {
      const [taskData, schedulingData] = await Promise.all([
        api<TaskCenterTask[]>(`/tasks${query ? `?${query}` : ''}`),
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
    void load(taskTypeFilter);
    const timer = window.setInterval(() => void load(taskTypeFilter), 60000);
    return () => window.clearInterval(timer);
  }, [taskTypeFilter]);

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

  React.useEffect(() => {
    if (!focusTask || appliedFocusNonce.current === focusTask.nonce) return;
    appliedFocusNonce.current = focusTask.nonce;
    setActionError('');
    fetchTaskDetail(focusTask.taskId)
      .then((taskDetail) => {
        setDetail(taskDetail);
        void loadMembershipForDetail(taskDetail, 1, MEMBERSHIP_PAGE_SIZE, DEFAULT_MEMBERSHIP_FILTERS);
      })
      .catch(() => setActionError(`读取任务 ${focusTask.taskId} 详情失败`))
      .finally(() => onFocusTaskConsumed?.());
  }, [focusTask, onFocusTaskConsumed]);

  async function fetchTaskDetail(taskId: string) {
    return api<TaskCenterDetail>(`/tasks/${taskId}`);
  }

  async function loadMembershipForDetail(taskDetail: TaskCenterDetail, page: number, pageSize: number, filters: MembershipFilters = membershipFilters) {
    if (isSystemTask(taskDetail.task)) {
      setMembershipPage({ current: 1, pageSize: MEMBERSHIP_PAGE_SIZE, total: 0, loading: false });
      return taskDetail;
    }
    try {
      const membershipItems = await fetchMembershipItems(taskDetail.task.id, page, pageSize, filters);
      const nextDetail = { ...taskDetail, membership_accounts: membershipItems };
      setDetail((current) => current && current.task.id === taskDetail.task.id ? nextDetail : current);
      return nextDetail;
    } catch (error) {
      setActionError(`读取准入前置失败：${errorMessage(error)}`);
      return taskDetail;
    }
  }

  async function loadDetail(task: TaskCenterTask) {
    setMembershipFilters(DEFAULT_MEMBERSHIP_FILTERS);
    setActionError('');
    try {
      const taskDetail = await fetchTaskDetail(task.id);
      setDetail(taskDetail);
      await loadMembershipForDetail(taskDetail, 1, membershipPage.pageSize, DEFAULT_MEMBERSHIP_FILTERS);
    } catch (error) {
      setActionError(`读取任务 ${task.id} 详情失败：${errorMessage(error)}`);
    }
  }

  async function fetchMembershipItems(taskId: string, page: number, pageSize: number, filters: MembershipFilters = membershipFilters) {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (filters.phase !== 'all') params.set('phase', filters.phase);
    if (filters.manualRequired === 'true') params.set('manual_required', 'true');
    if (filters.manualRequired === 'false') params.set('manual_required', 'false');
    setMembershipPage((current) => ({ ...current, current: page, pageSize, loading: true }));
    try {
      const response = await apiWithMeta<TaskMembershipItem[]>(`/tasks/${taskId}/membership-items?${params.toString()}`);
      const total = Number(response.headers.get('X-Total-Count') || response.data.length);
      setMembershipPage({ current: page, pageSize, total, loading: false });
      return response.data;
    } catch (error) {
      setMembershipPage((current) => ({ ...current, loading: false }));
      throw error;
    }
  }

  async function loadMembershipPage(page: number, pageSize: number, filters: MembershipFilters = membershipFilters) {
    if (!detail || isSystemTask(detail.task)) return;
    try {
      const membershipItems = await fetchMembershipItems(detail.task.id, page, pageSize, filters);
      setDetail((current) => current && current.task.id === detail.task.id ? { ...current, membership_accounts: membershipItems } : current);
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  function updateMembershipFilters(filters: MembershipFilters) {
    setMembershipFilters(filters);
    void loadMembershipPage(1, membershipPage.pageSize, filters);
  }

  function isSystemTask(task: TaskCenterTask | null | undefined) {
    return task?.type === 'account_profile_init'
      || task?.type === 'account_device_cleanup'
      || task?.type === 'account_2fa_setup'
      || task?.type === 'account_standby_session_provision';
  }

  function canDeleteTask(task: TaskCenterTask) {
    return canManageTasks && Boolean(task.id) && !isSystemTask(task);
  }

  function canStartTask(task: TaskCenterTask) {
    return !isSystemTask(task) && task.status !== 'running';
  }

  function canPauseTask(task: TaskCenterTask) {
    return !isSystemTask(task) && task.status === 'running';
  }

  async function openActionAttempts(action: TaskCenterAction) {
    setAttemptDetail({ action, attempts: [], loading: true });
    try {
      const attempts = await api<TaskExecutionAttempt[]>(`/tasks/${action.task_id}/actions/${action.id}/attempts`);
      setAttemptDetail({ action, attempts, loading: false });
    } catch (error) {
      setAttemptDetail(null);
      throw error;
    }
  }

  async function openCreateTask() {
    setActionError('');
    setActionWarning('');
    setPrecheck(null);
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
    const operationProfile = pacing.operation_profile || {};
    const operationTemplateId = operationProfile.template_id ?? 'natural_full_day';
    const operationCurve = curveNumbers(operationProfile.hourly_activity_curve ?? operationTemplate(operationTemplateId).curve);
    return {
      ...initialValuesForType(task.type as TaskCenterTaskType, schedulingSetting),
      name: task.name,
      priority: task.priority,
      timezone: task.timezone,
      scheduled_start: toDateTimeLocal(task.scheduled_start),
      scheduled_end: toDateTimeLocal(task.scheduled_end),
      ...config,
      ...(task.type === 'group_ai_chat' ? hardHourlyEditValues(config) : {}),
      selection_mode: account.selection_mode ?? 'all',
      account_group_id: account.account_group_id ?? null,
      account_ids: account.account_ids ?? [],
      max_concurrent: account.max_concurrent ?? 20,
      cooldown_per_account_minutes: account.cooldown_per_account_minutes ?? 5,
      ban_policy: account.ban_policy ?? 'skip',
      pacing_mode: 'template',
      max_actions_per_hour: pacing.max_actions_per_hour ?? null,
      operation_template_id: operationTemplateId,
      hourly_activity_curve: curveText(operationCurve),
      operation_profile_manual_override: Boolean(operationProfile.manual_override),
      quiet_threshold: operationProfile.quiet_threshold ?? 2,
      peak_threshold: operationProfile.peak_threshold ?? 8,
      max_retries: failure.max_retries ?? 3,
      retry_delay_seconds: failure.retry_delay_seconds ?? 60,
      retry_backoff: failure.retry_backoff ?? 'exponential',
      on_account_banned: failure.on_account_banned ?? 'skip_account',
      on_api_rate_limit: failure.on_api_rate_limit ?? 'wait_and_retry',
      on_content_rejected: failure.on_content_rejected ?? 'skip_message',
      source_operation_target_ids: Array.isArray(config.source_groups)
        ? config.source_groups.map((item: any) => item?.operation_target_id).filter(Boolean)
        : [],
      account_personas: formatKeyValueMap(config.account_personas),
      slang_terms: formatKeyValueMap(config.slang_terms),
      slang_prompt_template_id: task.type === 'group_ai_chat' ? (config.slang_prompt_template_id ?? defaultSlangTemplateId) : (config.slang_prompt_template_id ?? null),
      filter_bot_messages: task.type === 'group_relay' ? config.filter_bot_messages !== false : config.filter_bot_messages,
      filter_admin_messages: task.type === 'group_relay' ? Boolean(config.filter_admin_messages) : config.filter_admin_messages,
      excluded_sender_peer_ids: Array.isArray(config.excluded_sender_peer_ids) ? config.excluded_sender_peer_ids : [],
      excluded_sender_input: formatExcludedSenderInput(config),
      allowed_reactions: Array.isArray(config.allowed_reactions) ? config.allowed_reactions.join(',') : config.allowed_reactions,
      max_message_length: config.max_message_length ?? null,
    };
  }

  async function openEditTask(task: TaskCenterTask) {
    if (isSystemTask(task)) return;
    setActionError('');
    setActionWarning('');
    setEditRecommendation(null);
    const editableType = task.type as TaskCenterTaskType;
    setTaskType(editableType);
    await ensureTaskFormData(editableType);
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
    const config: Record<string, any> = {
      mode: values.pacing_mode ?? 'template',
      operation_profile: operationProfileFromValues(values),
      max_actions_per_hour: values.max_actions_per_hour ?? null,
    };
    return config;
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

  function applyAiLimitRecommendations(result: TaskPrecheck) {
    const recommendations = result.capacity_summary?.recommended_limits;
    if (!recommendations || !isAiLimitTaskType(taskType)) return;
    const fields = aiLimitRecommendationFields(taskType);
    const nextValues: Record<string, number> = {};
    fields.forEach((field) => {
      const value = recommendations[field as keyof typeof recommendations];
      if (typeof value === 'number' && !form.isFieldTouched(field)) nextValues[field] = value;
    });
    if (Object.keys(nextValues).length) form.setFieldsValue(nextValues);
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
      target_channel_id: values.target_channel_id ?? null,
      target_type: 'channel',
      target_input: values.target_input?.trim() || null,
      target_title: values.target_title?.trim() || '',
      target_channel_name: channel?.title ?? '',
      message_scope: values.message_scope ?? 'latest_n',
      message_count: ['latest_n', 'dynamic_new'].includes(values.message_scope) ? values.message_count ?? 10 : null,
      date_from: fromBeijingDateTimeLocalValue(values.date_from),
      date_to: fromBeijingDateTimeLocalValue(values.date_to),
      message_ids: values.message_scope === 'specific' ? csvNumbers(values.message_ids) : [],
    };
  }

  function channelViewProductionPayload(values: any) {
    const dailyTarget = values.per_message_daily_view_target ?? values.target_views_per_message ?? 50;
    return {
      initial_message_scope: values.message_scope === 'dynamic_new' ? 'new_only' : values.message_scope ?? 'latest_n',
      latest_message_count: ['latest_n', 'dynamic_new'].includes(values.message_scope) ? values.message_count ?? 10 : null,
      listen_new_messages: values.listen_new_messages !== false,
      per_message_daily_view_target: dailyTarget,
      per_message_total_view_target: values.per_message_total_view_target ?? Math.max(300, dailyTarget),
      message_active_days: values.message_active_days ?? 3,
      task_daily_view_safety_cap: values.task_daily_view_safety_cap ?? 500,
      max_views_per_account_per_day: values.max_views_per_account_per_day ?? 20,
      target_views_per_message: dailyTarget,
      execution_mode: values.execution_mode ?? 'distribute',
    };
  }

  function channelCommentPayload(values: any, base: Record<string, any>, includeScope: boolean) {
    const payload: Record<string, any> = {
      ...base,
      ...(includeScope ? channelScopePayload(values) : {}),
      reply_min_per_message: values.reply_min_per_message ?? 0,
      rule_set_id: values.rule_set_id ?? null,
      rule_set_version_id: values.rule_set_version_id ?? null,
      ai_model: values.ai_model ?? '',
      comment_style: values.comment_style ?? 'mixed',
      topic_hint: values.topic_hint ?? '',
      system_prompt_override: values.system_prompt_override ?? '',
      language: values.language ?? 'zh-CN',
      max_comment_length: values.max_comment_length ?? null,
      require_review: false,
    };
    if (values.target_comments_per_message != null) payload.target_comments_per_message = values.target_comments_per_message;
    if (values.max_comments_per_account_per_hour != null) payload.max_comments_per_account_per_hour = values.max_comments_per_account_per_hour;
    return payload;
  }

  function parseExcludedSenderInput(value?: string) {
    const result = { peerIds: [] as string[], usernames: [] as string[], names: [] as string[] };
    String(value ?? '')
      .split(/\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const lower = line.toLowerCase();
        if (line.startsWith('@')) {
          result.usernames.push(line.replace(/^@+/, '').trim());
        } else if (/^(id|peer|peer_id|sender_peer_id)[:=]/i.test(line)) {
          result.peerIds.push(line.replace(/^[^:=]+[:=]/, '').trim());
        } else if (/^-?\d+$/.test(line) || /^(account|user|bot)[:_-]/i.test(line)) {
          result.peerIds.push(line);
        } else if (lower) {
          result.names.push(line);
        }
      });
    return {
      peerIds: Array.from(new Set(result.peerIds.filter(Boolean))),
      usernames: Array.from(new Set(result.usernames.map((item) => item.replace(/^@+/, '').trim()).filter(Boolean))),
      names: Array.from(new Set(result.names.filter(Boolean))),
    };
  }

  function relaySourceFilterPayload(values: any) {
    const parsed = parseExcludedSenderInput(values.excluded_sender_input);
    const selectedPeerIds = Array.isArray(values.excluded_sender_peer_ids) ? values.excluded_sender_peer_ids.map(String) : [];
    return {
      filter_bot_messages: values.filter_bot_messages !== false,
      filter_admin_messages: Boolean(values.filter_admin_messages),
      excluded_sender_peer_ids: Array.from(new Set([...selectedPeerIds, ...parsed.peerIds].map((item) => item.trim()).filter(Boolean))),
      excluded_sender_usernames: parsed.usernames,
      excluded_sender_names: parsed.names,
    };
  }

  function formatExcludedSenderInput(config: any): string {
    const usernames = (Array.isArray(config.excluded_sender_usernames) ? config.excluded_sender_usernames : []).map((item: string) => `@${String(item).replace(/^@+/, '')}`);
    const names = Array.isArray(config.excluded_sender_names) ? config.excluded_sender_names : [];
    return [...usernames, ...names].filter(Boolean).join('\n');
  }

  function membershipStrategyPayload(values: any) {
    return {
      auto_join_target: values.auto_join_target !== false,
      auto_follow_required_channel: values.auto_follow_required_channel !== false,
      auto_resolve_verification: values.auto_resolve_verification !== false,
      ai_assisted_verification: values.ai_assisted_verification !== false,
      captcha_failure_policy: values.captcha_failure_policy ?? 'manual',
      membership_max_concurrent: values.membership_max_concurrent ?? 5,
    };
  }

  function hardHourlyTargetPayload(values: any) {
    const requested = Number(values.hourly_min_messages);
    const hourlyMin = Number.isFinite(requested) ? requested : GROUP_AI_HARD_HOURLY_MIN_MESSAGES;
    return {
      hard_hourly_target_enabled: true,
      hourly_min_messages: Math.max(GROUP_AI_HARD_HOURLY_MIN_MESSAGES, hourlyMin),
      hard_hourly_strategy: 'force_planning',
    };
  }

  function createPayload(values: any): Record<string, any> {
    const base = commonPayload(values);
    if (taskType === 'group_membership_admission') {
      return {
        ...base,
        scheduled_start: fromBeijingDateTimeLocalValue(values.scheduled_start),
        scheduled_end: fromBeijingDateTimeLocalValue(values.scheduled_end),
        account_config: { selection_mode: 'all', account_group_id: null, account_ids: [], max_concurrent: values.admission_max_concurrent ?? 5, cooldown_per_account_minutes: 0, ban_policy: 'skip' },
        pacing_config: { mode: 'template', operation_profile: operationProfileFromValues(values), max_actions_per_hour: values.admission_per_minute ? Number(values.admission_per_minute) * 60 : null },
        target_operation_target_id: values.target_operation_target_id,
        account_group_ids: csvNumbers(values.account_group_ids),
        admission_pacing: {
          mode: 'spread',
          max_concurrent: values.admission_max_concurrent ?? 5,
          per_minute: values.admission_per_minute ?? 10,
        },
        test_message: {
          mode: 'ai_random',
          min_chars: values.test_message_min_chars ?? 3,
          max_chars: values.test_message_max_chars ?? 12,
          delete_after_send: Boolean(values.delete_after_send),
        },
      };
    }
    if (taskType === 'group_ai_chat') {
      const target = groupTargets.find((item) => item.id === values.target_operation_target_id);
      return {
        ...base,
        target_operation_target_id: values.target_operation_target_id ?? null,
        target_type: 'group',
        target_input: values.target_input?.trim() || null,
        target_title: values.target_title?.trim() || '',
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
        allow_account_repeat: values.allow_account_repeat ?? true,
        repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2,
        account_personas: parseKeyValueMap(values.account_personas),
        account_memory_depth: values.account_memory_depth ?? 3,
        messages_per_round_mode: values.messages_per_round_mode ?? 'auto',
        messages_per_round: values.messages_per_round ?? 1,
        reply_min_per_round: values.reply_min_per_round ?? 0,
        ...hardHourlyTargetPayload(values),
        history_fetch_account_id: values.history_fetch_account_id ?? null,
        ...membershipStrategyPayload(values),
        context_expire_after_messages: values.context_expire_after_messages ?? 10,
        idle_continuation_enabled: values.idle_continuation_enabled ?? true,
        idle_continuation_seconds: values.idle_continuation_seconds ?? 300,
      };
    }
    if (taskType === 'group_relay') {
      const sourceTargetIds = csvNumbers(values.source_operation_target_ids);
      const targetOperationIds = csvNumbers(values.target_operation_target_ids);
      const sourceGroups: any[] = sourceTargetIds.map((id) => {
        const target = groupTargets.find((item) => item.id === id);
        return { operation_target_id: id, group_name: target?.title ?? '', is_active: true };
      });
      if (values.source_target_input?.trim()) {
        sourceGroups.push({ target_input: values.source_target_input.trim(), target_title: values.source_target_input.trim(), group_name: values.source_target_input.trim(), is_active: true });
      }
      return {
        ...base,
        source_groups: sourceGroups,
        rule_set_id: values.rule_set_id ?? null,
        rule_set_version_id: values.rule_set_version_id ?? null,
        target_operation_target_id: values.target_operation_target_id ?? null,
        target_type: 'group',
        target_input: values.target_input?.trim() || null,
        target_title: values.target_title?.trim() || '',
        target_operation_target_ids: targetOperationIds,
        send_account_ids: [],
        content_mode: values.content_mode ?? 'light_rewrite',
        ...relaySourceFilterPayload(values),
        require_review: false,
      };
    }
    if (taskType === 'channel_view') {
      return { ...base, ...channelScopePayload(values), ...channelViewProductionPayload(values) };
    }
    if (taskType === 'channel_like') {
      return { ...base, ...channelScopePayload(values), target_likes_per_message: values.target_likes_per_message ?? 50, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return channelCommentPayload(values, base, true);
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
      return {
        ...base,
        target_operation_target_id: values.target_operation_target_id ?? null,
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
        allow_account_repeat: values.allow_account_repeat ?? true,
        repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2,
        account_personas: parseKeyValueMap(values.account_personas),
        account_memory_depth: values.account_memory_depth ?? 3,
        messages_per_round_mode: values.messages_per_round_mode ?? 'auto',
        messages_per_round: values.messages_per_round ?? 1,
        reply_min_per_round: values.reply_min_per_round ?? 0,
        ...hardHourlyTargetPayload(values),
        history_fetch_account_id: values.history_fetch_account_id ?? null,
        ...membershipStrategyPayload(values),
        idle_continuation_enabled: values.idle_continuation_enabled ?? true,
        idle_continuation_seconds: values.idle_continuation_seconds ?? 300,
        context_expire_after_messages: values.context_expire_after_messages ?? 10,
      };
    }
    if (type === 'group_relay') {
      const sourceTargetIds = csvNumbers(values.source_operation_target_ids);
      const targetOperationIds = csvNumbers(values.target_operation_target_ids);
      const sourceGroups = sourceTargetIds.length ? sourceTargetIds.map((id) => {
        const target = groupTargets.find((item) => item.id === id);
        return { operation_target_id: id, group_name: target?.title ?? '', is_active: true };
      }) : [...(values.source_groups ?? [])];
      return { ...base, source_groups: sourceGroups, target_operation_target_id: values.target_operation_target_id ?? null, target_operation_target_ids: targetOperationIds, rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, content_mode: values.content_mode ?? 'light_rewrite', ...relaySourceFilterPayload(values), require_review: false };
    }
    if (type === 'channel_view') {
      return { ...base, ...channelViewProductionPayload(values) };
    }
    if (type === 'channel_like') {
      return { ...base, target_likes_per_message: values.target_likes_per_message ?? 50, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return channelCommentPayload(values, base, false);
  }

  async function runTaskPrecheck(values: any) {
    setPrecheckLoading(true);
    try {
      const result = await api<TaskPrecheck>('/tasks/precheck', {
        method: 'POST',
        body: JSON.stringify({ task_type: taskType, payload: createPayload(values) }),
        timeoutMs: TASK_CREATE_TIMEOUT_MS,
      });
      setPrecheck(result);
      applyAiLimitRecommendations(result);
      if (result.decision === 'block') {
        setActionWarning(`预检发现阻塞项：${formatPrecheckReasons(result.blockers) || '请检查账号、目标和风控配置'}`);
      } else if (result.decision === 'warn') {
        setActionWarning(`预检有风险提示：${formatPrecheckReasons([...result.warnings, ...result.risk_hits], 3) || '建议确认后再启动'}`);
      } else {
        setActionWarning('');
      }
      return result;
    } finally {
      setPrecheckLoading(false);
    }
  }

  function applyEditAiLimitRecommendations() {
    if (!detail || isSystemTask(detail.task) || !editRecommendation) return;
    const editableType = detail.task.type as TaskCenterTaskType;
    if (!isAiLimitTaskType(editableType)) return;
    const nextValues: Record<string, number> = {};
    aiLimitRecommendationFields(editableType).forEach((field) => {
      const value = editRecommendation[field];
      if (typeof value === 'number') nextValues[field] = value;
    });
    if (Object.keys(nextValues).length) editForm.setFieldsValue(nextValues);
  }

  async function runEditAiLimitRecommendation() {
    if (!detail || isSystemTask(detail.task)) return;
    const editableType = detail.task.type as TaskCenterTaskType;
    if (!isAiLimitTaskType(editableType)) return;
    setEditRecommendationLoading(true);
    setActionError('');
    try {
      await editForm.validateFields(editFieldsForSubmit(editableType, editAccountMode, 'template'));
      const result = await api<TaskPrecheck>('/tasks/precheck', {
        method: 'POST',
        body: JSON.stringify({ task_type: editableType, payload: settingsPayload(editableType, editForm.getFieldsValue(true)) }),
        timeoutMs: TASK_CREATE_TIMEOUT_MS,
      });
      setEditRecommendation(result.capacity_summary?.recommended_limits ?? null);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setEditRecommendationLoading(false);
    }
  }

  async function createTask(options: { start?: boolean; skipCapacityCheck?: boolean } = {}) {
    const start = options.start ?? true;
    setActionError('');
    setActionWarning('');
    try {
      await form.validateFields(fieldsForSubmit(taskType, messageScope, accountMode, pacingMode));
      const values = form.getFieldsValue(true);
      const result = taskType !== 'group_membership_admission' && !options.skipCapacityCheck ? precheck ?? await runTaskPrecheck(values) : precheck;
      if (start && result?.decision === 'block') {
        setActionError(`预检未通过：${formatPrecheckReasons(result.blockers) || '存在阻塞项'}`);
        return;
      }
      const submitValues = form.getFieldsValue(true);
      await api<TaskCenterTask>((start ? CREATE_AND_START_ENDPOINT : CREATE_ENDPOINT)[taskType], {
        method: 'POST',
        body: JSON.stringify(createPayload(submitValues)),
        timeoutMs: TASK_CREATE_TIMEOUT_MS,
      });
      form.resetFields();
      setPrecheck(null);
      setTaskType('group_ai_chat');
      form.setFieldsValue(initialValuesForType('group_ai_chat', schedulingSetting));
      setWizardStep(0);
      setModalOpen(false);
      await load();
    } catch (error) {
      if (error instanceof ApiError && error.status === 408) {
        await load();
      }
      setActionError(errorMessage(error));
    }
  }

  async function saveTaskSettings() {
    if (!detail) return;
    if (isSystemTask(detail.task)) return;
    setEditSaving(true);
    setActionError('');
    setActionWarning('');
    try {
      const editableType = detail.task.type as TaskCenterTaskType;
      await editForm.validateFields(editFieldsForSubmit(editableType, editAccountMode, 'template'));
      const values = editForm.getFieldsValue(true);
      const updated = await api<TaskCenterTask>(`/tasks/${detail.task.id}/settings`, { method: 'PATCH', body: JSON.stringify(settingsPayload(editableType, values)) });
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

  async function taskAction(task: TaskCenterTask, name: 'start' | 'pause' | 'resume' | 'stop' | 'retry' | 'reset', reason?: string) {
    setBusyId(`${task.id}:${name}`);
    setActionError('');
    try {
      const body = name === 'retry'
        ? JSON.stringify({ failed_only: true })
        : ['stop', 'reset'].includes(name)
          ? JSON.stringify({ reason: (reason ?? '').trim() })
          : undefined;
      await api<TaskCenterTask>(`/tasks/${task.id}/${name}`, { method: 'POST', body });
      await load();
      if (detail?.task.id === task.id) await loadDetail(task);
      return true;
    } catch (error) {
      setActionError(errorMessage(error));
      return false;
    } finally {
      setBusyId('');
    }
  }

  async function membershipAdmissionAction(path: string, loadingKey: string) {
    setBusyId(`admission:${loadingKey}`);
    setActionError('');
    try {
      const updated = await api<TaskCenterDetail>(path, { method: 'POST' });
      setDetail(updated);
      await load();
      return true;
    } catch (error) {
      setActionError(errorMessage(error));
      return false;
    } finally {
      setBusyId('');
    }
  }

  async function downloadMembershipAdmissionFailures(task: TaskCenterTask) {
    setBusyId(`admission:export:${task.id}`);
    setActionError('');
    try {
      const token = localStorage.getItem('tg_ops_token');
      const response = await fetch(`${API_BASE}/tasks/${task.id}/membership-admission/failures.csv`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) throw new ApiError(response.status, await response.text().catch(() => ''));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `membership-admission-${task.id}-failures.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyId('');
    }
  }

  async function deleteTask(task: TaskCenterTask, reason: string) {
    setBusyId(`${task.id}:delete`);
    setActionError('');
    try {
      await api(`/tasks/${task.id}`, { method: 'DELETE', body: JSON.stringify({ reason: reason.trim() }) });
      if (detail?.task.id === task.id) setDetail(null);
      await load();
      return true;
    } catch (error) {
      setActionError(errorMessage(error));
      return false;
    } finally {
      setBusyId('');
    }
  }

  function openDangerTaskAction(task: TaskCenterTask, action: DangerousTaskAction) {
    const config: Record<DangerousTaskAction, Omit<DangerousTaskState, 'task' | 'action'>> = {
      stop: {
        title: '停止任务',
        content: `确认停止“${task.name}”？未执行计划会标记为跳过。`,
        okText: '停止',
      },
      reset: {
        title: '重置并重新规划任务',
        content: '会清空这个任务旧的执行计划和执行记录，并重新拉取消息、重新生成计划。',
        okText: '重置',
      },
      delete: {
        title: '删除任务',
        content: `确认删除“${task.name}”？任务会停止并从任务中心隐藏，历史执行记录保留。`,
        okText: '删除',
      },
    };
    setActionError('');
    setDangerReason('');
    setDangerAction({ task, action, ...config[action] });
  }

  async function confirmDangerTaskAction() {
    if (!dangerAction) return;
    const reason = dangerReason.trim();
    if (!reason) {
      setActionError('请填写操作原因');
      return;
    }
    const ok = dangerAction.action === 'delete'
      ? await deleteTask(dangerAction.task, reason)
      : await taskAction(dangerAction.task, dangerAction.action, reason);
    if (ok) {
      setDangerAction(null);
      setDangerReason('');
    }
  }

  function relayRoleLabel(role?: string, isBot?: boolean) {
    if (isBot) return '机器人';
    if (role === 'owner') return '群主';
    if (role === 'admin') return '管理员';
    if (role === 'unknown') return '未知身份';
    return '普通成员';
  }

  function relaySourceOptionLabel(source: TaskCenterDetail['recent_relay_sources'][number]) {
    const username = source.sender_username ? ` @${source.sender_username.replace(/^@+/, '')}` : '';
    const peer = source.sender_peer_id ? ` / ${source.sender_peer_id}` : '';
    const group = source.source_group_title ? ` / ${source.source_group_title}` : '';
    return `${source.sender_name || '未知来源'}${username}${peer} / ${relayRoleLabel(source.sender_role, source.is_bot)}${group}`;
  }

  function relaySourceOptions(current: TaskCenterDetail | null) {
    const seen = new Set<string>();
    return (current?.recent_relay_sources ?? [])
      .filter((source) => source.sender_peer_id)
      .filter((source) => {
        if (seen.has(source.sender_peer_id)) return false;
        seen.add(source.sender_peer_id);
        return true;
      })
      .map((source) => ({ value: source.sender_peer_id, label: relaySourceOptionLabel(source) }));
  }

  async function addSourceIdentityToBlocklist(source: { peerId?: string; username?: string; name?: string; sourceActionId?: string; sourceAction?: string; reason?: string }) {
    if (!detail) return;
    if (!source.peerId && !source.username && !source.name) return;
    const payload = {
      sender_peer_id: source.peerId ?? '',
      sender_username: source.username ?? '',
      sender_name: source.name ?? '',
      source_action_id: source.sourceActionId ?? null,
      source_action: source.sourceAction ?? '',
      reason: source.reason ?? '从任务详情加入来源不转发名单',
    };
    setActionError('');
    try {
      const updated = await api<TaskCenterTask>(`/tasks/${detail.task.id}/source-filter-overrides`, { method: 'POST', body: JSON.stringify(payload) });
      setActionWarning('已加入当前任务的来源不转发名单。');
      await load();
      await loadDetail(updated);
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function addRelaySourceToBlocklist(item: TaskCenterDetail['relay_batches'][number]['items'][number]) {
    await addSourceIdentityToBlocklist({
      peerId: item.source_sender_peer_id,
      username: item.source_sender_username,
      name: item.source_sender_name,
      sourceActionId: item.action_id,
      sourceAction: `${relaySourceDisplay(item)} / ${item.source_remote_message_id || item.action_id}`,
      reason: '从转发批次明细加入来源不转发名单',
    });
  }

  async function addRecentRelaySourceToBlocklist(item: TaskCenterDetail['recent_relay_sources'][number]) {
    await addSourceIdentityToBlocklist({
      peerId: item.sender_peer_id,
      username: item.sender_username,
      name: item.sender_name,
      sourceActionId: item.remote_message_id,
      sourceAction: `${item.source_group_title || '源群'} / ${item.sender_name || item.sender_peer_id || item.sender_username || '未知来源'} / ${item.remote_message_id || '-'}`,
      reason: '从最近来源加入来源不转发名单',
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
      if (wizardStep === 3) {
        if (taskType !== 'group_membership_admission') await runTaskPrecheck(form.getFieldsValue(true));
      }
      setWizardStep((value) => Math.min(value + 1, WIZARD_STEPS.length - 1));
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  function resetTypeFields(nextType: TaskCenterTaskType) {
    setTaskType(nextType);
    setPrecheck(null);
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
    search: [(task) => [task.id, task.name, TYPE_LABEL[task.type] ?? task.type, runtimeStageLabel(task), statusLabel(task.status), task.status, task.target_summary, task.search_text, task.last_error]],
  });

  const columns: ColumnsType<TaskCenterTask> = [
    {
      title: '任务',
      key: 'task',
      width: 340,
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{taskListTitle(task)}</Typography.Text>
          {task.type === 'group_ai_chat' && task.name !== taskListTitle(task) && (
            <Typography.Text type="secondary">{task.name}</Typography.Text>
          )}
          <Typography.Text type="secondary">{TYPE_LABEL[task.type] ?? task.type}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 160,
      render: (_value, task) => {
        const stage = runtimeStage(task);
        return (
          <Space direction="vertical" size={0}>
            <TaskStatusBadge task={task} status={task.status} />
            <Typography.Text type="secondary">{stage.reason || '-'}</Typography.Text>
          </Space>
        );
      },
    },
    {
      title: '执行统计',
      key: 'stats',
      width: 220,
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{task.stats?.success_count ?? 0}/{task.stats?.total_actions ?? 0} 成功，{task.stats?.failure_count ?? 0} 失败</Typography.Text>
          <HardHourlyTaskSummary task={task} />
          <MembershipTaskSummary task={task} />
        </Space>
      ),
    },
    { title: '下次运行', dataIndex: 'next_run_at', width: 180, render: (value) => formatDateTime(value) },
    { title: '错误', dataIndex: 'last_error', width: 220, render: (value) => value || '无' },
    {
      title: '操作',
      key: 'actions',
      width: 420,
      fixed: 'right',
      render: (_, task) => (
        <Space className="task-action-bar" size={6}>
          {canManageTasks && canStartTask(task) && <Button size="small" type="primary" icon={<CirclePlay size={14} />} loading={busyId === `${task.id}:${task.status === 'paused' ? 'resume' : 'start'}`} onClick={() => taskAction(task, task.status === 'paused' ? 'resume' : 'start')}>{task.status === 'paused' ? '恢复运行' : '启动'}</Button>}
          {canManageTasks && canPauseTask(task) && <Button size="small" danger icon={<CirclePause size={14} />} loading={busyId === `${task.id}:pause`} onClick={() => taskAction(task, 'pause')}>暂停</Button>}
          {canManageTasks && !isSystemTask(task) && <Button size="small" loading={busyId === `${task.id}:retry`} onClick={() => taskAction(task, 'retry')}>重试</Button>}
          {canDispatchControl && !isSystemTask(task) && <Button size="small" danger loading={busyId === `${task.id}:reset`} onClick={() => openDangerTaskAction(task, 'reset')}>重置</Button>}
          {canManageTasks && !isSystemTask(task) && <Button size="small" danger loading={busyId === `${task.id}:stop`} onClick={() => openDangerTaskAction(task, 'stop')}>停止</Button>}
          {canDeleteTask(task) && <Button size="small" danger loading={busyId === `${task.id}:delete`} onClick={() => openDangerTaskAction(task, 'delete')}>删除</Button>}
          <Button size="small" onClick={() => loadDetail(task)}>详情</Button>
        </Space>
      ),
    },
  ];

  const planColumns: ColumnsType<TaskCenterAction> = [
    { title: '计划执行时间', dataIndex: 'scheduled_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '动作', dataIndex: 'action_type', width: 120, render: (value) => actionLabel(value) },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <ActionStatusBadge status={value} /> },
    { title: '目标', key: 'target', width: 180, render: (_, action) => actionTarget(action) },
    { title: '引用回复', key: 'reply_target', width: 260, render: (_, action) => actionReplyTarget(action) },
    { title: '内容', key: 'content', ellipsis: true, render: (_, action) => actionContent(action) },
  ];

  const recordColumns: ColumnsType<TaskCenterAction> = [
    { title: '动作', dataIndex: 'action_type', width: 120, render: (value) => actionLabel(value) },
    { title: '计划执行时间', dataIndex: 'scheduled_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '实际执行时间', dataIndex: 'executed_at', width: 190, render: (value) => formatDateTime(value) },
    { title: '账号', dataIndex: 'account_id', width: 170, render: (value) => accountDisplay(detail, value) },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <ActionStatusBadge status={value} /> },
    { title: '目标', key: 'target', width: 180, render: (_, action) => actionTarget(action) },
    { title: '引用回复', key: 'reply_target', width: 260, render: (_, action) => actionReplyTarget(action) },
    { title: '内容', key: 'content', ellipsis: true, render: (_, action) => actionContent(action) },
    { title: '账号/目标原因', key: 'failure_diagnosis_summary', width: 260, ellipsis: true, render: (_, action) => failureDiagnosis(action)?.operator_summary || action.failure_reason || action.result?.error_message || action.result?.detail || '-' },
    { title: '处理建议', key: 'failure_diagnosis_action', width: 260, ellipsis: true, render: (_, action) => failureDiagnosis(action)?.suggested_action || '-' },
    { title: '失败类型', key: 'failure_type', width: 140, render: (_, action) => action.failure_type || action.result?.error_code || '-' },
    { title: '可读原因', key: 'failure_reason', width: 220, ellipsis: true, render: (_, action) => action.failure_reason || action.result?.error_message || action.result?.detail || '-' },
    { title: '运营异常', key: 'operation_issue', width: 130, render: (_, action) => action.operation_issue_rolled_up ? <Tag color="red">已上卷 #{action.operation_issue_id.slice(0, 8)}</Tag> : '-' },
    { title: 'Trace / 原始错误', key: 'trace', width: 220, ellipsis: true, render: (_, action) => action.trace_id || action.raw_error || '-' },
    { title: '结果', key: 'result', width: 220, render: (_, action) => actionResult(action) },
    { title: '尝试', key: 'attempts', width: 100, render: (_, action) => <Button size="small" onClick={() => void openActionAttempts(action)}>查看</Button> },
  ];

  const attemptColumns: ColumnsType<TaskExecutionAttempt> = [
    { title: '序号', dataIndex: 'attempt_no', width: 80 },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <TaskStatusBadge status={value} /> },
    { title: 'Worker', dataIndex: 'worker_id', width: 180, ellipsis: true, render: (value) => value || '-' },
    { title: '账号', dataIndex: 'account_id', width: 150, render: (value) => accountDisplay(detail, value) },
    { title: '调用开始', dataIndex: 'gateway_call_started_at', width: 180, render: (value) => formatDateTime(value) },
    { title: '完成时间', dataIndex: 'after_call_at', width: 180, render: (value) => formatDateTime(value) },
    { title: '远端消息', dataIndex: 'remote_message_id', width: 150, ellipsis: true, render: (value) => value || '-' },
    { title: '失败类型', dataIndex: 'failure_type', width: 150, render: (value) => value || '-' },
    { title: '失败详情', dataIndex: 'failure_detail', ellipsis: true, render: (value) => value || '-' },
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
    { title: '直接评论', key: 'direct', width: 90, render: (_, item) => item.stats.direct ?? 0 },
    { title: '回复评论', key: 'reply', width: 90, render: (_, item) => item.stats.reply ?? 0 },
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
    {
      title: '引用回复',
      key: 'reply_target',
      width: 260,
      ellipsis: true,
      render: (_, turn) => turn.reply_to_message_id
        ? <Space size={4}><Tag color="blue">引用回复</Tag><span>{turn.reply_target_author || turn.reply_target_label || `#${turn.reply_to_message_id}`}：{turn.reply_target_preview || '-'}</span></Space>
        : <Tag>普通发言</Tag>,
    },
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
    { title: '目标画像', key: 'profile', width: 220, render: (_, item) => item.profile_scene ? `v${item.profile_version || 0} / ${item.profile_unavailable_reason || item.profile_hit_summary || '-'}` : '-' },
    { title: '质量风险', key: 'quality_risks', width: 180, render: (_, item) => item.quality_risks?.length ? item.quality_risks.join('；') : item.skip_reason || '-' },
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
    { title: '用户名', dataIndex: 'source_sender_username', width: 140, ellipsis: true, render: (value) => value ? `@${String(value).replace(/^@+/, '')}` : '-' },
    { title: '来源身份', key: 'source_role', width: 120, render: (_, item) => <Tag>{relayRoleLabel(item.source_sender_role, item.source_is_bot)}</Tag> },
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
    { title: '来源过滤', key: 'source_filter', width: 150, fixed: 'right', render: (_, item) => canManageTasks ? <Button size="small" onClick={() => addRelaySourceToBlocklist(item)}>加入不转发名单</Button> : '-' },
  ];

  const formValues = Form.useWatch([], form) ?? {};
  const editFormValues = Form.useWatch([], editForm) ?? {};
  const editableTaskType = detail && !isSystemTask(detail.task) ? detail.task.type as TaskCenterTaskType : taskType;
  const editShowsAiLimitRecommendation = isAiLimitTaskType(editableTaskType);
  const plannedActions = detail?.actions.filter(isPlannedAction) ?? [];
  const executedActions = detail?.actions.filter((action) => !isPlannedAction(action)) ?? [];
  const detailProfile = detail && !isSystemTask(detail.task) ? currentOperationProfile({ pacing_config: detail.task.pacing_config }) : null;
  const detailPlannedTotal = (detail?.stats.total_actions ?? 0) + plannedActions.length;
  const attemptDiagnosis = attemptDetail ? failureDiagnosis(attemptDetail.action) : null;
  const taskQuickGroups = buildTaskQuickGroups(table.filteredRows);
  const visibleTaskRows = filterTasksByQuickGroup(table.filteredRows, selectedTaskGroupId);
  const taskQuickGroupIds = taskQuickGroups.map((group) => group.id).join('|');

  React.useEffect(() => {
    if (selectedTaskGroupId === 'all') return;
    const exists = taskQuickGroupIds.split('|').includes(selectedTaskGroupId);
    if (!exists) setSelectedTaskGroupId('all');
  }, [selectedTaskGroupId, taskQuickGroupIds]);

  return (
    <>
      <Space className="stats-grid" wrap>
        <StatCard label="任务总数" value={tasks.length} detail="5 类型" icon={<Activity size={20} />} />
        <StatCard label="执行中" value={tasks.filter((task) => task.status === 'running').length} detail="正在调度" icon={<RefreshCcw size={20} />} />
        <StatCard label="失败任务" value={tasks.filter((task) => task.status === 'failed').length} detail="需处理" icon={<Activity size={20} />} />
      </Space>
      <Card className="panel" title="任务中心" extra={canManageTasks ? <Button type="primary" loading={supportLoading} onClick={() => void openCreateTask()}>创建任务</Button> : null}>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        {actionWarning && <Alert className="form-alert" type="warning" showIcon message={actionWarning} />}
        <Space className="toolbar-row" wrap>
          <Select<TaskTypeFilter> style={{ width: 180 }} value={taskTypeFilter} options={TASK_TYPE_FILTER_OPTIONS} onChange={setTaskTypeFilter} />
          <Typography.Text type="secondary">按目标群聊 + 关联频道</Typography.Text>
          <Select<string>
            aria-label="任务分组"
            style={{ width: TASK_GROUP_SELECT_WIDTH, maxWidth: '100%' }}
            value={selectedTaskGroupId}
            popupMatchSelectWidth={TASK_GROUP_DROPDOWN_WIDTH}
            onChange={(value) => {
              setSelectedTaskGroupId(String(value));
              table.setPage(1);
            }}
            options={[
              { label: `全部任务分组 ${table.filteredRows.length}`, value: 'all' },
              ...taskQuickGroups.map((group) => ({ label: group.label, value: group.id })),
            ]}
          />
          {table.searchInput}
          <Button loading={loading} onClick={() => void load(taskTypeFilter)}>刷新</Button>
        </Space>
        <Table<TaskCenterTask>
          className="tg-table"
          rowKey={(row) => row.id}
          columns={columns}
          dataSource={visibleTaskRows}
          pagination={{ ...table.pagination, total: visibleTaskRows.length }}
          scroll={{ x: 1380 }}
          loading={loading}
        />
      </Card>

      <Modal className="tg-modal large" title="创建任务" open={modalOpen} width={980} footer={null} destroyOnHidden centered onCancel={() => setModalOpen(false)}>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        {actionWarning && <Alert className="form-alert" type="warning" showIcon message={actionWarning} />}
        <Steps className="wizard-steps" current={wizardStep} items={WIZARD_STEPS.map((title) => ({ title }))} />
        <Form form={form} layout="vertical" initialValues={initialValuesForType(taskType, schedulingSetting)}>
          {wizardStep === 0 && <WizardBasics taskType={taskType} onTypeChange={resetTypeFields} />}
          {wizardStep === 1 && <WizardTarget taskType={taskType} groupTargets={groupTargets} channelTargets={channelTargets} messages={messages} messageScope={messageScope} targetChannelId={targetChannelId} onTargetChannelChange={() => form.setFieldsValue({ message_ids: [] })} />}
          {wizardStep === 2 && <WizardTypeConfig taskType={taskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} relaySourceOptions={[]} targetChannelId={targetChannelId} messageScope={messageScope} messageIds={messageIds} />}
          {wizardStep === 3 && (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <WizardAccounts accountMode={accountMode} accounts={accounts} accountPools={accountPools} taskType={taskType} />
              <WizardOperationProfile form={form} values={formValues} taskType={taskType} />
              <Collapse
                ghost
                items={[
                  {
                    key: 'advanced',
                    label: '高级覆盖',
                    children: (
                      <Space direction="vertical" style={{ width: '100%' }}>
                        <div className="form-grid">
                          <TaskRuntimeAdvancedFields />
                        </div>
                      </Space>
                    ),
                  },
                ]}
              />
            </Space>
          )}
          {wizardStep === 4 && <WizardReview taskType={taskType} values={formValues} accounts={accounts} accountPools={accountPools} targets={targets} ruleSets={ruleSets} slangTemplates={slangTemplates} precheck={precheck} loading={precheckLoading} />}
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
          {detail && !isSystemTask(detail.task) && ['group_ai_chat', 'group_relay'].includes(detail.task.type) && (
            <>
              <Typography.Title level={5}>目标来源</Typography.Title>
              <WizardTarget taskType={detail.task.type as TaskCenterTaskType} groupTargets={groupTargets} channelTargets={channelTargets} messages={messages} messageScope={editMessageScope} targetChannelId={editTargetChannelId} onTargetChannelChange={() => editForm.setFieldsValue({ message_ids: [] })} allowInlineTarget={false} />
            </>
          )}
          <Typography.Title level={5}>类型参数</Typography.Title>
          <WizardTypeConfig taskType={(detail && !isSystemTask(detail.task) ? detail.task.type : taskType) as TaskCenterTaskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} relaySourceOptions={relaySourceOptions(detail)} targetChannelId={editTargetChannelId} messageScope={editMessageScope} messageIds={editMessageIds} />
          <Typography.Title level={5}>账号选择</Typography.Title>
          <WizardAccounts accountMode={editAccountMode} accounts={accounts} accountPools={accountPools} taskType={editableTaskType} />
          <Typography.Title level={5}>节奏策略</Typography.Title>
          <WizardOperationProfile form={editForm} values={editFormValues} taskType={editableTaskType} />
          {editShowsAiLimitRecommendation && (
            <Alert
              className="form-alert"
              type="info"
              showIcon
              message="推荐数量"
              description={(
                <Space wrap>
                  <Typography.Text>{recommendedLimitSummary(editRecommendation)}</Typography.Text>
                  <Button loading={editRecommendationLoading} onClick={() => void runEditAiLimitRecommendation()}>计算推荐数量</Button>
                  {editRecommendation && <Button type="primary" onClick={applyEditAiLimitRecommendations}>一键应用推荐</Button>}
                </Space>
              )}
            />
          )}
          <TaskRuntimeAdvancedFields />
        </Form>
      </Modal>

      <TaskCenterDetailModal
        detail={detail}
        canManageTasks={canManageTasks && !isSystemTask(detail?.task)}
        supportLoading={supportLoading}
        plannedActions={plannedActions}
        executedActions={executedActions}
        detailProfile={detailProfile}
        detailPlannedTotal={detailPlannedTotal}
        membershipLoading={membershipPage.loading}
        membershipPagination={membershipPage}
        membershipFilters={membershipFilters}
        aiGenerationColumns={aiGenerationColumns}
        aiAccountProfileColumns={aiAccountProfileColumns}
        aiCycleColumns={aiCycleColumns}
        aiTurnColumns={aiTurnColumns}
        relayBatchColumns={relayBatchColumns}
        relayItemColumns={relayItemColumns}
        onBlockRelaySource={(source) => void addRecentRelaySourceToBlocklist(source)}
        messageColumns={messageColumns}
        planColumns={planColumns}
        recordColumns={recordColumns}
        onEditTask={(task) => void openEditTask(task)}
        onRefreshTask={(task) => void loadDetail(task)}
        onMembershipPageChange={(page, pageSize) => void loadMembershipPage(page, pageSize)}
        onMembershipFiltersChange={updateMembershipFilters}
        onOpenAccountDetail={onOpenAccountDetail}
        onResumeTask={(task) => void taskAction(task, 'resume')}
        admissionBusyId={busyId.startsWith('admission:') ? busyId.slice('admission:'.length) : ''}
        onRetryAdmissionItem={(item) => void membershipAdmissionAction(`/tasks/${detail?.task.id}/membership-admission/items/${item.id}/retry`, `retry:${item.id}`)}
        onRetryFailedAdmissionItems={(task) => void membershipAdmissionAction(`/tasks/${task.id}/membership-admission/retry-failed`, 'retry-failed')}
        onMarkAdmissionManualHandled={(item) => void membershipAdmissionAction(`/tasks/${detail?.task.id}/membership-admission/items/${item.id}/manual-handled`, `manual:${item.id}`)}
        onExportAdmissionFailures={(task) => void downloadMembershipAdmissionFailures(task)}
        onClose={() => {
          setDetail(null);
          setMembershipPage({ current: 1, pageSize: MEMBERSHIP_PAGE_SIZE, total: 0, loading: false });
          setMembershipFilters(DEFAULT_MEMBERSHIP_FILTERS);
        }}
      />
      <Modal
        className="tg-modal large"
        title={attemptDetail ? `执行尝试 ${attemptDetail.action.id}` : '执行尝试'}
        open={Boolean(attemptDetail)}
        width={980}
        footer={null}
        onCancel={() => setAttemptDetail(null)}
        destroyOnHidden
        centered
      >
        {attemptDiagnosis && (
          <Alert
            className="form-alert"
            type="warning"
            showIcon
            message={attemptDiagnosis.operator_summary}
            description={<Space direction="vertical" size={2}><Typography.Text strong>处理建议</Typography.Text><Typography.Text>{attemptDiagnosis.suggested_action}</Typography.Text></Space>}
          />
        )}
        <Table<TaskExecutionAttempt>
          className="tg-table"
          rowKey="id"
          columns={attemptColumns}
          dataSource={attemptDetail?.attempts ?? []}
          loading={attemptDetail?.loading}
          pagination={false}
          scroll={{ x: 1200 }}
          locale={{ emptyText: '暂无执行尝试记录' }}
        />
      </Modal>
      <Modal
        className="tg-modal"
        title={dangerAction?.title ?? '确认操作'}
        open={Boolean(dangerAction)}
        okText={dangerAction?.okText ?? '确认'}
        cancelText="取消"
        okButtonProps={{ danger: true, disabled: !dangerReason.trim() }}
        confirmLoading={Boolean(dangerAction && busyId === `${dangerAction.task.id}:${dangerAction.action}`)}
        onOk={() => void confirmDangerTaskAction()}
        onCancel={() => setDangerAction(null)}
        destroyOnHidden
        centered
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text>{dangerAction?.content}</Typography.Text>
          <Input.TextArea
            rows={3}
            value={dangerReason}
            maxLength={255}
            showCount
            placeholder="填写操作原因"
            onChange={(event) => setDangerReason(event.target.value)}
          />
        </Space>
      </Modal>
    </>
  );
}
