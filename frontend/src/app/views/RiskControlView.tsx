import React from 'react';
import { Activity, CheckCircle2, Database, RefreshCcw, ShieldAlert } from 'lucide-react';
import { App as AntdApp, Button, Card, Descriptions, Empty, Form, Input, InputNumber, Modal, Progress, Select, Space, Switch, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useLocation } from 'react-router-dom';
import { api } from '../../shared/api/client';
import type { AccountProxy, RiskControlAccountScore, RiskControlMetric, RiskControlSummary, RiskDispositionItem, RiskHitRecord, RiskProxyAlert } from '../types';
import { formatBeijingDateTime } from '../time';
import { Badge, StatCard, StatusBadge, useAntdTableControls } from '../components/shared';

type RiskGlobalPolicy = RiskControlSummary['global_policy'];
type RiskPolicyAudit = RiskControlSummary['policy_audits'][number];
type ProxyAlertAction = 'acknowledge' | 'ignore' | 'resolve';
type AccountDetailContext = {
  issue?: string;
  riskTab?: string;
  riskQuery?: string;
  riskPage?: number;
  riskPageSize?: number;
  riskQuickFilter?: string;
};
const riskAccountPhone = (account: RiskControlAccountScore) => account.phone_number || account.phone_masked;
const PROXY_ALERT_IGNORE_DEFAULT_MS = 60 * 60 * 1000;
const DATETIME_LOCAL_LENGTH = 16;
const RISK_TAB_KEYS = new Set(['overview', 'policy', 'accounts', 'queue', 'hits', 'policy-audit', 'proxy', 'proxy-alerts']);

type ProxyFormValues = {
  id?: number;
  name: string;
  protocol: string;
  host: string;
  port: number;
  username: string;
  password?: string;
  check_interval_seconds: number;
  timeout_ms: number;
  max_bound_accounts: number;
  max_concurrent_sessions: number;
  notes: string;
};

const STATUS_LABELS: Record<string, string> = {
  healthy: '健康',
  unhealthy: '异常',
  disabled: '已禁用',
  normal: '正常',
  observing: '观察中',
  alerting: '告警中',
  acknowledged: '处理中',
  ignored: '已忽略',
  recovered: '已恢复',
  pending: '待处理',
  running: '运行中',
  success: '成功',
  failed: '失败',
  skipped: '已跳过',
  critical: '严重',
  warning: '警告',
  info: '提示',
  skip_account: '跳过账号',
  pause_task: '暂停任务',
  stop_task: '停止任务',
  wait_and_retry: '等待后重试',
  skip: '跳过',
  pause: '暂停',
  skip_message: '跳过消息',
  rewrite_and_retry: '改写后重试',
  none: '不退避',
  linear: '线性退避',
  exponential: '指数退避',
  capacity_limit: '容量限制',
  flood_wait: 'FloodWait',
  account_limited: '账号受限',
  account_unavailable: '账号不可用',
  content_rejected: '内容拦截',
  duplicate_content: '重复内容',
  quiet_hours: '静默时段',
  rate_limit: '频率限制',
  proxy_missing: '代理未配置',
  proxy_alert_active: '代理告警',
  proxy_disabled: '代理禁用',
  proxy_unreachable: '代理不可达',
  proxy_timeout: '代理超时',
  proxy_auth_failed: '代理认证失败',
};

function labelOf(value: string | null | undefined) {
  if (!value) return '未配置';
  return STATUS_LABELS[value] ?? value;
}

function riskLevelTone(level: string) {
  if (level === 'A') return 'positive';
  if (level === 'B' || level === 'C') return 'warning';
  return 'danger';
}

function severityTone(severity: string) {
  if (severity === 'critical') return 'danger';
  if (severity === 'warning') return 'warning';
  return 'neutral';
}

function riskLevelLabel(level: string) {
  const labels: Record<string, string> = {
    A: 'A级 稳健',
    B: 'B级 可用',
    C: 'C级 观察',
    D: 'D级 降频',
    E: 'E级 阻塞',
  };
  return labels[level] ?? level;
}

function formatLimit(used: number, limit: number) {
  return limit > 0 ? `${used}/${limit}` : `${used}/不限`;
}

function defaultProxyAlertIgnoreUntil() {
  return new Date(Date.now() + PROXY_ALERT_IGNORE_DEFAULT_MS).toISOString().slice(0, DATETIME_LOCAL_LENGTH);
}

function errorText(exc: unknown) {
  if (exc instanceof Error) return exc.message;
  if (typeof exc === 'string') return exc;
  return '未知错误';
}

interface Props {
  onOpenAccounts: () => void;
  onOpenAccountDetail?: (accountId: number, tab?: string, context?: AccountDetailContext) => void;
  canManageRisk?: boolean;
  canManageProxies?: boolean;
}

