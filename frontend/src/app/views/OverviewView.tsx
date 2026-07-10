import React from 'react';
import { Activity, RefreshCcw, Send, ShieldAlert, Smartphone, Users } from 'lucide-react';
import { Alert, Button, Card, Descriptions, Drawer, Empty, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import type { OperationCenterSummary, OperationIssue, OperationIssueDetail, OperationPlan, OperationPlanApplyResult, OperationPlanGenerateResult, OperationPlanPreview, OperationTarget, Overview, TargetRuntimeSummary } from '../types';
import { StatCard, Badge, StatusBadge } from '../components/shared';
import OperationTargetSelect from '../components/OperationTargetSelect';
import { riskTone } from '../utils';
import { formatBeijingDateTime } from '../time';
import { api, apiWithMeta, ApiError } from '../../shared/api/client';
import { GROUP_AI_HARD_HOURLY_MIN_MESSAGES } from './taskCenterViewModel';

const TARGET_WORKBENCH_PAGE_SIZE = 8;

type ActivityPoint = NonNullable<Overview['activity_24h']>[number];
type MetricKey = 'sent_messages' | 'likes' | 'comments' | 'success_rate' | 'failure_rate';
type IssueAction = 'claim' | 'acknowledge' | 'resolve' | 'ignore';
type PlanEditForm = {
  name: string;
  description: string;
  target_type: string;
  status: string;
  target_ids: number[];
  task_types: string[];
};

type TargetWorkbenchRow = {
  key: string;
  targetId: number;
  target?: OperationTarget;
  summary?: TargetRuntimeSummary;
  issues: OperationIssue[];
};

type TargetPageQuery = Readonly<{ page: number; pageSize: number }>;
type OperationDataRequestIdentity = Readonly<{ sequence: number; queryKey: string }>;
type OperationDataRequest = Readonly<{
  sequence: number;
  queryKey: string;
  query: TargetPageQuery;
  controller: AbortController;
}>;

function targetPageQueryKey(query: TargetPageQuery) {
  return JSON.stringify(query);
}

function operationTargetPagePath(query: TargetPageQuery) {
  const params = new URLSearchParams();
  params.set('page', String(query.page));
  params.set('page_size', String(query.pageSize));
  return `/operation-targets?${params.toString()}`;
}

function targetRuntimeSummaryPath(targetIds: readonly number[]) {
  const params = new URLSearchParams();
  if (!targetIds.length) params.append('target_ids', '');
  for (const targetId of targetIds) params.append('target_ids', String(targetId));
  return `/operation-targets/runtime-summary?${params.toString()}`;
}

function operationTargetResponseTotal(headers: Headers) {
  const rawTotal = headers.get('x-total-count');
  if (rawTotal === null) throw new Error('运营目标分页响应缺少 x-total-count');
  const total = Number(rawTotal);
  if (!Number.isSafeInteger(total) || total < 0) throw new Error(`运营目标总数无效：${rawTotal}`);
  return total;
}

interface Props {
  overview: Overview;
  onOpenTargets?: (targetId?: number) => void;
  onOpenTaskDetail?: (taskId?: string) => void;
  onOpenMessageSending?: () => void;
  onOpenAccounts?: () => void;
  onOpenAccountDetail?: (accountId: number) => void;
  onOpenRules?: () => void;
  onOpenRisk?: () => void;
  canManageOperationIssues?: boolean;
}

const VOLUME_SERIES: Array<{ key: MetricKey; label: string; color: string }> = [
  { key: 'sent_messages', label: '发送', color: '#1677ff' },
  { key: 'likes', label: '点赞', color: '#16a34a' },
  { key: 'comments', label: '评论', color: '#d97706' },
];

const RATE_SERIES: Array<{ key: MetricKey; label: string; color: string }> = [
  { key: 'success_rate', label: '成功率', color: '#16a34a' },
  { key: 'failure_rate', label: '失败率', color: '#dc2626' },
];

const PLAN_TASK_OPTIONS: Record<string, Array<{ value: string; label: string }>> = {
  group: [
    { value: 'group_ai_chat', label: 'AI 活跃群' },
    { value: 'group_relay', label: '转发监听群' },
  ],
  channel: [
    { value: 'channel_view', label: '频道浏览' },
    { value: 'channel_like', label: '频道点赞' },
    { value: 'channel_comment', label: '频道评论/回复' },
  ],
};

function isMessageIssue(issue?: OperationIssue | null) {
  return Boolean(issue?.source_task_id?.startsWith('message_task:') || issue?.return_to?.page === 'message-sending');
}

function errorText(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

export default function OverviewView({ overview, onOpenTargets, onOpenTaskDetail, onOpenMessageSending, onOpenAccounts, onOpenAccountDetail, onOpenRules, onOpenRisk, canManageOperationIssues = false }: Props) {
  const [plans, setPlans] = React.useState<OperationPlan[]>([]);
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [targetPageQuery, setTargetPageQuery] = React.useState<TargetPageQuery>({ page: 1, pageSize: TARGET_WORKBENCH_PAGE_SIZE });
  const [targetTotal, setTargetTotal] = React.useState(0);
  const [operationCenter, setOperationCenter] = React.useState<OperationCenterSummary | null>(overview.operation_center ?? null);
  const [targetSummaries, setTargetSummaries] = React.useState<TargetRuntimeSummary[]>([]);
  const [issues, setIssues] = React.useState<OperationIssue[]>([]);
  const [operationLoading, setOperationLoading] = React.useState(false);
  const [operationError, setOperationError] = React.useState('');
  const [issueDetail, setIssueDetail] = React.useState<OperationIssueDetail | null>(null);
  const [issueDrawerOpen, setIssueDrawerOpen] = React.useState(false);
  const [issueLoading, setIssueLoading] = React.useState(false);
  const [issueBusy, setIssueBusy] = React.useState('');
  const [issueAction, setIssueAction] = React.useState<IssueAction | null>(null);
  const [issueActionReason, setIssueActionReason] = React.useState('');
  const activeIssueDetailId = React.useRef<string | null>(null);
  const operationDataRequestRef = React.useRef<OperationDataRequestIdentity>({ sequence: 0, queryKey: '' });
  const operationDataAbortController = React.useRef<AbortController | null>(null);
  const targetPageQueryRef = React.useRef(targetPageQuery);
  const activePlanActionKey = React.useRef('');
  const activePlanEditSaveRequestRef = React.useRef<{ seq: number; planId: number | null; signature: string }>({ seq: 0, planId: null, signature: '' });
  const activeImpactApplyRequestRef = React.useRef<{ seq: number; planId: number | null; signature: string }>({ seq: 0, planId: null, signature: '' });
  const activeIssueActionRequestRef = React.useRef({ seq: 0, issueId: '', signature: '' });
  const [planBusy, setPlanBusy] = React.useState('');
  const [planPreview, setPlanPreview] = React.useState<OperationPlanPreview | null>(null);
  const [planPreviewTitle, setPlanPreviewTitle] = React.useState('');
  const [planPreviewOpen, setPlanPreviewOpen] = React.useState(false);
  const [editingPlan, setEditingPlan] = React.useState<OperationPlan | null>(null);
  const [planEditOpen, setPlanEditOpen] = React.useState(false);
  const [planEditForm, setPlanEditForm] = React.useState<PlanEditForm>(emptyPlanEditForm());
  const [impactPlan, setImpactPlan] = React.useState<OperationPlan | null>(null);
  const [impactOpen, setImpactOpen] = React.useState(false);
  const [impactResult, setImpactResult] = React.useState<OperationPlanApplyResult | null>(null);
  const [impactReason, setImpactReason] = React.useState('');
  targetPageQueryRef.current = targetPageQuery;
  const activity = React.useMemo(() => normalizedActivity(overview.activity_24h), [overview.activity_24h]);
  const operationSummary = operationCenter ?? overview.operation_center ?? null;
  const totals = activity.reduce(
    (acc, item) => ({
      sent: acc.sent + item.sent_messages,
      likes: acc.likes + item.likes,
      comments: acc.comments + item.comments,
      success: acc.success + item.success,
      failed: acc.failed + item.failed,
      total: acc.total + item.total,
    }),
    { sent: 0, likes: 0, comments: 0, success: 0, failed: 0, total: 0 },
  );
  const successRate = totals.total ? Math.round((totals.success * 1000) / totals.total) / 10 : 0;
  const failureRate = totals.total ? Math.round((totals.failed * 1000) / totals.total) / 10 : 0;
  const issueTaskStage = issueDetail?.task_runtime_stage || issueDetail?.related_task_summary?.summary?.runtime_stage;

  function isActiveIssueDetail(issueId: string) {
    return activeIssueDetailId.current === issueId;
  }

  function beginOperationDataRequest(query: TargetPageQuery): OperationDataRequest {
    operationDataAbortController.current?.abort();
    const controller = new AbortController();
    const identity = {
      sequence: operationDataRequestRef.current.sequence + 1,
      queryKey: targetPageQueryKey(query),
    };
    operationDataRequestRef.current = identity;
    operationDataAbortController.current = controller;
    return { ...identity, query, controller };
  }

  function isActiveOperationDataRequest(request: OperationDataRequest) {
    return !request.controller.signal.aborted
      && operationDataRequestRef.current.sequence === request.sequence
      && operationDataRequestRef.current.queryKey === request.queryKey;
  }

  function beginPlanAction(actionKey: string) {
    activePlanActionKey.current = actionKey;
    return actionKey;
  }

  function isActivePlanAction(actionKey: string) {
    return activePlanActionKey.current === actionKey;
  }

  function planEditSavePayloadSignature(planId: number, payload: Record<string, any>) {
    return JSON.stringify({ plan_id: planId, payload });
  }

  function impactApplyPayloadSignature(planId: number, payload: Record<string, any>) {
    return JSON.stringify({ plan_id: planId, payload });
  }

  function issueActionPayloadSignature(issueId: string, action: IssueAction, reason: string) {
    return JSON.stringify({ issue_id: issueId, action, reason });
  }

  function invalidatePlanEditSaveRequest() {
    const requestSeq = activePlanEditSaveRequestRef.current.seq + 1;
    activePlanEditSaveRequestRef.current = { seq: requestSeq, planId: null, signature: '' };
  }

  function invalidateImpactApplyRequest() {
    const requestSeq = activeImpactApplyRequestRef.current.seq + 1;
    activeImpactApplyRequestRef.current = { seq: requestSeq, planId: null, signature: '' };
  }

  function invalidateIssueActionRequest() {
    const requestSeq = activeIssueActionRequestRef.current.seq + 1;
    activeIssueActionRequestRef.current = { seq: requestSeq, issueId: '', signature: '' };
  }

  function beginPlanEditSaveRequest(planId: number, signature: string) {
    const requestSeq = activePlanEditSaveRequestRef.current.seq + 1;
    activePlanEditSaveRequestRef.current = { seq: requestSeq, planId, signature };
    return requestSeq;
  }

  function beginImpactApplyRequest(planId: number, signature: string) {
    const requestSeq = activeImpactApplyRequestRef.current.seq + 1;
    activeImpactApplyRequestRef.current = { seq: requestSeq, planId, signature };
    return requestSeq;
  }

  function beginIssueActionRequest(issueId: string, signature: string) {
    const requestSeq = activeIssueActionRequestRef.current.seq + 1;
    activeIssueActionRequestRef.current = { seq: requestSeq, issueId, signature };
    return requestSeq;
  }

  function currentPlanEditSavePayloadSignature() {
    if (!editingPlan) return '';
    return planEditSavePayloadSignature(editingPlan.id, planEditPayloadFromForm(planEditForm));
  }

  function currentImpactApplyPayloadSignature() {
    if (!impactPlan) return '';
    return impactApplyPayloadSignature(impactPlan.id, { reason: impactReason.trim(), confirm_apply: true });
  }

  function currentIssueActionPayloadSignature() {
    const issue = issueDetail?.issue;
    if (!issue || !issueAction) return '';
    return issueActionPayloadSignature(issue.id, issueAction, issueActionReason.trim());
  }

  function isCurrentPlanEditSaveRequest(requestSeq: number) {
    return activePlanEditSaveRequestRef.current.seq === requestSeq;
  }

  function isCurrentImpactApplyRequest(requestSeq: number) {
    return activeImpactApplyRequestRef.current.seq === requestSeq;
  }

  function isCurrentIssueActionRequest(requestSeq: number) {
    return activeIssueActionRequestRef.current.seq === requestSeq;
  }

  function isActivePlanEditSaveRequest(planId: number, requestSeq: number, signature: string) {
    return isCurrentPlanEditSaveRequest(requestSeq)
      && activePlanEditSaveRequestRef.current.planId === planId
      && activePlanEditSaveRequestRef.current.signature === signature
      && currentPlanEditSavePayloadSignature() === signature;
  }

  function isActiveImpactApplyRequest(planId: number, requestSeq: number, signature: string) {
    return isCurrentImpactApplyRequest(requestSeq)
      && activeImpactApplyRequestRef.current.planId === planId
      && activeImpactApplyRequestRef.current.signature === signature
      && currentImpactApplyPayloadSignature() === signature;
  }

  function isActiveIssueActionRequest(issueId: string, requestSeq: number, signature: string) {
    return isCurrentIssueActionRequest(requestSeq)
      && isActiveIssueDetail(issueId)
      && activeIssueActionRequestRef.current.issueId === issueId
      && activeIssueActionRequestRef.current.signature === signature
      && currentIssueActionPayloadSignature() === signature;
  }

  React.useEffect(() => {
    void loadOperationData();
    return () => operationDataAbortController.current?.abort();
  }, [targetPageQuery]);

  async function fetchOperationData(request: OperationDataRequest) {
    const requestOptions = { signal: request.controller.signal };
    const targetRequest = apiWithMeta<OperationTarget[]>(operationTargetPagePath(request.query), requestOptions);
    const targetResponse = await targetRequest;
    const runtimePath = targetRuntimeSummaryPath(targetResponse.data.map((target) => target.id));
    const [planRows, centerSummary, runtimeRows, issueRows] = await Promise.all([
      api<OperationPlan[]>('/operation-plans', requestOptions),
      api<OperationCenterSummary>('/operation-center/overview', requestOptions),
      api<TargetRuntimeSummary[]>(runtimePath, { signal: request.controller.signal }),
      api<OperationIssue[]>('/operation-issues', requestOptions),
    ]);
    if (!isActiveOperationDataRequest(request)) return false;
    const responseTotal = operationTargetResponseTotal(targetResponse.headers);
    setPlans(planRows);
    setTargets(targetResponse.data);
    setTargetTotal(responseTotal);
    setOperationCenter(centerSummary);
    setTargetSummaries(runtimeRows);
    setIssues(issueRows);
    return true;
  }

  async function loadOperationData() {
    const request = beginOperationDataRequest(targetPageQueryRef.current);
    setOperationLoading(true);
    setOperationError('');
    try {
      await fetchOperationData(request);
    } catch (err) {
      if (!isActiveOperationDataRequest(request)) return;
      setOperationError(err instanceof Error ? err.message : String(err));
    } finally {
      if (isActiveOperationDataRequest(request)) setOperationLoading(false);
    }
  }

  async function refreshOperationDataAfterAction(actionLabel: string) {
    const request = beginOperationDataRequest(targetPageQueryRef.current);
    try {
      await fetchOperationData(request);
    } catch (error) {
      if (!isActiveOperationDataRequest(request)) return;
      setOperationError(`运营中心数据刷新失败：${actionLabel}操作已完成，但刷新运营中心数据失败：${errorText(error)}`);
    }
  }

  async function createDefaultPlan(target?: OperationTarget) {
    const selectedTarget = target ?? (targetTotal === 1 ? targets[0] : undefined);
    if (!selectedTarget && targetTotal > 1) {
      void message.warning('请在目标工作台的目标行点击创建方案，避免选错目标');
      return;
    }
    if (!selectedTarget) {
      void message.warning('请先在目标管理里添加运营目标');
      return;
    }
    const actionKey = beginPlanAction('create');
    setPlanBusy(actionKey);
    try {
      await api<OperationPlan>('/operation-plans', {
        method: 'POST',
        body: JSON.stringify({
          name: `${selectedTarget.title} 日常运营方案`,
          description: '运营中心快速创建的目标方案',
          target_type: selectedTarget.target_type,
          target_ids: [selectedTarget.id],
          task_blueprints: defaultBlueprints(selectedTarget),
        }),
      });
      if (!isActivePlanAction(actionKey)) return;
      void message.success(`${selectedTarget.title} 的运营方案已创建`);
      await refreshOperationDataAfterAction('运营方案创建');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      void message.error(`创建运营方案失败：${errorText(error)}`);
    } finally {
      if (isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  async function previewPlan(plan: OperationPlan) {
    const actionKey = beginPlanAction(`${plan.id}:preview`);
    setPlanPreviewTitle(plan.name);
    setPlanPreview(null);
    setPlanPreviewOpen(true);
    setPlanBusy(actionKey);
    try {
      const result = await api<OperationPlanPreview>(`/operation-plans/${plan.id}/generate-preview`, { method: 'POST', body: JSON.stringify({}) });
      if (!isActivePlanAction(actionKey)) return;
      setPlanPreview(result);
      void message.success(`预览生成 ${result.estimated_task_count || result.planned_tasks.length} 个任务${result.blockers.length ? `，阻塞 ${result.blockers.length} 项` : ''}`);
      await refreshOperationDataAfterAction('运营方案预览');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      void message.error(`生成运营方案预览失败：${errorText(error)}`);
    } finally {
      if (isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  async function generatePlanTasks(plan: OperationPlan, autoStart: boolean) {
    const busyKey = `${plan.id}:${autoStart ? 'generate' : 'draft'}`;
    const actionKey = beginPlanAction(busyKey);
    setPlanBusy(actionKey);
    try {
      const result = await api<OperationPlanGenerateResult>(`/operation-plans/${plan.id}/generate-tasks`, {
        method: 'POST',
        body: JSON.stringify({ auto_start: autoStart, reason: autoStart ? '运营中心生成并启动方案任务' : '运营中心生成方案草稿' }),
      });
      if (!isActivePlanAction(actionKey)) return;
      void message.success(autoStart ? `已生成并启动 ${result.created_task_ids.length} 个关联任务` : `已生成 ${result.created_task_ids.length} 个任务草稿`);
      await refreshOperationDataAfterAction(autoStart ? '方案任务生成并启动' : '方案任务草稿生成');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      void message.error(`生成方案任务失败：${errorText(error)}`);
    } finally {
      if (isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  async function changePlanLifecycle(plan: OperationPlan, action: 'pause' | 'resume' | 'copy' | 'archive') {
    const labels = {
      pause: '暂停',
      resume: '恢复',
      copy: '复制',
      archive: '归档',
    };
    if (action === 'archive') {
      const ok = window.confirm(`确认归档运营方案「${plan.name}」？归档后不会继续作为日常方案入口。`);
      if (!ok) return;
    }
    const actionKey = beginPlanAction(`${plan.id}:${action}`);
    setPlanBusy(actionKey);
    try {
      await api<OperationPlan>(`/operation-plans/${plan.id}/${action}`, { method: 'POST', body: JSON.stringify({}) });
      if (!isActivePlanAction(actionKey)) return;
      void message.success(`运营方案已${labels[action]}`);
      await refreshOperationDataAfterAction(`运营方案${labels[action]}`);
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      void message.error(`运营方案${labels[action]}失败：${errorText(error)}`);
    } finally {
      if (isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  function openPlanEditor(plan: OperationPlan) {
    const taskTypes = plan.task_blueprints.map((item) => String(item.task_type || item.type || '')).filter(Boolean);
    invalidatePlanEditSaveRequest();
    setEditingPlan(plan);
    setPlanEditForm({
      name: plan.name,
      description: plan.description || '',
      target_type: plan.target_type || 'group',
      status: plan.status || 'active',
      target_ids: plan.targets.map((item) => item.target_id),
      task_types: taskTypes.length ? taskTypes : defaultTaskTypesForPlan(plan.target_type || 'group'),
    });
    setPlanEditOpen(true);
  }

  function closePlanEditor() {
    invalidatePlanEditSaveRequest();
    setPlanEditOpen(false);
  }

  async function savePlanEditor() {
    if (!editingPlan) return;
    if (!planEditForm.name.trim()) {
      void message.warning('方案名称不能为空');
      return;
    }
    if (!planEditForm.target_ids.length) {
      void message.warning('至少选择一个运营目标');
      return;
    }
    if (!planEditForm.task_types.length) {
      void message.warning('至少选择一个任务模板');
      return;
    }
    const planId = editingPlan.id;
    const actionKey = beginPlanAction(`${editingPlan.id}:edit`);
    let requestSeq = 0;
    let payloadSignature = '';
    setPlanBusy(actionKey);
    try {
      const payload = planEditPayloadFromForm(planEditForm);
      payloadSignature = planEditSavePayloadSignature(planId, payload);
      requestSeq = beginPlanEditSaveRequest(planId, payloadSignature);
      await api<OperationPlan>(`/operation-plans/${planId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActivePlanAction(actionKey)) return;
      if (!isActivePlanEditSaveRequest(planId, requestSeq, payloadSignature)) return;
      setPlanEditOpen(false);
      void message.success('运营方案已保存');
      await refreshOperationDataAfterAction('运营方案保存');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      if (requestSeq && !isActivePlanEditSaveRequest(planId, requestSeq, payloadSignature)) return;
      void message.error(`保存运营方案失败：${errorText(error)}`);
    } finally {
      if (requestSeq ? isCurrentPlanEditSaveRequest(requestSeq) : isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  async function openImpactPreview(plan: OperationPlan) {
    const actionKey = beginPlanAction(`${plan.id}:impact`);
    invalidateImpactApplyRequest();
    setImpactPlan(plan);
    setImpactResult(null);
    setImpactReason('');
    setImpactOpen(true);
    setPlanBusy(actionKey);
    try {
      const result = await api<OperationPlanApplyResult>(`/operation-plans/${plan.id}/apply-to-linked-tasks`, {
        method: 'POST',
        body: JSON.stringify({ reason: '预览关联任务影响', confirm_apply: false }),
      });
      if (!isActivePlanAction(actionKey)) return;
      setImpactResult(result);
      await refreshOperationDataAfterAction('关联任务影响预览');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      void message.error(`生成关联任务影响预览失败：${errorText(error)}`);
    } finally {
      if (isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  function closeImpactPreview() {
    invalidateImpactApplyRequest();
    setImpactOpen(false);
  }

  async function confirmImpactApply() {
    if (!impactPlan) return;
    if (!impactReason.trim()) {
      void message.warning('应用到关联任务前必须填写原因');
      return;
    }
    const planId = impactPlan.id;
    const actionKey = beginPlanAction(`${impactPlan.id}:apply`);
    let requestSeq = 0;
    let payloadSignature = '';
    setPlanBusy(actionKey);
    try {
      const reason = impactReason.trim();
      const payload = { reason, confirm_apply: true };
      payloadSignature = impactApplyPayloadSignature(planId, payload);
      requestSeq = beginImpactApplyRequest(planId, payloadSignature);
      const result = await api<OperationPlanApplyResult>(`/operation-plans/${planId}/apply-to-linked-tasks`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActivePlanAction(actionKey)) return;
      if (!isActiveImpactApplyRequest(planId, requestSeq, payloadSignature)) return;
      setImpactResult(result);
      void message.success(`已应用 ${result.applied_task_ids.length} 个关联任务`);
      await refreshOperationDataAfterAction('关联任务应用');
    } catch (error) {
      if (!isActivePlanAction(actionKey)) return;
      if (requestSeq && !isActiveImpactApplyRequest(planId, requestSeq, payloadSignature)) return;
      void message.error(`应用关联任务失败：${errorText(error)}`);
    } finally {
      if (requestSeq ? isCurrentImpactApplyRequest(requestSeq) : isActivePlanAction(actionKey)) setPlanBusy('');
    }
  }

  async function openIssueDetail(issueId: string) {
    activeIssueDetailId.current = issueId;
    setIssueDrawerOpen(true);
    setIssueLoading(true);
    setIssueDetail(null);
    setIssueBusy('');
    setIssueAction(null);
    setIssueActionReason('');
    try {
      const detail = await api<OperationIssueDetail>(`/operation-issues/${issueId}`);
      if (!isActiveIssueDetail(issueId)) return;
      setIssueDetail(detail);
    } catch (error) {
      if (!isActiveIssueDetail(issueId)) return;
      void message.error(`读取目标异常失败：${errorText(error)}`);
      setIssueDrawerOpen(false);
    } finally {
      if (isActiveIssueDetail(issueId)) setIssueLoading(false);
    }
  }

  function closeIssueDrawer() {
    activeIssueDetailId.current = null;
    setIssueDrawerOpen(false);
    setIssueDetail(null);
    setIssueLoading(false);
    setIssueBusy('');
    setIssueAction(null);
    setIssueActionReason('');
  }

  function openIssueAction(action: IssueAction) {
    if (!canManageOperationIssues) {
      void message.warning('当前账号没有异常处理权限');
      return;
    }
    invalidateIssueActionRequest();
    setIssueAction(action);
    setIssueActionReason('');
  }

  function closeIssueActionModal() {
    invalidateIssueActionRequest();
    setIssueAction(null);
    setIssueActionReason('');
  }

  function handleTargetWorkbenchTableChange(pagination: TablePaginationConfig) {
    const pageSize = pagination.pageSize ?? targetPageQuery.pageSize;
    const page = pageSize === targetPageQuery.pageSize ? pagination.current ?? 1 : 1;
    setTargetPageQuery({ page, pageSize });
  }

  async function submitIssueAction() {
    const issue = issueDetail?.issue;
    const action = issueAction;
    if (!issue || !action) return;
    const issueId = issue.id;
    const actionLabel = issueActionLabel(action);
    const reason = issueActionReason.trim();
    if (!reason) {
      void message.warning('需要填写处理原因');
      return;
    }
    let requestSeq = 0;
    let payloadSignature = '';
    setIssueBusy(action);
    try {
      payloadSignature = issueActionPayloadSignature(issueId, action, reason);
      requestSeq = beginIssueActionRequest(issueId, payloadSignature);
      const updated = await api<OperationIssue>(`/operation-issues/${issue.id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      });
      if (!isActiveIssueDetail(issueId)) return;
      if (!isActiveIssueActionRequest(issueId, requestSeq, payloadSignature)) return;
      setIssueDetail((current) => current ? { ...current, issue: updated } : current);
      setIssueAction(null);
      setIssueActionReason('');
      void message.success(`${actionLabel}已提交`);
      await refreshOperationDataAfterAction(`异常${actionLabel}`);
    } catch (error) {
      if (!isActiveIssueDetail(issueId)) return;
      if (requestSeq && !isActiveIssueActionRequest(issueId, requestSeq, payloadSignature)) return;
      void message.error(`${actionLabel}失败：${errorText(error)}`);
    } finally {
      if (requestSeq ? isCurrentIssueActionRequest(requestSeq) : isActiveIssueDetail(issueId)) setIssueBusy('');
    }
  }

  const targetRows = React.useMemo<TargetWorkbenchRow[]>(() => {
    const issueMap = new Map<number, OperationIssue[]>();
    issues.forEach((issue) => {
      if (!issue.target_id) return;
      issueMap.set(issue.target_id, [...(issueMap.get(issue.target_id) ?? []), issue]);
    });
    const rowMap = new Map<number, TargetWorkbenchRow>();
    targets.forEach((target) => {
      rowMap.set(target.id, {
        key: String(target.id),
        targetId: target.id,
        target,
        summary: undefined,
        issues: issueMap.get(target.id) ?? [],
      });
    });
    targetSummaries.forEach((summary) => {
      const current = rowMap.get(summary.target_id);
      rowMap.set(summary.target_id, {
        key: String(summary.target_id),
        targetId: summary.target_id,
        target: current?.target,
        summary,
        issues: issueMap.get(summary.target_id) ?? current?.issues ?? [],
      });
    });
    return Array.from(rowMap.values()).sort((left, right) => {
      const leftOpen = left.summary?.open_issue_count ?? left.issues.length;
      const rightOpen = right.summary?.open_issue_count ?? right.issues.length;
      if (rightOpen !== leftOpen) return rightOpen - leftOpen;
      return (right.summary?.failed_action_count ?? 0) - (left.summary?.failed_action_count ?? 0);
    });
  }, [issues, targetSummaries, targets]);

  const targetColumns: ColumnsType<TargetWorkbenchRow> = [
    {
      title: '目标',
      key: 'target',
      width: 280,
      render: (_, row) => (
        <Space orientation="vertical" size={0}>
          <Typography.Text strong>{row.target?.title ?? `目标 #${row.targetId}`}</Typography.Text>
          <Typography.Text type="secondary">{row.target?.target_type === 'channel' ? '频道' : '群'} / {row.target?.username ? `@${row.target.username}` : row.target?.tg_peer_id ?? '-'}</Typography.Text>
        </Space>
      ),
    },
    { title: '运行状态', key: 'status', width: 130, render: (_, row) => <StatusBadge status={row.summary?.status ?? '未汇总'} label={targetRuntimeStatusLabel(row.summary?.status)} /> },
    { title: 'Open Issue', key: 'open_issue_count', width: 120, render: (_, row) => row.summary?.open_issue_count ?? row.issues.length },
    { title: '失败执行项', key: 'failed_action_count', width: 120, render: (_, row) => row.summary?.failed_action_count ?? 0 },
    { title: '关联任务', key: 'affected_task_count', width: 120, render: (_, row) => row.summary?.affected_task_count ?? 0 },
    { title: '最近失败', key: 'latest_failure_at', width: 190, render: (_, row) => formatBeijingDateTime(row.summary?.latest_failure_at) },
    {
      title: '处理入口',
      key: 'actions',
      width: 300,
      fixed: 'right',
      render: (_, row) => {
        const issue = row.issues[0];
        return (
          <Space size={6} wrap>
            <Button size="small" icon={<Users size={14} />} onClick={() => onOpenTargets?.(row.targetId)}>目标详情</Button>
            <Button size="small" icon={<Activity size={14} />} onClick={() => isMessageIssue(issue) ? onOpenMessageSending?.() : onOpenTaskDetail?.(issue?.source_task_id)}>{isMessageIssue(issue) ? '消息发送' : '任务详情'}</Button>
            <Button size="small" icon={<ShieldAlert size={14} />} disabled={!issue} onClick={() => issue && void openIssueDetail(issue.id)}>查看异常</Button>
            <Button size="small" loading={planBusy === 'create'} disabled={!row.target} onClick={() => row.target && void createDefaultPlan(row.target)}>创建方案</Button>
          </Space>
        );
      },
    },
  ];

  const issueColumns: ColumnsType<OperationIssue> = [
    { title: '等级', dataIndex: 'severity', width: 90, render: (value) => <Badge tone={severityTone(value)}>{severityLabel(value)}</Badge> },
    { title: '目标', dataIndex: 'target_id', width: 110, render: (value) => value ? `#${value}` : '未绑定' },
    { title: '失败类型', dataIndex: 'failure_type', width: 160, render: (value) => value || '-' },
    { title: '处理方式', dataIndex: 'handling_mode', width: 120, render: (value) => handlingModeLabel(value) },
    { title: '关联任务', dataIndex: 'source_task_id', width: 180, render: (value, issue) => issue.affected_task_count || value || '-' },
    { title: '影响账号', key: 'accounts', width: 100, render: (_, issue) => issue.affected_account_count || issue.affected_account_ids.length },
    { title: '最后出现', dataIndex: 'last_seen_at', width: 190, render: (value) => formatBeijingDateTime(value) },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      fixed: 'right',
      render: (_, issue) => (
        <Space size={6}>
          <Button size="small" icon={<ShieldAlert size={14} />} onClick={() => void openIssueDetail(issue.id)}>展开</Button>
          <Button size="small" icon={<Activity size={14} />} onClick={() => isMessageIssue(issue) ? onOpenMessageSending?.() : onOpenTaskDetail?.(issue.source_task_id)}>{isMessageIssue(issue) ? '消息' : '任务'}</Button>
        </Space>
      ),
    },
  ];

  const failedActionColumns: ColumnsType<Record<string, any>> = [
    { title: '动作', dataIndex: 'action_type', width: 120, render: (value) => value || '-' },
    { title: '账号', dataIndex: 'account_id', width: 110, render: (value) => value ?? '-' },
    { title: '失败类型', dataIndex: 'failure_type', width: 150, render: (value) => value || '-' },
    { title: '失败原因', dataIndex: 'failure_reason', ellipsis: true, render: (value) => value || '-' },
    { title: '执行时间', key: 'executed_at', width: 180, render: (_, action) => formatBeijingDateTime(action.executed_at ?? action.scheduled_at) },
    { title: '重试', dataIndex: 'retry_count', width: 80, render: (value) => value ?? 0 },
  ];
  const previewTaskColumns: ColumnsType<Record<string, any>> = [
    { title: '目标', dataIndex: 'target_title', width: 220, ellipsis: true, render: (value) => value || '-' },
    { title: '任务类型', dataIndex: 'task_type', width: 150, render: (value) => value || '-' },
    { title: '任务名', dataIndex: 'name', ellipsis: true, render: (value) => value || '-' },
    { title: '优先级', dataIndex: 'priority', width: 90, render: (value) => value ?? '-' },
    { title: '账号口径', dataIndex: 'account_config', width: 180, render: (value) => value?.selection_mode || '-' },
  ];
  const previewTargetColumns: ColumnsType<Record<string, any>> = [
    { title: '目标', dataIndex: 'target_title', width: 220, ellipsis: true, render: (value) => value || '-' },
    { title: '类型', dataIndex: 'target_type', width: 100, render: (value) => value === 'channel' ? '频道' : '群' },
    { title: '任务数', dataIndex: 'task_count', width: 90, render: (value) => value ?? 0 },
    { title: '可发账号', dataIndex: 'send_account_count', width: 100, render: (value) => value ?? 0 },
    { title: '监听账号', dataIndex: 'listener_account_count', width: 100, render: (value) => value ?? 0 },
    { title: '阻塞', dataIndex: 'blockers', ellipsis: true, render: (value) => renderPreviewList(value) },
    { title: '提醒', dataIndex: 'warnings', ellipsis: true, render: (value) => renderPreviewList(value) },
  ];
  const admissionColumns: ColumnsType<Record<string, any>> = [
    { title: '目标', dataIndex: 'target_title', width: 220, ellipsis: true, render: (value) => value || '-' },
    { title: '任务类型', dataIndex: 'task_type', width: 150, render: (value) => value || '-' },
    { title: '准入动作', dataIndex: 'action_type', width: 190, render: (value) => admissionActionLabel(value) },
    { title: '预计账号', dataIndex: 'required_accounts', width: 100, render: (value) => value ?? 0 },
    { title: '阻塞原因', dataIndex: 'blocking_reasons', ellipsis: true, render: (value) => renderPreviewList(value) },
  ];
  const impactColumns: ColumnsType<Record<string, any>> = [
    { title: '任务', key: 'task', width: 250, render: (_, item) => <Space orientation="vertical" size={0}><Typography.Text strong>{item.task_name || item.task_id}</Typography.Text><Typography.Text type="secondary">{item.task_type} / {item.task_status}</Typography.Text></Space> },
    { title: '目标', dataIndex: 'target_title', width: 190, ellipsis: true, render: (value, item) => value || (item.target_id ? `#${item.target_id}` : '-') },
    { title: '变更字段', dataIndex: 'changed_fields', width: 220, render: (value) => renderChangedFields(value) },
    { title: '是否应用', key: 'will_update', width: 110, render: (_, item) => item.will_update ? <Tag color="blue">待确认</Tag> : <Tag>不更新</Tag> },
    { title: '说明', key: 'reason', ellipsis: true, render: (_, item) => item.block_reason || (item.requires_confirmation ? '运行中任务，确认后更新后续规划口径' : '仅配置差异') },
  ];

  return (
    <section className="view-grid">
      <div className="stats-grid">
        <StatCard label="TG 账号" value={overview.totals.accounts} detail="在线与待登录账号" icon={<Smartphone size={22} />} />
        <StatCard label="运营目标" value={overview.totals.targets ?? overview.totals.groups} detail={`群/频道资产 ${overview.totals.groups}`} icon={<Users size={22} />} />
        <StatCard label="运行中任务" value={overview.queue.running_tasks ?? 0} detail={`总任务 ${overview.totals.tasks ?? 0}`} icon={<Activity size={22} />} />
        <StatCard label="24小时发送" value={totals.sent} detail={`点赞 ${totals.likes} / 评论 ${totals.comments}`} icon={<Send size={22} />} />
        <StatCard label="成功率" value={`${successRate}%`} detail={`失败率 ${failureRate}%`} icon={<Activity size={22} />} />
        <StatCard label="待执行项" value={overview.queue.pending_actions ?? overview.queue.queued ?? 0} detail="pending/executing 动作" icon={<Activity size={22} />} />
        <StatCard label="失败任务" value={overview.queue.failed_tasks ?? 0} detail={`失败执行项 ${overview.queue.failed_actions ?? overview.queue.failed ?? 0}`} icon={<ShieldAlert size={22} />} />
        <StatCard label="风险提醒" value={overview.risks.length} detail="账号、目标、监听与执行异常" icon={<ShieldAlert size={22} />} />
      </div>

      <Card
        className="panel operation-workbench"
        title="目标工作台"
        extra={<Button size="small" icon={<RefreshCcw size={14} />} loading={operationLoading} onClick={() => void loadOperationData()}>刷新</Button>}
      >
        {operationError && <Alert className="form-alert" type="error" showIcon message="运营中心数据刷新失败" description={operationError} />}
        <div className="operation-kpi-grid">
          <OperationKpi label="目标异常" value={operationSummary?.open_issue_count ?? 0} detail="open issue" />
          <OperationKpi label="影响目标" value={operationSummary?.affected_target_count ?? 0} detail="需要运营处理" />
          <OperationKpi label="失败执行项" value={operationSummary?.failed_action_count ?? 0} detail="来自汇总读模型" />
          <OperationKpi label="影响账号" value={operationSummary?.affected_account_count ?? 0} detail="关联失败账号" />
        </div>
        {operationSummary?.stale && (
          <Alert
            className="form-alert"
            type="warning"
            showIcon
            message="汇总数据可能延迟"
            description={`最近更新时间：${formatBeijingDateTime(operationSummary.latest_updated_at)}`}
          />
        )}
        {!operationSummary?.stale && (
          <Typography.Text className="operation-updated" type="secondary">最近更新时间：{formatBeijingDateTime(operationSummary?.latest_updated_at)}</Typography.Text>
        )}
        <Table<TargetWorkbenchRow>
          className="tg-table operation-target-table"
          rowKey="key"
          columns={targetColumns}
          dataSource={targetRows}
          loading={operationLoading}
          pagination={{
            current: targetPageQuery.page,
            pageSize: targetPageQuery.pageSize,
            total: targetTotal,
            showSizeChanger: true,
          }}
          onChange={handleTargetWorkbenchTableChange}
          scroll={{ x: 1240 }}
          locale={{ emptyText: '暂无目标运行摘要。' }}
        />
        <div className="operation-issue-section">
          <div className="section-title-row">
            <Typography.Title level={4}>目标异常</Typography.Title>
            <Typography.Text type="secondary">按目标聚合失败，展开后看关联任务和代表执行项</Typography.Text>
          </div>
          <Table<OperationIssue>
            className="tg-table"
            rowKey="id"
            columns={issueColumns}
            dataSource={issues}
            loading={operationLoading}
            pagination={{ pageSize: 6 }}
            scroll={{ x: 1020 }}
            locale={{ emptyText: '暂无待处理目标异常。' }}
          />
        </div>
      </Card>

      <Card className="panel" title="运营方案 / 策略模板" extra={<Button loading={planBusy === 'create'} onClick={() => void createDefaultPlan()}>新建默认方案</Button>}>
        {plans.length ? (
          <div className="risk-list">
            {plans.map((plan) => {
              const archived = plan.status === 'archived';
              return (
                <article key={plan.id}>
                <div className="list-item-main">
                  <Space wrap><Badge tone={plan.status === 'active' ? 'positive' : plan.status === 'archived' ? 'neutral' : 'warning'}>{plan.status}</Badge><strong>{plan.name}</strong></Space>
                  <p>{`目标 ${plan.targets.length} 个 / 关联任务 ${plan.task_links.length} 个 / 最近效果 ${plan.latest_run?.status ?? '暂无'} / 最近异常 ${planIssueCount(plan, issues)} 个 / 最近运行 ${plan.latest_run?.run_type ?? '暂无'}`}</p>
                </div>
                <Space size={6} wrap>
                  <Button size="small" loading={planBusy === `${plan.id}:edit`} onClick={() => openPlanEditor(plan)}>编辑方案</Button>
                  <Button size="small" loading={planBusy === `${plan.id}:preview`} disabled={archived} onClick={() => void previewPlan(plan)}>生成预览</Button>
                  <Button size="small" loading={planBusy === `${plan.id}:draft`} disabled={archived} onClick={() => void generatePlanTasks(plan, false)}>生成草稿</Button>
                  <Button type="primary" size="small" loading={planBusy === `${plan.id}:generate`} disabled={archived} onClick={() => void generatePlanTasks(plan, true)}>生成并启动</Button>
                  <Button size="small" loading={planBusy === `${plan.id}:impact`} disabled={archived || !plan.task_links.length} onClick={() => void openImpactPreview(plan)}>调整关联任务</Button>
                  {plan.status === 'paused'
                    ? <Button size="small" loading={planBusy === `${plan.id}:resume`} onClick={() => void changePlanLifecycle(plan, 'resume')}>恢复</Button>
                    : <Button size="small" loading={planBusy === `${plan.id}:pause`} disabled={archived} onClick={() => void changePlanLifecycle(plan, 'pause')}>暂停</Button>}
                  <Button size="small" loading={planBusy === `${plan.id}:copy`} onClick={() => void changePlanLifecycle(plan, 'copy')}>复制</Button>
                  <Button size="small" danger loading={planBusy === `${plan.id}:archive`} disabled={archived} onClick={() => void changePlanLifecycle(plan, 'archive')}>归档</Button>
                </Space>
                </article>
              );
            })}
          </div>
        ) : <Empty description="暂无运营方案。先选择目标创建方案，再预览并生成任务。" />}
      </Card>

      <Card className="panel" title="风险提醒" extra={<Typography.Text type="secondary">保留账号、目标、监听与执行风险</Typography.Text>}>
        {overview.risks?.length ? (
          <div className="risk-list">
            {overview.risks.map((risk, index) => (
              <article key={`${risk.level}:${risk.title}:${index}`}>
                <Badge tone={riskTone(risk.level)}>{risk.level}</Badge>
                <div>
                  <strong>{risk.title}</strong>
                  <p>{risk.detail}</p>
                </div>
              </article>
            ))}
          </div>
        ) : <Empty description="暂无风险提醒。" />}
      </Card>

      <div className="overview-chart-grid">
        <Card className="panel overview-chart-card" title="24小时运营趋势" extra={<Legend items={VOLUME_SERIES} />}>
          <LineChart data={activity} series={VOLUME_SERIES} maxValue={maxOf(activity, ['sent_messages', 'likes', 'comments'])} suffix="次" />
        </Card>
        <Card className="panel overview-chart-card" title="每小时互动拆分" extra={<Legend items={VOLUME_SERIES} />}>
          <HourlyBars data={activity} />
        </Card>
        <Card className="panel overview-chart-card" title="成功率与失败率" extra={<Legend items={RATE_SERIES} />}>
          <LineChart data={activity} series={RATE_SERIES} maxValue={100} suffix="%" />
        </Card>
      </div>

      <Drawer
        title={editingPlan ? `编辑方案：${editingPlan.name}` : '编辑方案'}
        open={planEditOpen}
        size="large"
        onClose={closePlanEditor}
        destroyOnHidden
        extra={<Button type="primary" loading={editingPlan ? planBusy === `${editingPlan.id}:edit` : false} onClick={() => void savePlanEditor()}>保存方案</Button>}
      >
        <Space orientation="vertical" size={16} style={{ width: '100%' }}>
          <label className="form-field">
            <span>方案名称</span>
            <Input value={planEditForm.name} onChange={(event) => setPlanEditForm((form) => ({ ...form, name: event.target.value }))} maxLength={160} />
          </label>
          <label className="form-field">
            <span>方案描述</span>
            <Input.TextArea value={planEditForm.description} onChange={(event) => setPlanEditForm((form) => ({ ...form, description: event.target.value }))} rows={3} />
          </label>
          <Space size={12} wrap>
            <label className="form-field">
              <span>目标类型</span>
              <Select
                value={planEditForm.target_type}
                style={{ minWidth: 180 }}
                options={[{ value: 'group', label: '群' }, { value: 'channel', label: '频道' }]}
                onChange={(value) => setPlanEditForm((form) => ({ ...form, target_type: value, target_ids: [], task_types: defaultTaskTypesForPlan(value) }))}
              />
            </label>
            <label className="form-field">
              <span>状态</span>
              <Select
                value={planEditForm.status}
                style={{ minWidth: 180 }}
                options={[{ value: 'active', label: '启用' }, { value: 'paused', label: '暂停' }, { value: 'draft', label: '草稿' }]}
                onChange={(value) => setPlanEditForm((form) => ({ ...form, status: value }))}
              />
            </label>
          </Space>
          <label className="form-field">
            <span>绑定目标</span>
            <OperationTargetSelect
              mode="multiple"
              value={planEditForm.target_ids}
              query={{ targetType: planEditForm.target_type as OperationTarget['target_type'] }}
              onChange={(value) => setPlanEditForm((form) => ({ ...form, target_ids: Array.isArray(value) ? value : [value] }))}
              placeholder="选择这个方案覆盖的目标"
            />
          </label>
          <label className="form-field">
            <span>任务模板</span>
            <Select
              mode="multiple"
              value={planEditForm.task_types}
              options={PLAN_TASK_OPTIONS[planEditForm.target_type] ?? []}
              onChange={(value) => setPlanEditForm((form) => ({ ...form, task_types: value }))}
              placeholder="选择方案可以生成或调整的任务类型"
            />
          </label>
          <Alert type="info" showIcon message="保存方案不会直接改任务" description="调整关联任务前必须先生成影响预览，填写原因并确认后才会更新运行中任务配置。" />
        </Space>
      </Drawer>

      <Drawer
        title={impactPlan ? `关联任务影响预览：${impactPlan.name}` : '关联任务影响预览'}
        open={impactOpen}
        size="large"
        onClose={closeImpactPreview}
        destroyOnHidden
        extra={<Button type="primary" danger loading={impactPlan ? planBusy === `${impactPlan.id}:apply` : false} disabled={!impactResult?.impact_preview?.changed_task_count} onClick={() => void confirmImpactApply()}>确认应用</Button>}
      >
        {!impactResult && <div className="sub-panel compact-panel">生成影响预览中...</div>}
        {impactResult && (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              column={4}
              size="small"
              items={[
                { key: 'linked', label: '关联任务', children: impactResult.linked_task_count },
                { key: 'changed', label: '将更新', children: impactResult.impact_preview?.changed_task_count ?? 0 },
                { key: 'running', label: '运行中影响', children: impactResult.impact_preview?.running_task_count ?? 0 },
                { key: 'applied', label: '已应用', children: impactResult.applied_task_ids.length },
              ]}
            />
            {(impactResult.impact_preview?.blockers?.length || impactResult.impact_preview?.warnings?.length) && (
              <Alert
                type={impactResult.impact_preview?.blockers?.length ? 'error' : 'warning'}
                showIcon
                message="影响预览提示"
                description={[...(impactResult.impact_preview?.blockers ?? []), ...(impactResult.impact_preview?.warnings ?? [])].join('；')}
              />
            )}
            <label className="form-field">
              <span>确认原因</span>
              <Input.TextArea value={impactReason} onChange={(event) => setImpactReason(event.target.value)} rows={3} placeholder="例如：同步最新账号节奏和规则版本，确认影响运行中任务的后续规划。" />
            </label>
            <Table<Record<string, any>>
              className="tg-table"
              rowKey="task_id"
              columns={impactColumns}
              dataSource={impactResult.impact_preview?.items ?? []}
              pagination={{ pageSize: 8 }}
              scroll={{ x: 980 }}
              size="small"
              locale={{ emptyText: '暂无关联任务影响。' }}
            />
          </Space>
        )}
      </Drawer>

      <Drawer
        title={`方案生成预览${planPreviewTitle ? `：${planPreviewTitle}` : ''}`}
        open={planPreviewOpen}
        size="large"
        onClose={() => setPlanPreviewOpen(false)}
        destroyOnHidden
      >
        {!planPreview && <div className="sub-panel compact-panel">生成中...</div>}
        {planPreview && (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              column={4}
              size="small"
              items={[
                { key: 'targets', label: '预计目标', children: planPreview.estimated_target_count || planPreview.target_count },
                { key: 'tasks', label: '预计任务', children: planPreview.estimated_task_count || planPreview.planned_tasks.length },
                { key: 'blockers', label: '阻塞原因', children: planPreview.blockers.length },
                { key: 'warnings', label: '提醒', children: planPreview.warnings.length },
                { key: 'send', label: '可发送账号', children: planPreview.account_capacity?.send_available ?? 0 },
                { key: 'listen', label: '可监听账号', children: planPreview.account_capacity?.listen_available ?? 0 },
                { key: 'join', label: '可准入账号', children: planPreview.account_capacity?.join_available ?? 0 },
                { key: 'capacity', label: '剩余容量', children: planPreview.account_capacity?.remaining_capacity ?? 0 },
              ]}
            />
            {planPreview.account_capacity?.stale_or_missing && (
              <Alert type="warning" showIcon message="账号容量汇总为空或可能未刷新" description="生成预览已返回容量口径，创建并启动任务时后端仍会重新做实时预检。" />
            )}
            {(planPreview.blockers.length > 0 || planPreview.warnings.length > 0) && (
              <Alert type={planPreview.blockers.length ? 'error' : 'warning'} showIcon message="预览检查结果" description={[...planPreview.blockers, ...planPreview.warnings].join('；')} />
            )}
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>目标预览</Typography.Title>
                <Typography.Text type="secondary">{planPreview.target_previews.length} 个目标</Typography.Text>
              </div>
              <Table<Record<string, any>> rowKey="target_id" columns={previewTargetColumns} dataSource={planPreview.target_previews} pagination={false} scroll={{ x: 920 }} size="small" />
            </div>
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>准入动作</Typography.Title>
                <Typography.Text type="secondary">{planPreview.admission_actions.length} 项</Typography.Text>
              </div>
              <Table<Record<string, any>> rowKey={(item) => `${item.target_id}:${item.task_type}:${item.action_type}`} columns={admissionColumns} dataSource={planPreview.admission_actions} pagination={false} scroll={{ x: 860 }} size="small" locale={{ emptyText: '暂无额外准入动作。' }} />
            </div>
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>预计任务</Typography.Title>
                <Typography.Text type="secondary">{planPreview.planned_tasks.length} 个</Typography.Text>
              </div>
              <Table<Record<string, any>> rowKey={(item) => `${item.target_id}:${item.task_type}:${item.name}`} columns={previewTaskColumns} dataSource={planPreview.planned_tasks} pagination={{ pageSize: 8 }} scroll={{ x: 840 }} size="small" />
            </div>
          </Space>
        )}
      </Drawer>

      <Drawer
        title={issueDetail?.issue ? `目标异常 ${issueDetail.issue.id}` : '目标异常'}
        open={issueDrawerOpen}
        size="large"
        onClose={closeIssueDrawer}
        destroyOnHidden
        extra={canManageOperationIssues && issueDetail?.issue && (
          <Space>
            <Button size="small" loading={issueBusy === 'claim'} onClick={() => openIssueAction('claim')}>认领处理</Button>
            <Button size="small" loading={issueBusy === 'acknowledge'} onClick={() => openIssueAction('acknowledge')}>确认异常</Button>
            <Button size="small" loading={issueBusy === 'resolve'} onClick={() => openIssueAction('resolve')}>标记解决</Button>
            <Button size="small" danger loading={issueBusy === 'ignore'} onClick={() => openIssueAction('ignore')}>忽略</Button>
          </Space>
        )}
      >
        {issueLoading && <Card className="sub-panel compact-panel">加载中...</Card>}
        {!issueLoading && issueDetail && (
          <div className="operation-issue-drawer">
            <Descriptions
              bordered
              column={2}
              size="small"
              items={[
                { key: 'status', label: '状态', children: <StatusBadge status={issueDetail.issue.status} /> },
                { key: 'severity', label: '等级', children: <Badge tone={severityTone(issueDetail.issue.severity)}>{severityLabel(issueDetail.issue.severity)}</Badge> },
                { key: 'target', label: '目标', children: issueDetail.target?.title ?? (issueDetail.issue.target_id ? `#${issueDetail.issue.target_id}` : '-') },
                { key: 'task', label: '关联任务', children: issueDetail.source_task?.name ?? issueDetail.issue.source_task_id ?? '-' },
                {
                  key: 'task-stage',
                  label: '任务阶段',
                  children: issueTaskStage?.stage_label
                    ? <Space direction="vertical" size={0}><StatusBadge status={issueTaskStage.stage_label} label={issueTaskStage.stage_label} /><Typography.Text type="secondary">{issueTaskStage.reason || '-'}</Typography.Text></Space>
                    : '-',
                },
                { key: 'counts', label: '影响范围', children: `任务 ${issueDetail.issue.affected_task_count || 0} / 账号 ${issueDetail.issue.affected_account_count || issueDetail.affected_accounts.length}` },
                { key: 'mode', label: '处理方式', children: handlingModeLabel(issueDetail.issue.handling_mode) },
                { key: 'failure_type', label: '失败类型', children: issueDetail.issue.failure_type || '-' },
                { key: 'last_seen', label: '最后出现', children: formatBeijingDateTime(issueDetail.issue.last_seen_at) },
                { key: 'claimed_by', label: '处理负责人', children: issueDetail.issue.claimed_by ? `${issueDetail.issue.claimed_by} / ${formatBeijingDateTime(issueDetail.issue.claimed_at)}` : '-' },
                { key: 'return_to', label: '返回上下文', span: 2, children: returnToLabel(issueDetail.issue.return_to) },
                { key: 'failure_reason', label: '失败原因', span: 2, children: issueDetail.issue.failure_reason || '-' },
                { key: 'suggested_action', label: '建议动作', span: 2, children: issueDetail.issue.suggested_action || '-' },
              ]}
            />
            <Space className="operation-drawer-actions" wrap>
              <Button icon={<Activity size={14} />} onClick={() => isMessageIssue(issueDetail.issue) ? onOpenMessageSending?.() : onOpenTaskDetail?.(issueDetail.issue.source_task_id)}>{isMessageIssue(issueDetail.issue) ? '消息发送' : '任务详情'}</Button>
              <Button icon={<Users size={14} />} onClick={() => onOpenTargets?.(issueDetail.issue.target_id ?? undefined)}>目标详情</Button>
              <Button icon={<Smartphone size={14} />} onClick={onOpenAccounts}>账号管理</Button>
              <Button icon={<ShieldAlert size={14} />} onClick={onOpenRules}>规则中心</Button>
              <Button icon={<ShieldAlert size={14} />} onClick={onOpenRisk}>风控中心</Button>
            </Space>
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>来源明细</Typography.Title>
                <Typography.Text type="secondary">{issueDetail.sources.length} 条来源 / {issueDetail.issue_accounts.length} 个账号影响</Typography.Text>
              </div>
              <Table<Record<string, any>>
                className="tg-table"
                rowKey="id"
                columns={[
                  { title: '来源类型', dataIndex: 'source_type', width: 110 },
                  { title: '来源 ID', dataIndex: 'source_id', ellipsis: true },
                  { title: '失败类型', dataIndex: 'failure_type', width: 160, render: (value) => value || '-' },
                  { title: '最近出现', dataIndex: 'latest_seen_at', width: 180, render: (value) => formatBeijingDateTime(value) },
                ]}
                dataSource={issueDetail.sources}
                pagination={{ pageSize: 6 }}
                scroll={{ x: 760 }}
                locale={{ emptyText: '暂无来源明细。' }}
                size="small"
              />
            </div>
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>影响账号</Typography.Title>
                <Typography.Text type="secondary">{issueDetail.affected_accounts.length} 个</Typography.Text>
              </div>
              {issueDetail.affected_accounts.length ? (
                <div className="risk-list">
                  {issueDetail.affected_accounts.map((account) => (
                    <article key={account.id}>
                      <div>
                        <strong>{account.display_name ?? `账号 #${account.id}`}</strong>
                        <p>{`@${account.username ?? '未设置'} / ${account.status ?? '-'} / 健康分 ${account.health_score ?? '-'}`}</p>
                      </div>
                      <Button size="small" icon={<Smartphone size={14} />} onClick={() => onOpenAccountDetail?.(Number(account.id))}>账号详情</Button>
                    </article>
                  ))}
                </div>
              ) : <Empty description="暂无账号影响明细。" />}
            </div>
            <div className="operation-issue-section">
              <div className="section-title-row">
                <Typography.Title level={4}>关联任务失败</Typography.Title>
                <Typography.Text type="secondary">保留最近失败 action 和重试信息</Typography.Text>
              </div>
              <Table<Record<string, any>>
                className="tg-table"
                rowKey="id"
                columns={failedActionColumns}
                dataSource={issueDetail.recent_failed_actions}
                pagination={{ pageSize: 8 }}
                scroll={{ x: 880 }}
                locale={{ emptyText: '暂无失败 action。' }}
              />
            </div>
          </div>
        )}
      </Drawer>
      <Modal
        title={issueAction ? `${issueActionLabel(issueAction)}原因` : '处理原因'}
        open={Boolean(issueAction)}
        okText="提交"
        cancelText="取消"
        confirmLoading={Boolean(issueBusy)}
        onOk={() => void submitIssueAction()}
        onCancel={closeIssueActionModal}
        destroyOnHidden
        centered
      >
        <Input.TextArea
          value={issueActionReason}
          onChange={(event) => setIssueActionReason(event.target.value)}
          rows={4}
          maxLength={255}
          showCount
          placeholder="填写本次处理原因，便于审计和后续追踪。"
        />
      </Modal>
    </section>
  );
}

function OperationKpi({ label, value, detail }: { label: string; value: number | string; detail: string }) {
  return (
    <div className="operation-kpi-item">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function targetRuntimeStatusLabel(status?: string) {
  if (!status) return '未汇总';
  if (status === 'healthy') return '健康';
  if (status === 'warning') return '需关注';
  if (status === 'failed') return '有失败';
  return status;
}

function severityTone(value?: string) {
  if (['critical', 'high', '高'].includes(value ?? '')) return 'danger';
  if (['medium', '中'].includes(value ?? '')) return 'warning';
  if (['low', '低'].includes(value ?? '')) return 'positive';
  return 'neutral';
}

function severityLabel(value?: string) {
  if (value === 'critical') return '严重';
  if (value === 'high') return '高';
  if (value === 'medium') return '中';
  if (value === 'low') return '低';
  return value || '-';
}

function issueActionLabel(action: IssueAction) {
  if (action === 'claim') return '认领处理';
  if (action === 'acknowledge') return '确认异常';
  if (action === 'resolve') return '标记解决';
  return '忽略异常';
}

function handlingModeLabel(value?: string) {
  if (value === 'modal') return '弹窗处理';
  if (value === 'drawer') return '抽屉处理';
  if (value === 'deep_link') return '深链跳转';
  return value || '-';
}

function returnToLabel(value?: Record<string, any>) {
  if (!value || Object.keys(value).length === 0) return '-';
  const filters = value.filters && typeof value.filters === 'object' ? value.filters : {};
  const parts = [
    value.page ? `页面 ${value.page}` : '',
    value.target_id ? `目标 #${value.target_id}` : '',
    value.task_id ? `任务 ${value.task_id}` : '',
    value.source_issue_id ? `来源 ${value.source_issue_id}` : '',
    filters.status ? `状态 ${filters.status}` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' / ') : '-';
}

function renderPreviewList(value?: unknown) {
  if (!Array.isArray(value) || value.length === 0) return '-';
  return value.map((item) => String(item)).join('、');
}

function renderChangedFields(value?: unknown) {
  if (!Array.isArray(value) || value.length === 0) return <Typography.Text type="secondary">无变化</Typography.Text>;
  return (
    <Space size={4} wrap>
      {value.map((item) => <Tag key={String(item)}>{changedFieldLabel(String(item))}</Tag>)}
    </Space>
  );
}

function changedFieldLabel(value: string) {
  const labels: Record<string, string> = {
    name: '任务名称',
    priority: '优先级',
    account_config: '账号范围',
    pacing_config: '节奏',
    failure_policy: '失败策略',
    type_config: '任务配置',
  };
  return labels[value] ?? value;
}

function admissionActionLabel(value?: unknown) {
  if (value === 'ensure_channel_membership') return '频道准入确认';
  if (value === 'ensure_group_send_or_listen') return '群发送/监听确认';
  return String(value || '-');
}

function taskTypeLabel(value: string) {
  const options = [...PLAN_TASK_OPTIONS.group, ...PLAN_TASK_OPTIONS.channel];
  return options.find((item) => item.value === value)?.label ?? value;
}

function emptyPlanEditForm(): PlanEditForm {
  return { name: '', description: '', target_type: 'group', status: 'active', target_ids: [], task_types: defaultTaskTypesForPlan('group') };
}

function planEditPayloadFromForm(form: PlanEditForm): Record<string, any> {
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    target_type: form.target_type,
    status: form.status,
    target_ids: form.target_ids,
    task_blueprints: form.task_types.map((taskType) => ({ task_type: taskType, name: taskTypeLabel(taskType) })),
  };
}

function defaultTaskTypesForPlan(targetType: string) {
  return targetType === 'channel' ? ['channel_view', 'channel_like'] : ['group_ai_chat'];
}

function planIssueCount(plan: OperationPlan, issues: OperationIssue[]) {
  const targetIds = new Set(plan.targets.map((target) => target.target_id));
  return issues.filter((issue) => issue.target_id && targetIds.has(issue.target_id)).length;
}

function defaultBlueprints(target: OperationTarget): Array<Record<string, any>> {
  if (target.target_type === 'channel') {
    return [
      { task_type: 'channel_view', name: '频道浏览', type_config: { target_channel_id: target.id, target_channel_name: target.title, target_type: 'channel' } },
      { task_type: 'channel_like', name: '频道点赞', type_config: { target_channel_id: target.id, target_channel_name: target.title, target_type: 'channel' } },
    ];
  }
  return [
    {
      task_type: 'group_ai_chat',
      name: '群活跃暖场',
      type_config: {
        target_operation_target_id: target.id,
        target_group_name: target.title,
        target_type: 'group',
        hard_hourly_target_enabled: true,
        hourly_min_messages: GROUP_AI_HARD_HOURLY_MIN_MESSAGES,
        hard_hourly_strategy: 'force_planning',
      },
    },
  ];
}

function normalizedActivity(items?: ActivityPoint[]): ActivityPoint[] {
  if (items?.length) return items;
  const now = new Date();
  return Array.from({ length: 24 }, (_, index) => {
    const hour = new Date(now);
    hour.setHours(now.getHours() - 23 + index, 0, 0, 0);
    return {
      hour: `${String(hour.getHours()).padStart(2, '0')}:00`,
      sent_messages: 0,
      likes: 0,
      comments: 0,
      success: 0,
      failed: 0,
      total: 0,
      success_rate: 0,
      failure_rate: 0,
    };
  });
}

function maxOf(data: ActivityPoint[], keys: MetricKey[]): number {
  const maxValue = Math.max(...data.flatMap((item) => keys.map((key) => Number(item[key] || 0))));
  return Math.max(1, maxValue);
}

function Legend({ items }: { items: Array<{ key: string; label: string; color: string }> }) {
  return (
    <span className="chart-legend">
      {items.map((item) => (
        <span key={item.key}>
          <i style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
    </span>
  );
}

function LineChart({ data, series, maxValue, suffix }: { data: ActivityPoint[]; series: Array<{ key: MetricKey; label: string; color: string }>; maxValue: number; suffix: string }) {
  if (!data.length) return <Empty description="暂无数据" />;
  const width = 720;
  const height = 230;
  const padding = { top: 18, right: 18, bottom: 32, left: 42 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const xFor = (index: number) => padding.left + (plotWidth * index) / Math.max(1, data.length - 1);
  const yFor = (value: number) => padding.top + plotHeight - (plotHeight * Math.min(maxValue, Math.max(0, value))) / maxValue;
  const yTicks = [0, Math.round(maxValue / 2), maxValue];

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="24小时折线图">
        {yTicks.map((tick, index) => (
          <g key={`${tick}:${index}`}>
            <line className="chart-grid-line" x1={padding.left} x2={width - padding.right} y1={yFor(tick)} y2={yFor(tick)} />
            <text className="chart-axis-label" x={padding.left - 10} y={yFor(tick) + 4} textAnchor="end">{tick}{suffix}</text>
          </g>
        ))}
        {data.map((item, index) => index % 4 === 0 || index === data.length - 1 ? (
          <text key={item.hour} className="chart-axis-label" x={xFor(index)} y={height - 8} textAnchor="middle">{item.hour}</text>
        ) : null)}
        {series.map((item) => (
          <polyline
            key={item.key}
            fill="none"
            stroke={item.color}
            strokeWidth={3}
            strokeLinecap="round"
            strokeLinejoin="round"
            points={data.map((point, index) => `${xFor(index)},${yFor(Number(point[item.key] || 0))}`).join(' ')}
          />
        ))}
        {series.flatMap((item) => data.map((point, index) => (
          <circle key={`${item.key}-${point.hour}`} cx={xFor(index)} cy={yFor(Number(point[item.key] || 0))} r={index % 4 === 0 || index === data.length - 1 ? 3.5 : 2.2} fill={item.color}>
            <title>{`${point.hour} ${item.label}: ${point[item.key]}${suffix}`}</title>
          </circle>
        )))}
      </svg>
    </div>
  );
}

function HourlyBars({ data }: { data: ActivityPoint[] }) {
  if (!data.length) return <Empty description="暂无数据" />;
  const maxValue = Math.max(1, ...data.map((item) => item.sent_messages + item.likes + item.comments));
  return (
    <div className="hourly-bars" aria-label="每小时互动柱状图">
      {data.map((item, index) => {
        const sentHeight = Math.max(2, Math.round((item.sent_messages / maxValue) * 100));
        const likeHeight = Math.max(2, Math.round((item.likes / maxValue) * 100));
        const commentHeight = Math.max(2, Math.round((item.comments / maxValue) * 100));
        return (
          <div className="hourly-bar" key={item.hour}>
            <div className="hourly-bar-stack" title={`${item.hour} 发送 ${item.sent_messages}，点赞 ${item.likes}，评论 ${item.comments}`}>
              <span className="bar sent" style={{ height: `${sentHeight}%` }} />
              <span className="bar like" style={{ height: `${likeHeight}%` }} />
              <span className="bar comment" style={{ height: `${commentHeight}%` }} />
            </div>
            {(index % 4 === 0 || index === data.length - 1) && <small>{item.hour}</small>}
          </div>
        );
      })}
    </div>
  );
}
