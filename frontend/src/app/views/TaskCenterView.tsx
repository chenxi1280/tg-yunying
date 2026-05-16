import React from 'react';
import { Alert, Button, Card, Collapse, Form, Input, Modal, Space, Steps, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, RefreshCcw } from 'lucide-react';
import { api } from '../../shared/api/client';
import type { Account, AccountPool, ChannelMessage, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, SchedulingSetting, TaskCenterAction, TaskCenterDetail, TaskCenterPrefill, TaskCenterTask, TaskCenterTaskType, TaskPrecheck } from '../types';
import { StatusBadge, StatCard, useAntdTableControls } from '../components/shared';
import { fromBeijingDateTimeLocalValue } from '../time';
import {
  CREATE_AND_START_ENDPOINT,
  CREATE_ENDPOINT,
  TYPE_LABEL,
  WIZARD_STEPS,
  accountDisplay,
  actionContent,
  actionLabel,
  actionResult,
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
  initialValuesForType,
  isPlannedAction,
  normalizePromptTemplateType,
  operationProfileFromValues,
  operationTemplate,
  parseKeyValueMap,
  statusLabel,
  toDateTimeLocal,
  words,
} from './taskCenterViewModel';
import { EditBasics, TaskRuntimeAdvancedFields, WizardAccounts, WizardBasics, WizardOperationProfile, WizardReview, WizardTarget, WizardTypeConfig } from './TaskCenterWizardSections';
import { TaskCenterDetailModal } from './TaskCenterDetailModal';

function TaskStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={statusLabel(status)} />;
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
  const [precheck, setPrecheck] = React.useState<TaskPrecheck | null>(null);
  const [precheckLoading, setPrecheckLoading] = React.useState(false);
  const [wizardStep, setWizardStep] = React.useState(0);
  const [taskType, setTaskType] = React.useState<TaskCenterTaskType>('group_ai_chat');
  const [form] = Form.useForm();
  const [editForm] = Form.useForm();
  const appliedPrefillNonce = React.useRef<number | null>(null);
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
      pacing_mode: 'template',
      max_actions_per_hour: pacing.max_actions_per_hour ?? null,
      max_actions_per_day: pacing.max_actions_per_day ?? null,
      operation_template_id: operationTemplateId,
      hourly_activity_curve: curveText(operationCurve),
      operation_profile_manual_override: Boolean(operationProfile.manual_override),
      quiet_threshold: operationProfile.quiet_threshold ?? 20,
      peak_threshold: operationProfile.peak_threshold ?? 70,
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
      filter_bot_messages: task.type === 'group_relay' ? config.filter_bot_messages !== false : config.filter_bot_messages,
      filter_admin_messages: task.type === 'group_relay' ? Boolean(config.filter_admin_messages) : config.filter_admin_messages,
      excluded_sender_peer_ids: Array.isArray(config.excluded_sender_peer_ids) ? config.excluded_sender_peer_ids : [],
      excluded_sender_input: formatExcludedSenderInput(config),
      allowed_reactions: Array.isArray(config.allowed_reactions) ? config.allowed_reactions.join(',') : config.allowed_reactions,
      reply_to_message_ids: Array.isArray(config.reply_to_message_ids) ? config.reply_to_message_ids : csvNumbers(config.reply_to_message_ids),
      max_message_length: config.max_message_length ?? null,
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
    const config: Record<string, any> = {
      mode: values.pacing_mode ?? 'template',
      operation_profile: operationProfileFromValues(values),
      max_actions_per_hour: values.max_actions_per_hour ?? null,
      max_actions_per_day: values.max_actions_per_day ?? null,
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
        allow_account_repeat: values.allow_account_repeat ?? true,
        repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2,
        account_personas: parseKeyValueMap(values.account_personas),
        account_memory_depth: values.account_memory_depth ?? 3,
        messages_per_round_mode: values.messages_per_round_mode ?? 'auto',
        messages_per_round: values.messages_per_round ?? 1,
        history_fetch_account_id: values.history_fetch_account_id ?? null,
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
        target_operation_target_id: values.target_operation_target_id,
        target_operation_target_ids: targetOperationIds,
        send_account_ids: [],
        content_mode: values.content_mode ?? 'light_rewrite',
        ...relaySourceFilterPayload(values),
        require_review: false,
      };
    }
    if (taskType === 'channel_view') {
      return { ...base, ...channelScopePayload(values), target_views_per_message: values.target_views_per_message ?? 50, execution_mode: values.execution_mode ?? 'distribute' };
    }
    if (taskType === 'channel_like') {
      return { ...base, ...channelScopePayload(values), target_likes_per_message: values.target_likes_per_message ?? 50, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return { ...base, ...channelScopePayload(values), target_comments_per_message: values.target_comments_per_message ?? 10, comment_mode: values.comment_mode ?? 'comment', reply_to_message_ids: csvNumbers(values.reply_to_message_ids), rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, ai_model: values.ai_model ?? '', comment_style: values.comment_style ?? 'mixed', topic_hint: values.topic_hint ?? '', system_prompt_override: values.system_prompt_override ?? '', language: values.language ?? 'zh-CN', max_comment_length: values.max_comment_length ?? null, max_comments_per_account_per_hour: values.max_comments_per_account_per_hour ?? 3, require_review: false };
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
      return { ...base, target_operation_target_id: values.target_operation_target_id ?? null, rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, target_group_name: target?.title ?? '', topic_hint: values.topic_hint ?? '', chat_history_depth: values.chat_history_depth ?? 50, ai_model: values.ai_model ?? '', system_prompt_override: values.system_prompt_override ?? '', slang_prompt_template_id: values.slang_prompt_template_id ?? null, slang_terms: parseKeyValueMap(values.slang_terms), tone: values.tone ?? 'auto', language: values.language ?? 'zh-CN', max_message_length: values.max_message_length ?? null, participation_rate: values.participation_rate ?? 0.6, allow_account_repeat: values.allow_account_repeat ?? true, repeat_cooldown_rounds: values.repeat_cooldown_rounds ?? 2, account_personas: parseKeyValueMap(values.account_personas), account_memory_depth: values.account_memory_depth ?? 3, messages_per_round_mode: values.messages_per_round_mode ?? 'auto', messages_per_round: values.messages_per_round ?? 1, history_fetch_account_id: values.history_fetch_account_id ?? null, idle_continuation_enabled: values.idle_continuation_enabled ?? true, idle_continuation_seconds: values.idle_continuation_seconds ?? 300, context_expire_after_messages: values.context_expire_after_messages ?? 10 };
    }
    if (type === 'group_relay') {
      const sourceTargetIds = csvNumbers(values.source_operation_target_ids);
      const targetOperationIds = csvNumbers(values.target_operation_target_ids);
      return { ...base, source_groups: sourceTargetIds.length ? sourceTargetIds.map((id) => {
        const target = groupTargets.find((item) => item.id === id);
        return { operation_target_id: id, group_name: target?.title ?? '', is_active: true };
      }) : values.source_groups ?? [], target_operation_target_id: values.target_operation_target_id ?? null, target_operation_target_ids: targetOperationIds, rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, content_mode: values.content_mode ?? 'light_rewrite', ...relaySourceFilterPayload(values), require_review: false };
    }
    if (type === 'channel_view') {
      return { ...base, target_views_per_message: values.target_views_per_message ?? 50, execution_mode: values.execution_mode ?? 'distribute' };
    }
    if (type === 'channel_like') {
      return { ...base, target_likes_per_message: values.target_likes_per_message ?? 50, reaction_type: values.reaction_type ?? 'random', allowed_reactions: words(values.allowed_reactions || '👍'), max_likes_per_account_per_hour: values.max_likes_per_account_per_hour ?? 10 };
    }
    return { ...base, target_comments_per_message: values.target_comments_per_message ?? 10, comment_mode: values.comment_mode ?? 'comment', reply_to_message_ids: csvNumbers(values.reply_to_message_ids), rule_set_id: values.rule_set_id ?? null, rule_set_version_id: values.rule_set_version_id ?? null, ai_model: values.ai_model ?? '', comment_style: values.comment_style ?? 'mixed', topic_hint: values.topic_hint ?? '', system_prompt_override: values.system_prompt_override ?? '', language: values.language ?? 'zh-CN', max_comment_length: values.max_comment_length ?? null, max_comments_per_account_per_hour: values.max_comments_per_account_per_hour ?? 3, require_review: false };
  }

  async function runTaskPrecheck(values: any) {
    setPrecheckLoading(true);
    try {
      const result = await api<TaskPrecheck>('/tasks/precheck', { method: 'POST', body: JSON.stringify({ task_type: taskType, payload: createPayload(values) }) });
      setPrecheck(result);
      if (result.decision === 'block') {
        setActionWarning(`预检发现阻塞项：${result.blockers.join('；') || '请检查账号、目标和风控配置'}`);
      } else if (result.decision === 'warn') {
        setActionWarning(`预检有风险提示：${[...result.warnings, ...result.risk_hits].filter(Boolean).slice(0, 3).join('；') || '建议确认后再启动'}`);
      } else {
        setActionWarning('');
      }
      return result;
    } finally {
      setPrecheckLoading(false);
    }
  }

  async function createTask(options: { start?: boolean; skipCapacityCheck?: boolean } = {}) {
    const start = options.start ?? true;
    setActionError('');
    setActionWarning('');
    try {
      await form.validateFields(fieldsForSubmit(taskType, messageScope, accountMode, pacingMode));
      const values = form.getFieldsValue(true);
      const result = !options.skipCapacityCheck ? await runTaskPrecheck(values) : precheck;
      if (start && result?.decision === 'block') {
        setActionError(`预检未通过：${result.blockers.join('；') || '存在阻塞项'}`);
        return;
      }
      await api<TaskCenterTask>((start ? CREATE_AND_START_ENDPOINT : CREATE_ENDPOINT)[taskType], { method: 'POST', body: JSON.stringify(createPayload(values)) });
      form.resetFields();
      setPrecheck(null);
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
      await editForm.validateFields(editFieldsForSubmit(detail.task.type, editAccountMode, 'template'));
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

  async function addSourceIdentityToBlocklist(source: { peerId?: string; username?: string; name?: string }) {
    if (!detail) return;
    const config = detail.task.type_config ?? {};
    const payload: Record<string, any> = {};
    if (source.peerId) {
      payload.excluded_sender_peer_ids = Array.from(new Set([...(config.excluded_sender_peer_ids ?? []), source.peerId]));
    } else if (source.username) {
      payload.excluded_sender_usernames = Array.from(new Set([...(config.excluded_sender_usernames ?? []), source.username.replace(/^@+/, '')]));
    } else if (source.name) {
      payload.excluded_sender_names = Array.from(new Set([...(config.excluded_sender_names ?? []), source.name]));
    }
    if (!Object.keys(payload).length) return;
    setActionError('');
    try {
      const updated = await api<TaskCenterTask>(`/tasks/${detail.task.id}/settings`, { method: 'PATCH', body: JSON.stringify(payload) });
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
    });
  }

  async function addRecentRelaySourceToBlocklist(item: TaskCenterDetail['recent_relay_sources'][number]) {
    await addSourceIdentityToBlocklist({
      peerId: item.sender_peer_id,
      username: item.sender_username,
      name: item.sender_name,
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
        await runTaskPrecheck(form.getFieldsValue(true));
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
    { title: '来源过滤', key: 'source_filter', width: 150, fixed: 'right', render: (_, item) => <Button size="small" onClick={() => addRelaySourceToBlocklist(item)}>加入不转发名单</Button> },
  ];

  const formValues = Form.useWatch([], form) ?? {};
  const editFormValues = Form.useWatch([], editForm) ?? {};
  const plannedActions = detail?.actions.filter(isPlannedAction) ?? [];
  const executedActions = detail?.actions.filter((action) => !isPlannedAction(action)) ?? [];
  const detailProfile = detail ? currentOperationProfile({ pacing_config: detail.task.pacing_config }) : null;
  const detailPlannedTotal = (detail?.stats.total_actions ?? 0) + plannedActions.length;

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
          {wizardStep === 2 && <WizardTypeConfig taskType={taskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} relaySourceOptions={[]} targetChannelId={targetChannelId} messageScope={messageScope} messageIds={messageIds} />}
          {wizardStep === 3 && (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <WizardAccounts accountMode={accountMode} accounts={accounts} accountPools={accountPools} />
              <WizardOperationProfile form={form} values={formValues} />
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
          {detail && ['group_ai_chat', 'group_relay'].includes(detail.task.type) && (
            <>
              <Typography.Title level={5}>目标来源</Typography.Title>
              <WizardTarget taskType={detail.task.type} groupTargets={groupTargets} channelTargets={channelTargets} messages={messages} messageScope={editMessageScope} targetChannelId={editTargetChannelId} onTargetChannelChange={() => editForm.setFieldsValue({ message_ids: [], reply_to_message_ids: [] })} />
            </>
          )}
          <Typography.Title level={5}>类型参数</Typography.Title>
          <WizardTypeConfig taskType={detail?.task.type ?? taskType} ruleSets={ruleSets} slangTemplates={slangTemplates} comments={comments} relaySourceOptions={relaySourceOptions(detail)} targetChannelId={editTargetChannelId} messageScope={editMessageScope} messageIds={editMessageIds} />
          <Typography.Title level={5}>账号选择</Typography.Title>
          <WizardAccounts accountMode={editAccountMode} accounts={accounts} accountPools={accountPools} />
          <Typography.Title level={5}>节奏策略</Typography.Title>
          <WizardOperationProfile form={editForm} values={editFormValues} />
          <TaskRuntimeAdvancedFields />
        </Form>
      </Modal>

      <TaskCenterDetailModal
        detail={detail}
        supportLoading={supportLoading}
        plannedActions={plannedActions}
        executedActions={executedActions}
        detailProfile={detailProfile}
        detailPlannedTotal={detailPlannedTotal}
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
        onClose={() => setDetail(null)}
      />
    </>
  );
}