export default function RiskControlView({ onOpenAccounts, onOpenAccountDetail, canManageRisk = false, canManageProxies = false }: Props) {
  const { message, modal } = AntdApp.useApp();
  const location = useLocation();
  const restoredSearchRef = React.useRef('');
  const [summary, setSummary] = React.useState<RiskControlSummary | null>(null);
  const [proxies, setProxies] = React.useState<AccountProxy[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [checkingProxyId, setCheckingProxyId] = React.useState<number | null>(null);
  const [handlingAction, setHandlingAction] = React.useState('');
  const [activeTab, setActiveTab] = React.useState('overview');
  const [policyOpen, setPolicyOpen] = React.useState(false);
  const [policySaving, setPolicySaving] = React.useState(false);
  const [proxyOpen, setProxyOpen] = React.useState(false);
  const [proxySaving, setProxySaving] = React.useState(false);
  const [proxyAlertIgnoreOpen, setProxyAlertIgnoreOpen] = React.useState(false);
  const [proxyAlertIgnoreId, setProxyAlertIgnoreId] = React.useState<number | null>(null);
  const [proxyAlertIgnoreReason, setProxyAlertIgnoreReason] = React.useState('');
  const [proxyAlertIgnoreUntil, setProxyAlertIgnoreUntil] = React.useState('');
  const [error, setError] = React.useState('');
  const [accountQuickFilter, setAccountQuickFilter] = React.useState('');
  const [policyForm] = Form.useForm<RiskGlobalPolicy>();
  const [proxyForm] = Form.useForm<ProxyFormValues>();
  const riskControlDataRequestSeq = React.useRef(0);
  const activeRiskControlActionKey = React.useRef('');
  const activeRiskPolicySaveRequestRef = React.useRef({ seq: 0, signature: '' });
  const activeRiskProxySaveRequestRef = React.useRef({ seq: 0, signature: '' });

  const beginRiskControlDataRequest = React.useCallback(() => {
    riskControlDataRequestSeq.current += 1;
    return riskControlDataRequestSeq.current;
  }, []);

  const isActiveRiskControlDataRequest = React.useCallback((requestSeq: number) => {
    return riskControlDataRequestSeq.current === requestSeq;
  }, []);

  function beginRiskControlAction(actionKey: string) {
    activeRiskControlActionKey.current = actionKey;
    return activeRiskControlActionKey.current;
  }

  function isActiveRiskControlAction(actionKey: string) {
    return activeRiskControlActionKey.current === actionKey;
  }

  function riskPolicyPayloadSignature(payload: RiskGlobalPolicy) {
    return JSON.stringify({
      jitter_min_seconds: payload.jitter_min_seconds,
      jitter_max_seconds: payload.jitter_max_seconds,
      batch_interval_seconds: payload.batch_interval_seconds,
      respect_send_window: payload.respect_send_window,
      quiet_hours_enabled: payload.quiet_hours_enabled,
      quiet_start: payload.quiet_start,
      quiet_end: payload.quiet_end,
      quiet_timezone: payload.quiet_timezone,
      default_max_retries: payload.default_max_retries,
      default_retry_delay_seconds: payload.default_retry_delay_seconds,
      default_retry_backoff: payload.default_retry_backoff,
      default_on_account_banned: payload.default_on_account_banned,
      default_on_api_rate_limit: payload.default_on_api_rate_limit,
      default_on_content_rejected: payload.default_on_content_rejected,
      default_account_hour_limit: payload.default_account_hour_limit,
      default_account_day_limit: payload.default_account_day_limit,
      default_account_cooldown_seconds: payload.default_account_cooldown_seconds,
    });
  }

  function riskProxyPayloadSignature(payload: ProxyFormValues) {
    return JSON.stringify({
      id: payload.id ?? null,
      name: payload.name,
      protocol: payload.protocol,
      host: payload.host,
      port: payload.port,
      username: payload.username || '',
      password: payload.password || '',
      check_interval_seconds: payload.check_interval_seconds,
      timeout_ms: payload.timeout_ms,
      max_bound_accounts: payload.max_bound_accounts,
      max_concurrent_sessions: payload.max_concurrent_sessions,
      notes: payload.notes || '',
    });
  }

  function beginRiskPolicySaveRequest(signature: string) {
    activeRiskPolicySaveRequestRef.current = { seq: activeRiskPolicySaveRequestRef.current.seq + 1, signature };
    return activeRiskPolicySaveRequestRef.current;
  }

  function beginRiskProxySaveRequest(signature: string) {
    activeRiskProxySaveRequestRef.current = { seq: activeRiskProxySaveRequestRef.current.seq + 1, signature };
    return activeRiskProxySaveRequestRef.current;
  }

  function currentRiskPolicyPayloadSignature() {
    return riskPolicyPayloadSignature(policyForm.getFieldsValue(true) as RiskGlobalPolicy);
  }

  function currentRiskProxyPayloadSignature() {
    return riskProxyPayloadSignature(proxyForm.getFieldsValue(true) as ProxyFormValues);
  }

  function isActiveRiskPolicySaveRequest(request: { seq: number; signature: string }) {
    return activeRiskPolicySaveRequestRef.current.seq === request.seq;
  }

  function isActiveRiskProxySaveRequest(request: { seq: number; signature: string }) {
    return activeRiskProxySaveRequestRef.current.seq === request.seq;
  }

  function isCurrentRiskPolicySaveRequest(request: { seq: number; signature: string }) {
    return isActiveRiskPolicySaveRequest(request) && currentRiskPolicyPayloadSignature() === request.signature;
  }

  function isCurrentRiskProxySaveRequest(request: { seq: number; signature: string }) {
    return isActiveRiskProxySaveRequest(request) && currentRiskProxyPayloadSignature() === request.signature;
  }

  const fetchRiskControlData = React.useCallback(async (requestSeq: number) => {
    const [nextSummary, nextProxies] = await Promise.all([
      api<RiskControlSummary>('/risk-control/summary'),
      api<AccountProxy[]>('/account-proxies'),
    ]);
    if (!isActiveRiskControlDataRequest(requestSeq)) return false;
    setSummary(nextSummary);
    setProxies(nextProxies);
    return true;
  }, [isActiveRiskControlDataRequest]);

  const loadSummary = React.useCallback(async () => {
    const requestSeq = beginRiskControlDataRequest();
    setLoading(true);
    setError('');
    try {
      await fetchRiskControlData(requestSeq);
    } catch (exc) {
      if (!isActiveRiskControlDataRequest(requestSeq)) return;
      setError(exc instanceof Error ? exc.message : '读取风控中心失败');
    } finally {
      if (isActiveRiskControlDataRequest(requestSeq)) setLoading(false);
    }
  }, [beginRiskControlDataRequest, fetchRiskControlData, isActiveRiskControlDataRequest]);

  React.useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const quickFilteredAccountRows = React.useMemo(() => {
    const rows = summary?.account_scores ?? [];
    if (accountQuickFilter === 'available') return rows.filter((row) => row.can_join_task);
    if (accountQuickFilter === 'degraded') return rows.filter((row) => ['B', 'C', 'D'].includes(row.risk_level));
    if (accountQuickFilter === 'blocked') return rows.filter((row) => row.risk_level === 'E');
    return rows;
  }, [accountQuickFilter, summary?.account_scores]);
  const accountTable = useAntdTableControls<RiskControlAccountScore>({
    rows: quickFilteredAccountRows,
    placeholder: '搜索账号 / 分组 / 状态 / 风险 / 代理',
    search: [(row) => [row.account_id, row.display_name, row.username, row.pool_name, row.login_status, row.risk_level, riskLevelLabel(row.risk_level), row.can_join_task ? '可用' : '不可用', row.recent_risk, row.blocked_reason, row.proxy_name, row.proxy_local_address, row.proxy_status, row.proxy_alert_status, ...(row.score_reasons ?? []), ...(row.non_score_reasons ?? [])]],
  });
  const queueTable = useAntdTableControls<RiskDispositionItem>({
    rows: summary?.disposition_queue ?? [],
    placeholder: '搜索处置类型 / 账号 / 原因',
    search: [(row) => [row.item_type, row.account_name, row.target, row.reason, row.suggested_action, row.status]],
  });
  const hitTable = useAntdTableControls<RiskHitRecord>({
    rows: summary?.hit_records ?? [],
    placeholder: '搜索命中来源 / 账号 / 策略 / 任务',
    search: [(row) => [row.source, row.account_name, row.task_id, row.target, row.policy, row.action, row.detail, row.impact_scope, row.suggested_entry, row.affects_health_score ? '扣健康分' : '不扣健康分']],
  });
  const policyAuditTable = useAntdTableControls<RiskPolicyAudit>({
    rows: summary?.policy_audits ?? [],
    placeholder: '搜索策略动作 / 操作人 / 对象 / 原因',
    search: [(row) => [row.actor, row.action, row.target_type, row.target_id, row.target_label, row.detail]],
  });

  function positiveSearchNumber(value: string | null, fallback: number) {
    const parsed = Number(value || fallback);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }

  function restoreRiskTableContext(params: URLSearchParams) {
    const tab = params.get('tab') || 'overview';
    if (!RISK_TAB_KEYS.has(tab)) return;
    const query = params.get('query') || '';
    const page = positiveSearchNumber(params.get('page'), 1);
    const pageSize = positiveSearchNumber(params.get('page_size'), 10);
    setActiveTab(tab);
    if (tab === 'accounts') {
      setAccountQuickFilter(params.get('quick_filter') || '');
      accountTable.setQuery(query);
      accountTable.setPage(page, pageSize);
    }
    if (tab === 'queue') {
      queueTable.setQuery(query);
      queueTable.setPage(page, pageSize);
    }
    if (tab === 'hits') {
      hitTable.setQuery(query);
      hitTable.setPage(page, pageSize);
    }
    if (tab === 'policy-audit') {
      policyAuditTable.setQuery(query);
      policyAuditTable.setPage(page, pageSize);
    }
  }

  React.useEffect(() => {
    if (!summary) return;
    const params = new URLSearchParams(location.search);
    if (!params.get('tab') && !params.get('query') && !params.get('quick_filter')) return;
    if (restoredSearchRef.current === location.search) return;
    restoredSearchRef.current = location.search;
    restoreRiskTableContext(params);
  }, [location.search, summary]);

  function handleMetricClick(metric: RiskControlMetric) {
    if (metric.key === 'available_accounts') {
      setAccountQuickFilter('available');
      accountTable.setQuery('');
      setActiveTab('accounts');
      return;
    }
    if (metric.key === 'degraded_accounts') {
      setAccountQuickFilter('degraded');
      accountTable.setQuery('');
      setActiveTab('accounts');
      return;
    }
    if (metric.key === 'blocked_accounts') {
      setAccountQuickFilter('blocked');
      accountTable.setQuery('');
      setActiveTab('accounts');
      return;
    }
    if (metric.key === 'pending_dispositions') {
      setActiveTab('queue');
      return;
    }
    if (metric.key === 'recent_flood_wait') {
      hitTable.setQuery('FloodWait');
      setActiveTab('hits');
      return;
    }
    if (metric.key === 'proxy_alerts') {
      setActiveTab('proxy-alerts');
    }
  }

  async function refreshRiskControlSummaryAfterAction(actionLabel: string) {
    const requestSeq = beginRiskControlDataRequest();
    try {
      await fetchRiskControlData(requestSeq);
    } catch (exc) {
      if (!isActiveRiskControlDataRequest(requestSeq)) return;
      setError(`风控中心数据刷新失败：${actionLabel}操作已完成，但刷新风控中心数据失败：${errorText(exc)}`);
    }
  }

  async function checkProxy(proxyId: number) {
    if (!canManageProxies) return;
    const actionKey = beginRiskControlAction(`proxy-check:${proxyId}`);
    setCheckingProxyId(proxyId);
    setError('');
    try {
      await api(`/account-proxies/${proxyId}/check`, {
        method: 'POST',
        body: JSON.stringify({ check_type: 'quick', reason: '风控中心手动检查' }),
      });
      if (!isActiveRiskControlAction(actionKey)) return;
      void message.success('代理健康检查已完成');
      await refreshRiskControlSummaryAfterAction('代理健康检查');
    } catch (exc) {
      if (!isActiveRiskControlAction(actionKey)) return;
      setError(exc instanceof Error ? exc.message : '代理检查失败');
    } finally {
      if (isActiveRiskControlAction(actionKey)) setCheckingProxyId(null);
    }
  }

  function openPolicyEdit() {
    if (!canManageRisk) return;
    if (!summary?.global_policy) return;
    policyForm.setFieldsValue(summary.global_policy);
    setPolicyOpen(true);
  }

  async function savePolicy() {
    if (!canManageRisk) return;
    const values = await policyForm.validateFields();
    const signature = riskPolicyPayloadSignature(values);
    const saveRequest = beginRiskPolicySaveRequest(signature);
    setPolicySaving(true);
    setError('');
    try {
      await api('/risk-control/global-policy', {
        method: 'PATCH',
        body: JSON.stringify(values),
      });
      if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;
      setPolicyOpen(false);
      void message.success('全局风控策略已保存');
      await refreshRiskControlSummaryAfterAction('全局风控策略保存');
    } catch (exc) {
      if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;
      setError(exc instanceof Error ? exc.message : '保存全局策略失败');
    } finally {
      if (isActiveRiskPolicySaveRequest(saveRequest)) setPolicySaving(false);
    }
  }

  function openProxyCreate() {
    if (!canManageProxies) return;
    proxyForm.setFieldsValue({
      id: undefined,
      name: '',
      protocol: 'socks5',
      host: '127.0.0.1',
      port: 1080,
      username: '',
      password: '',
      check_interval_seconds: 300,
      timeout_ms: 3000,
      max_bound_accounts: 5,
      max_concurrent_sessions: 2,
      notes: '',
    });
    setProxyOpen(true);
  }

  function openProxyEdit(proxy: AccountProxy) {
    if (!canManageProxies) return;
    proxyForm.setFieldsValue({
      id: proxy.id,
      name: proxy.name,
      protocol: proxy.protocol,
      host: proxy.host,
      port: proxy.port,
      username: proxy.username,
      password: '',
      check_interval_seconds: proxy.check_interval_seconds,
      timeout_ms: proxy.timeout_ms,
      max_bound_accounts: proxy.max_bound_accounts,
      max_concurrent_sessions: proxy.max_concurrent_sessions,
      notes: proxy.notes,
    });
    setProxyOpen(true);
  }

  async function saveProxy() {
    if (!canManageProxies) return;
    const values = await proxyForm.validateFields();
    const isEdit = Boolean(values.id);
    const signature = riskProxyPayloadSignature(values);
    const saveRequest = beginRiskProxySaveRequest(signature);
    setProxySaving(true);
    setError('');
    try {
      const payload = isEdit
        ? {
            name: values.name,
            protocol: values.protocol,
            host: values.host,
            port: values.port,
            username: values.username || '',
            password_reset: values.password || undefined,
            check_interval_seconds: values.check_interval_seconds,
            timeout_ms: values.timeout_ms,
            max_bound_accounts: values.max_bound_accounts,
            max_concurrent_sessions: values.max_concurrent_sessions,
            notes: values.notes || '',
            change_reason: '风控中心编辑代理资源',
          }
        : {
            name: values.name,
            protocol: values.protocol,
            host: values.host,
            port: values.port,
            username: values.username || '',
            password: values.password || '',
            check_interval_seconds: values.check_interval_seconds,
            timeout_ms: values.timeout_ms,
            max_bound_accounts: values.max_bound_accounts,
            max_concurrent_sessions: values.max_concurrent_sessions,
            notes: values.notes || '',
          };
      await api(isEdit ? `/account-proxies/${values.id}` : '/account-proxies', {
        method: isEdit ? 'PATCH' : 'POST',
        body: JSON.stringify(payload),
      });
      if (!isCurrentRiskProxySaveRequest(saveRequest)) return;
      setProxyOpen(false);
      void message.success(isEdit ? '代理资源已保存' : '代理资源已新增');
      await refreshRiskControlSummaryAfterAction(isEdit ? '代理资源保存' : '代理资源新增');
    } catch (exc) {
      if (!isCurrentRiskProxySaveRequest(saveRequest)) return;
      setError(exc instanceof Error ? exc.message : '保存代理资源失败');
    } finally {
      if (isActiveRiskProxySaveRequest(saveRequest)) setProxySaving(false);
    }
  }

  async function disableProxy(proxy: AccountProxy) {
    if (!canManageProxies) return;
    modal.confirm({
      title: `禁用代理 ${proxy.name}`,
      content: '禁用后，绑定该代理的账号会被风控阻塞，直到重新启用或切换代理。',
      okText: '确认禁用',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await api(`/account-proxies/${proxy.id}/disable`, {
            method: 'POST',
            body: JSON.stringify({ disabled_reason: '风控中心手动禁用' }),
          });
          void message.success('代理已禁用');
          await refreshRiskControlSummaryAfterAction('代理禁用');
        } catch (exc) {
          setError(exc instanceof Error ? exc.message : '禁用代理失败');
        }
      },
    });
  }

  async function handleProxyAlert(alertId: number, action: ProxyAlertAction) {
    if (!canManageProxies) return;
    const key = `proxy-alert:${alertId}:${action}`;
    beginRiskControlAction(key);
    setHandlingAction(key);
    setError('');
    try {
      await api(`/proxy-alerts/${alertId}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason: action === 'acknowledge' ? '风控中心标记处理中' : '风控中心标记已恢复' }),
      });
      if (!isActiveRiskControlAction(key)) return;
      void message.success(action === 'acknowledge' ? '已标记处理中' : '已标记恢复');
      await refreshRiskControlSummaryAfterAction(action === 'acknowledge' ? '代理告警标记处理中' : '代理告警标记恢复');
    } catch (exc) {
      if (!isActiveRiskControlAction(key)) return;
      setError(exc instanceof Error ? exc.message : '处理代理告警失败');
    } finally {
      if (isActiveRiskControlAction(key)) setHandlingAction('');
    }
  }

  function openProxyAlertIgnore(alertId: number) {
    if (!canManageProxies) return;
    setProxyAlertIgnoreId(alertId);
    setProxyAlertIgnoreReason('');
    setProxyAlertIgnoreUntil(defaultProxyAlertIgnoreUntil());
    setProxyAlertIgnoreOpen(true);
  }

  async function submitProxyAlertIgnore() {
    if (!canManageProxies || !proxyAlertIgnoreId) return;
    const reason = proxyAlertIgnoreReason.trim();
    if (!reason) {
      void message.error('请填写忽略原因');
      return;
    }
    if (!proxyAlertIgnoreUntil) {
      void message.error('请选择忽略到期时间');
      return;
    }
    const key = `proxy-alert:${proxyAlertIgnoreId}:ignore`;
    beginRiskControlAction(key);
    setHandlingAction(key);
    setError('');
    try {
      await api(`/proxy-alerts/${proxyAlertIgnoreId}/ignore`, {
        method: 'POST',
        body: JSON.stringify({ reason, ignored_until: proxyAlertIgnoreUntil }),
      });
      if (!isActiveRiskControlAction(key)) return;
      setProxyAlertIgnoreOpen(false);
      void message.success('代理告警已忽略');
      await refreshRiskControlSummaryAfterAction('代理告警忽略');
    } catch (exc) {
      if (!isActiveRiskControlAction(key)) return;
      setError(exc instanceof Error ? exc.message : '忽略代理告警失败');
    } finally {
      if (isActiveRiskControlAction(key)) setHandlingAction('');
    }
  }

  function proxyAlertIdFromKey(key: string) {
    const match = key.match(/^proxy-alert:(\d+)$/);
    return match ? Number(match[1]) : null;
  }

  function proxyIdFromDisposition(row: RiskDispositionItem) {
    const match = row.key.match(/^proxy:\d+:(\d+)$/);
    return match ? Number(match[1]) : null;
  }

  function renderDispositionActions(row: RiskDispositionItem) {
    const alertId = proxyAlertIdFromKey(row.key);
    const proxyId = proxyIdFromDisposition(row);
    if ((alertId || proxyId) && !canManageProxies) {
      return <Typography.Text type="secondary">需要代理管理权限</Typography.Text>;
    }
    if (alertId) {
      return (
        <Space size={6} wrap>
          <Button size="small" loading={handlingAction === `proxy-alert:${alertId}:acknowledge`} onClick={() => void handleProxyAlert(alertId, 'acknowledge')}>处理中</Button>
          <Button size="small" loading={handlingAction === `proxy-alert:${alertId}:ignore`} onClick={() => openProxyAlertIgnore(alertId)}>忽略</Button>
          <Button size="small" type="primary" loading={handlingAction === `proxy-alert:${alertId}:resolve`} onClick={() => void handleProxyAlert(alertId, 'resolve')}>已恢复</Button>
        </Space>
      );
    }
    if (proxyId) {
      return (
        <Space size={6} wrap>
          <Button size="small" loading={checkingProxyId === proxyId} onClick={() => void checkProxy(proxyId)}>检查代理</Button>
          <Button size="small" onClick={onOpenAccounts}>切换账号代理</Button>
        </Space>
      );
    }
    if (row.account_id) {
      return (
        <Space size={6} wrap>
          <Button size="small" onClick={() => openAccountCenter({ account_id: row.account_id!, blocked_reason: row.reason } as RiskControlAccountScore)}>去账号中心</Button>
          <Button size="small" onClick={() => setActiveTab('hits')}>看命中</Button>
        </Space>
      );
    }
    return <Button size="small" onClick={() => setActiveTab('hits')}>查看详情</Button>;
  }

  function accountDetailTabFor(row: RiskControlAccountScore) {
    const reason = `${row.blocked_reason} ${row.proxy_risk_reason} ${row.security_risk_reason} ${row.score_reasons?.join(' ')}`;
    if (/备用|授权|standby|session/i.test(reason)) return '授权资产';
    if (/代理/.test(reason)) return '可用性';
    if (/二步|可信|外部|资料/.test(reason)) return '账号安全';
    return '可用性';
  }

  function accountDetailContextFor(row: RiskControlAccountScore) {
    const issue = row.blocked_reason || row.proxy_risk_reason || row.security_risk_reason || row.recent_risk || row.score_reasons?.[0] || row.non_score_reasons?.[0];
    return { issue, ...riskTableReturnContext() };
  }

  function riskTableReturnContext() {
    const table = activeTab === 'queue'
      ? queueTable
      : activeTab === 'hits'
        ? hitTable
        : activeTab === 'policy-audit'
          ? policyAuditTable
          : accountTable;
    return {
      riskTab: activeTab,
      riskQuery: table.query,
      riskPage: Number(table.pagination.current ?? 1),
      riskPageSize: Number(table.pagination.pageSize ?? 10),
      riskQuickFilter: activeTab === 'accounts' ? accountQuickFilter : undefined,
    };
  }

  function openAccountCenter(row: RiskControlAccountScore) {
    if (onOpenAccountDetail) {
      onOpenAccountDetail(row.account_id, accountDetailTabFor(row), accountDetailContextFor(row));
      return;
    }
    onOpenAccounts();
  }

  const accountColumns: ColumnsType<RiskControlAccountScore> = [
    {
      title: '账号',
      key: 'account',
      fixed: 'left',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.display_name}</Typography.Text>
          <Typography.Text type="secondary">@{row.username ?? '未设置'} / {riskAccountPhone(row)}</Typography.Text>
          <Typography.Text type="secondary">{row.pool_name}</Typography.Text>
        </Space>
      ),
    },
    { title: '登录状态', dataIndex: 'login_status', width: 130, render: (value: string) => <StatusBadge status={labelOf(value)} /> },
    { title: '等级', dataIndex: 'risk_level', width: 110, render: (value: string) => <Badge tone={riskLevelTone(value)}>{riskLevelLabel(value)}</Badge> },
    { title: '健康分', dataIndex: 'health_score', width: 150, render: (value: number) => <Progress percent={value} size="small" status={value < 55 ? 'exception' : value < 85 ? 'normal' : 'success'} /> },
    {
      title: '本地代理',
      key: 'proxy',
      width: 240,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.proxy_name || '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">{row.proxy_local_address || '建议绑定后执行高频发送'}</Typography.Text>
          {row.proxy_status && <StatusBadge status={labelOf(row.proxy_status)} label={row.proxy_alert_status ? `${labelOf(row.proxy_status)} / ${labelOf(row.proxy_alert_status)}` : labelOf(row.proxy_status)} />}
        </Space>
      ),
    },
    {
      title: '账号安全',
      key: 'account_security',
      width: 210,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <StatusBadge status={labelOf(row.trusted_session_status)} label={`可信 ${labelOf(row.trusted_session_status)}`} />
          <Typography.Text type="secondary">外部设备：{row.external_authorization_count}</Typography.Text>
          <Typography.Text type="secondary">2FA：{labelOf(row.two_fa_status)}</Typography.Text>
          {row.security_risk_reason && <Typography.Text type="secondary">{row.security_risk_reason}</Typography.Text>}
        </Space>
      ),
    },
    { title: '当前策略', dataIndex: 'current_policy', width: 130, render: (value: string) => labelOf(value) },
    {
      title: '扣分原因',
      key: 'score_reasons',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          {(row.score_reasons ?? []).map((reason) => <Typography.Text key={reason} type="secondary">{reason}</Typography.Text>)}
        </Space>
      ),
    },
    {
      title: '非扣分失败',
      key: 'non_score_reasons',
      width: 240,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          {(row.non_score_reasons ?? []).map((reason) => <Typography.Text key={reason} type="secondary">{reason}</Typography.Text>)}
          {!(row.non_score_reasons ?? []).length && <Typography.Text type="secondary">-</Typography.Text>}
        </Space>
      ),
    },
    { title: '小时用量', key: 'hour', width: 110, render: (_, row) => formatLimit(row.hour_usage, row.hour_limit) },
    { title: '日用量', key: 'day', width: 110, render: (_, row) => formatLimit(row.day_usage, row.day_limit) },
    { title: '冷却到', dataIndex: 'cooldown_until', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '最近风险', dataIndex: 'recent_risk', width: 180, render: (value: string) => value || '-' },
    { title: '准入', dataIndex: 'can_join_task', width: 100, render: (value: boolean, row) => value ? <StatusBadge status="可用" /> : <StatusBadge status={labelOf(row.blocked_reason || '容量不足')} /> },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      fixed: 'right',
      render: (_, row) => (
        <Space size={6} wrap>
          <Button size="small" onClick={() => openAccountCenter(row)}>账号中心处理</Button>
          <Button size="small" onClick={() => { hitTable.setQuery(String(row.account_id)); setActiveTab('hits'); }}>看命中</Button>
        </Space>
      ),
    },
  ];

  const queueColumns: ColumnsType<RiskDispositionItem> = [
    { title: '类型', dataIndex: 'item_type', width: 150, fixed: 'left' },
    { title: '级别', dataIndex: 'severity', width: 100, render: (value: string) => <Badge tone={severityTone(value)}>{labelOf(value)}</Badge> },
    { title: '账号', dataIndex: 'account_name', width: 160, render: (value: string) => value || '-' },
    { title: '原因', dataIndex: 'reason', width: 280 },
    { title: '处置动作', dataIndex: 'suggested_action', width: 240 },
    { title: '时间', dataIndex: 'occurred_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <StatusBadge status={labelOf(value)} /> },
    { title: '操作', key: 'actions', width: 220, fixed: 'right', render: (_, row) => renderDispositionActions(row) },
  ];

  const hitColumns: ColumnsType<RiskHitRecord> = [
    { title: '来源', dataIndex: 'source', width: 120, render: (value: string) => labelOf(value) },
    { title: '账号', dataIndex: 'account_name', width: 160, render: (value: string) => value || '-' },
    { title: '任务/记录', dataIndex: 'task_id', width: 130 },
    { title: '命中策略', dataIndex: 'policy', width: 160, render: (value: string) => <StatusBadge status={labelOf(value)} /> },
    { title: '系统动作', dataIndex: 'action', width: 170, render: (value: string) => labelOf(value) },
    { title: '影响对象', dataIndex: 'impact_scope', width: 130, render: (value: string) => labelOf(value) },
    { title: '健康分', dataIndex: 'affects_health_score', width: 120, render: (value: boolean) => value ? <Badge tone="danger">扣分</Badge> : <Badge tone="neutral">不扣分</Badge> },
    { title: '处理入口', dataIndex: 'suggested_entry', width: 180 },
    { title: '失败原因', dataIndex: 'detail', width: 280 },
    { title: '时间', dataIndex: 'occurred_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
  ];

  const policyAuditColumns: ColumnsType<RiskPolicyAudit> = [
    { title: '动作', dataIndex: 'action', width: 170, fixed: 'left' },
    { title: '操作人', dataIndex: 'actor', width: 130 },
    { title: '对象', dataIndex: 'target_label', width: 140 },
    { title: '对象ID', dataIndex: 'target_id', width: 130 },
    { title: '原因 / 变更详情', dataIndex: 'detail', width: 360, render: (value: string) => value || '-' },
    { title: '时间', dataIndex: 'occurred_at', width: 170, render: (value: string) => formatBeijingDateTime(value) },
  ];

  const proxyColumns: ColumnsType<RiskProxyAlert> = [
    { title: 'proxy_id', dataIndex: 'proxy_id', width: 140 },
    { title: '本地代理地址', dataIndex: 'local_address', width: 220 },
    { title: '告警状态', dataIndex: 'alert_status', width: 140, render: (value: string) => <StatusBadge status={labelOf(value)} /> },
    { title: '绑定账号', dataIndex: 'bound_accounts', width: 110 },
    { title: '最近错误', dataIndex: 'last_error', width: 260 },
    { title: '处置动作', dataIndex: 'suggested_action', width: 220 },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      fixed: 'right',
      render: (_, row) => canManageProxies && row.id ? (
        <Space size={6} wrap>
          <Button size="small" loading={handlingAction === `proxy-alert:${row.id}:acknowledge`} onClick={() => void handleProxyAlert(row.id!, 'acknowledge')}>处理中</Button>
          <Button size="small" loading={handlingAction === `proxy-alert:${row.id}:ignore`} onClick={() => openProxyAlertIgnore(row.id!)}>忽略</Button>
          <Button size="small" type="primary" loading={handlingAction === `proxy-alert:${row.id}:resolve`} onClick={() => void handleProxyAlert(row.id!, 'resolve')}>恢复</Button>
        </Space>
      ) : null,
    },
  ];

  const proxyResourceColumns: ColumnsType<AccountProxy> = [
    {
      title: '代理资源',
      key: 'proxy',
      fixed: 'left',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">{row.local_address}</Typography.Text>
          {row.username && <Typography.Text type="secondary">认证用户：{row.username}</Typography.Text>}
        </Space>
      ),
    },
    { title: '状态', key: 'status', width: 160, render: (_, row) => <StatusBadge status={labelOf(row.status)} label={`${labelOf(row.status)} / ${labelOf(row.alert_status)}`} /> },
    { title: '绑定账号', dataIndex: 'bound_account_count', width: 110 },
    { title: '容量', key: 'capacity', width: 150, render: (_, row) => `${row.bound_account_count}/${row.max_bound_accounts || '不限'} 账号` },
    { title: '检查间隔', dataIndex: 'check_interval_seconds', width: 120, render: (value: number) => `${value}s` },
    { title: '最近检查', dataIndex: 'last_check_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '最近错误', dataIndex: 'last_error', width: 260, render: (value: string) => value || '-' },
    {
      title: '操作',
      key: 'actions',
      width: 230,
      fixed: 'right',
      render: (_, row) => {
        if (!canManageProxies) return null;
        return (
          <Space size={6} wrap>
            <Button size="small" onClick={() => openProxyEdit(row)}>编辑</Button>
            <Button size="small" icon={<RefreshCcw size={14} />} loading={checkingProxyId === row.id} onClick={() => void checkProxy(row.id)}>检查</Button>
            <Button size="small" danger disabled={row.status === 'disabled'} onClick={() => void disableProxy(row)}>禁用</Button>
          </Space>
        );
      },
    },
  ];

  if (!summary && error) {
    return (
      <Card className="panel">
        <Empty description={error} />
        <Button icon={<RefreshCcw size={16} />} loading={loading} onClick={loadSummary}>重新加载</Button>
      </Card>
    );
  }

  const policy = summary?.global_policy;

  return (
    <section className="view-grid risk-control-view">
      <div className="stats-grid">
        <StatCard label="当前风控等级" value={summary?.overview.current_level ?? '-'} detail={summary?.overview.quiet_active ? '静默中' : '按策略运行'} icon={<CheckCircle2 size={22} />} />
        {(summary?.overview.metrics ?? []).map((metric) => (
          <button key={metric.key} type="button" className="stat-card-button" onClick={() => handleMetricClick(metric)}>
            <StatCard label={metric.label} value={metric.value} detail={metric.detail} icon={metric.key.includes('proxy') ? <Database size={22} /> : metric.key.includes('blocked') ? <ShieldAlert size={22} /> : <Activity size={22} />} />
          </button>
        ))}
      </div>

      <Card
        className="panel"
        title="风控中心"
        extra={<Button icon={<RefreshCcw size={16} />} loading={loading} onClick={loadSummary}>刷新</Button>}
      >
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: 'overview',
              label: '总览',
              children: (
                <Space direction="vertical" size={16} className="full-width">
                  <Card size="small" className="sub-panel">
                    <Space align="center" size={16} wrap>
                      <Badge tone={summary?.overview.current_level === '正常' ? 'positive' : summary?.overview.current_level === '注意' ? 'warning' : 'danger'}>{summary?.overview.current_level ?? '-'}</Badge>
                      <Typography.Text>{summary?.overview.level_detail ?? '加载中'}</Typography.Text>
                      <StatusBadge status={summary?.overview.quiet_active ? '冷却中' : '可用'} label={summary?.overview.quiet_active ? '静默中' : '非静默'} />
                    </Space>
                  </Card>
                  <Table<RiskDispositionItem>
                    className="tg-table"
                    rowKey="key"
                    columns={queueColumns}
                    dataSource={(summary?.disposition_queue ?? []).slice(0, 8)}
                    pagination={false}
                    scroll={{ x: 1440 }}
                    locale={{ emptyText: '暂无待处理处置项。' }}
                  />
                </Space>
              ),
            },
            {
              key: 'policy',
              label: '全局策略',
              children: policy ? (
                <Space direction="vertical" size={12} className="full-width">
                  <Space className="table-toolbar" wrap>
                      {canManageRisk && <Button type="primary" onClick={openPolicyEdit}>编辑全局策略</Button>}
                    <Typography.Text type="secondary">策略在风控中心维护，任务级配置只能在全局边界内收紧。</Typography.Text>
                  </Space>
                  <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
                    <Descriptions.Item label="发送抖动">{policy.jitter_min_seconds}s - {policy.jitter_max_seconds}s</Descriptions.Item>
                    <Descriptions.Item label="批次间隔">{policy.batch_interval_seconds}s</Descriptions.Item>
                    <Descriptions.Item label="发送窗口">{policy.respect_send_window ? '遵守' : '不限制'}</Descriptions.Item>
                    <Descriptions.Item label="静默时段">{policy.quiet_hours_enabled ? `${policy.quiet_start}-${policy.quiet_end}` : '关闭'}</Descriptions.Item>
                    <Descriptions.Item label="时区">{policy.quiet_timezone}</Descriptions.Item>
                    <Descriptions.Item label="默认重试">{policy.default_max_retries} 次 / {policy.default_retry_delay_seconds}s / {labelOf(policy.default_retry_backoff)}</Descriptions.Item>
                    <Descriptions.Item label="账号异常">{labelOf(policy.default_on_account_banned)}</Descriptions.Item>
                    <Descriptions.Item label="API 限流">{labelOf(policy.default_on_api_rate_limit)}</Descriptions.Item>
                    <Descriptions.Item label="内容拦截">{labelOf(policy.default_on_content_rejected)}</Descriptions.Item>
                    <Descriptions.Item label="账号小时上限">{policy.default_account_hour_limit || '不限'}</Descriptions.Item>
                    <Descriptions.Item label="账号日上限">{policy.default_account_day_limit || '不限'}</Descriptions.Item>
                    <Descriptions.Item label="账号冷却">{policy.default_account_cooldown_seconds}s</Descriptions.Item>
                  </Descriptions>
                </Space>
              ) : <Empty description="暂无策略数据" />,
            },
            {
              key: 'accounts',
              label: '账号评分',
              children: (
                <>
                  <Space className="table-toolbar" wrap>
                    {accountTable.searchInput}
                    {accountQuickFilter && <Button onClick={() => setAccountQuickFilter('')}>清除指标筛选</Button>}
                    <Button onClick={onOpenAccounts}>去账号中心</Button>
                  </Space>
                  <Table<RiskControlAccountScore>
                    className="tg-table"
                    rowKey="account_id"
                    columns={accountColumns}
                    dataSource={accountTable.filteredRows}
                    pagination={accountTable.pagination}
                    loading={loading}
                    scroll={{ x: 2220 }}
                    locale={{ emptyText: '暂无账号评分数据。' }}
                  />
                </>
              ),
            },
            {
              key: 'queue',
              label: '处置队列',
              children: (
                <>
                  <Space className="table-toolbar" wrap>{queueTable.searchInput}</Space>
                  <Table<RiskDispositionItem>
                    className="tg-table"
                    rowKey="key"
                    columns={queueColumns}
                    dataSource={queueTable.filteredRows}
                    pagination={queueTable.pagination}
                    loading={loading}
                    scroll={{ x: 1440 }}
                    locale={{ emptyText: '暂无待处理处置项。' }}
                  />
                </>
              ),
            },
            {
              key: 'hits',
              label: '命中记录',
              children: (
                <>
                  <Space className="table-toolbar" wrap>{hitTable.searchInput}</Space>
                  <Table<RiskHitRecord>
                    className="tg-table"
                    rowKey="key"
                    columns={hitColumns}
                    dataSource={hitTable.filteredRows}
                    pagination={hitTable.pagination}
                    loading={loading}
                    scroll={{ x: 1660 }}
                    locale={{ emptyText: '暂无策略命中记录。' }}
                  />
                </>
              ),
            },
            {
              key: 'policy-audit',
              label: '策略审计',
              children: (
                <>
                  <Space className="table-toolbar" wrap>{policyAuditTable.searchInput}</Space>
                  <Table<RiskPolicyAudit>
                    className="tg-table"
                    rowKey="id"
                    columns={policyAuditColumns}
                    dataSource={policyAuditTable.filteredRows}
                    pagination={policyAuditTable.pagination}
                    loading={loading}
                    scroll={{ x: 1100 }}
                    locale={{ emptyText: '暂无风控策略审计记录。' }}
                  />
                </>
              ),
            },
            {
              key: 'proxy',
              label: '代理资源',
              children: (
                <Space direction="vertical" size={16} className="full-width">
                  <Space className="table-toolbar" wrap>
                    {canManageProxies && <Button type="primary" onClick={openProxyCreate}>新增代理资源</Button>}
                    <Typography.Text type="secondary">只维护 socks5/http 本地地址，不管理机场订阅或节点。</Typography.Text>
                  </Space>
                  <Table<AccountProxy>
                    className="tg-table"
                    rowKey="id"
                    columns={proxyResourceColumns}
                    dataSource={proxies}
                    loading={loading}
                    scroll={{ x: 1500 }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无本地代理资源。" /> }}
                  />
                </Space>
              ),
            },
            {
              key: 'proxy-alerts',
              label: '代理告警',
              children: (
                <Space direction="vertical" size={16} className="full-width">
                  <Typography.Text type="secondary">代理不可达、认证失败、超时和恢复状态在这里集中处置。</Typography.Text>
                  <Table<RiskProxyAlert>
                    className="tg-table"
                    rowKey={(row) => row.id ?? row.proxy_id ?? row.local_address}
                    columns={proxyColumns}
                    dataSource={summary?.proxy_alerts ?? []}
                    loading={loading}
                    scroll={{ x: 1280 }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无本地代理告警。" /> }}
                  />
                </Space>
              ),
            },
          ]}
        />
      </Card>

      {error && (
        <Card className="panel compact-panel">
          <Space>
            <ShieldAlert size={18} />
            <Typography.Text type="danger">{error}</Typography.Text>
          </Space>
        </Card>
      )}

      <Modal
        className="tg-modal large"
        title="编辑全局风控策略"
        open={policyOpen}
        onCancel={() => setPolicyOpen(false)}
        onOk={() => void savePolicy()}
        confirmLoading={policySaving}
        okText="保存策略"
        cancelText="取消"
        destroyOnHidden
        centered
        width={860}
      >
        <Form form={policyForm} layout="vertical" className="form-grid">
          <Form.Item name="jitter_min_seconds" label="最小发送抖动" rules={[{ required: true, message: '请输入最小发送抖动' }]}><InputNumber min={0} addonAfter="秒" /></Form.Item>
          <Form.Item name="jitter_max_seconds" label="最大发送抖动" rules={[{ required: true, message: '请输入最大发送抖动' }]}><InputNumber min={0} addonAfter="秒" /></Form.Item>
          <Form.Item name="batch_interval_seconds" label="批次间隔" rules={[{ required: true, message: '请输入批次间隔' }]}><InputNumber min={0} addonAfter="秒" /></Form.Item>
          <Form.Item name="respect_send_window" label="遵守发送窗口" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="quiet_hours_enabled" label="启用静默时段" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="quiet_start" label="静默开始"><Input placeholder="02:00" /></Form.Item>
          <Form.Item name="quiet_end" label="静默结束"><Input placeholder="08:00" /></Form.Item>
          <Form.Item name="quiet_timezone" label="时区"><Input placeholder="Asia/Shanghai" /></Form.Item>
          <Form.Item name="default_max_retries" label="默认重试次数"><InputNumber min={0} max={20} /></Form.Item>
          <Form.Item name="default_retry_delay_seconds" label="重试等待"><InputNumber min={0} addonAfter="秒" /></Form.Item>
          <Form.Item name="default_retry_backoff" label="退避策略"><Select options={[{ value: 'none', label: '不退避' }, { value: 'linear', label: '线性退避' }, { value: 'exponential', label: '指数退避' }]} /></Form.Item>
          <Form.Item name="default_on_account_banned" label="账号异常处理"><Select options={[{ value: 'skip_account', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'stop_task', label: '停止任务' }]} /></Form.Item>
          <Form.Item name="default_on_api_rate_limit" label="API 限流处理"><Select options={[{ value: 'wait_and_retry', label: '等待后重试' }, { value: 'skip', label: '跳过' }, { value: 'pause', label: '暂停' }]} /></Form.Item>
          <Form.Item name="default_on_content_rejected" label="内容拦截处理"><Select options={[{ value: 'skip_message', label: '跳过消息' }, { value: 'rewrite_and_retry', label: '改写后重试' }, { value: 'pause', label: '暂停' }]} /></Form.Item>
          <Form.Item name="default_account_hour_limit" label="账号小时上限"><InputNumber min={0} addonAfter="0 为不限" /></Form.Item>
          <Form.Item name="default_account_day_limit" label="账号日上限"><InputNumber min={0} addonAfter="0 为不限" /></Form.Item>
          <Form.Item name="default_account_cooldown_seconds" label="账号全局冷却"><InputNumber min={0} addonAfter="秒" /></Form.Item>
        </Form>
      </Modal>

      <Modal
        className="tg-modal large"
        title={proxyForm.getFieldValue('id') ? '编辑代理资源' : '新增代理资源'}
        open={proxyOpen}
        onCancel={() => setProxyOpen(false)}
        onOk={() => void saveProxy()}
        confirmLoading={proxySaving}
        okText="保存代理"
        cancelText="取消"
        destroyOnHidden
        centered
        width={820}
      >
        <Form form={proxyForm} layout="vertical" className="form-grid">
          <Form.Item name="id" hidden><Input /></Form.Item>
          <Form.Item name="name" label="代理名称" rules={[{ required: true, message: '请输入代理名称' }]}><Input placeholder="proxy_1080" /></Form.Item>
          <Form.Item name="protocol" label="协议"><Select options={[{ value: 'socks5', label: 'SOCKS5' }, { value: 'http', label: 'HTTP' }]} /></Form.Item>
          <Form.Item name="host" label="本地地址" rules={[{ required: true, message: '请输入本地地址' }]}><Input placeholder="127.0.0.1" /></Form.Item>
          <Form.Item name="port" label="本地端口" rules={[{ required: true, message: '请输入本地端口' }]}><InputNumber min={1} max={65535} /></Form.Item>
          <Form.Item name="username" label="认证用户"><Input placeholder="无认证可留空" /></Form.Item>
          <Form.Item name="password" label="认证密码"><Input.Password placeholder={proxyForm.getFieldValue('id') ? '不修改则留空' : '无认证可留空'} /></Form.Item>
          <Form.Item name="check_interval_seconds" label="检查间隔"><InputNumber min={30} addonAfter="秒" /></Form.Item>
          <Form.Item name="timeout_ms" label="检查超时"><InputNumber min={100} addonAfter="毫秒" /></Form.Item>
          <Form.Item name="max_bound_accounts" label="最多绑定账号"><InputNumber min={0} addonAfter="0 为不限" /></Form.Item>
          <Form.Item name="max_concurrent_sessions" label="最大并发会话"><InputNumber min={0} addonAfter="0 为不限" /></Form.Item>
          <Form.Item name="notes" label="备注" className="wide-field"><Input.TextArea rows={3} placeholder="只记录本地代理用途，不写机场订阅或节点名称" /></Form.Item>
        </Form>
      </Modal>

      <Modal
        title="忽略代理告警"
        open={proxyAlertIgnoreOpen}
        onCancel={() => setProxyAlertIgnoreOpen(false)}
        onOk={() => void submitProxyAlertIgnore()}
        confirmLoading={handlingAction === `proxy-alert:${proxyAlertIgnoreId}:ignore`}
        okText="确认忽略"
        cancelText="取消"
        destroyOnHidden
        centered
      >
        <Space direction="vertical" size={12} className="full-width">
          <label>
            <Typography.Text>忽略原因</Typography.Text>
            <Input.TextArea rows={3} value={proxyAlertIgnoreReason} onChange={(event) => setProxyAlertIgnoreReason(event.target.value)} placeholder="说明为什么暂时忽略该代理告警" />
          </label>
          <label>
            <Typography.Text>忽略到期时间</Typography.Text>
            <Input type="datetime-local" value={proxyAlertIgnoreUntil} onChange={(event) => setProxyAlertIgnoreUntil(event.target.value)} />
          </label>
        </Space>
      </Modal>
    </section>
  );
}
