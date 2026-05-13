import React from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircle2, Database, RefreshCcw, ShieldAlert } from 'lucide-react';
import { API_BASE, api } from '../../shared/api/client';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';
import type { OperationTarget, RuleSet, RuleSetBoundTask } from '../types';
import { formatBeijingDateTime } from '../time';

type RuleRow = {
  key: string;
  category: string;
  name: string;
  status: string;
  detail: string;
  version: string;
  source: string;
  metadata: Record<string, any>;
};

type RuleSummary = {
  system_rule_count: number;
  keyword_rule_count: number;
  relay_task_rule_count: number;
  items: RuleRow[];
  conflicts: RuleConflict[];
  execution_metrics: RuleExecutionMetric[];
  target_metrics: RuleDimensionMetric[];
  account_metrics: RuleDimensionMetric[];
  keyword_metrics: RuleDimensionMetric[];
  trend_metrics: RuleTrendMetric[];
  conversion_metrics: RuleConversionMetric[];
  cross_metrics: RuleCrossMetric[];
};

type RuleVersionRow = RuleSet['versions'][number] & {
  rule_set_name: string;
  ruleSet: RuleSet;
};

type RelayMaterialAttribution = {
  key: string;
  material_fingerprint: string;
  sample_text: string;
  task_count: number;
  source_event_count: number;
  target_count: number;
  account_count: number;
  action_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  pending_count: number;
  retry_count: number;
  success_rate: number;
  last_used_at: string | null;
};

type RelayAttributionReport = {
  total_materials: number;
  total_source_events: number;
  total_actions: number;
  rows: RelayMaterialAttribution[];
};

type RuleConflict = {
  key: string;
  level: string;
  title: string;
  detail: string;
  related_ids: string[];
};

type RuleExecutionMetric = {
  key: string;
  rule_set_id: number | null;
  rule_set_version_id: number | null;
  rule_set_name: string;
  version: number | null;
  task_count: number;
  action_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  pending_count: number;
  last_used_at: string | null;
};

type RuleDimensionMetric = {
  key: string;
  dimension: 'target' | 'account' | 'keyword';
  name: string;
  related_id: string;
  action_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  pending_count: number;
  last_used_at: string | null;
};

type RuleTrendMetric = {
  date: string;
  action_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  pending_count: number;
};

type RuleConversionMetric = {
  key: string;
  rule_set_id: number | null;
  rule_set_version_id: number | null;
  rule_set_name: string;
  version: number | null;
  current_action_count: number;
  current_success_count: number;
  current_success_rate: number;
  previous_action_count: number;
  previous_success_count: number;
  previous_success_rate: number;
  success_rate_delta: number;
};

type RuleCrossMetric = {
  key: string;
  rule_set_id: number | null;
  rule_set_version_id: number | null;
  rule_set_name: string;
  version: number | null;
  target_group_id: number | null;
  target_name: string;
  account_id: number | null;
  account_name: string;
  action_count: number;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  pending_count: number;
  success_rate: number;
  last_used_at: string | null;
};

type RuleTestResult = {
  result: string;
  test_mode: string;
  is_test_data: boolean;
  hits: Array<{ rule_id: number; keyword: string; match_type: string; note: string }>;
  input_hits: string[];
  output_candidates: Array<{ index: number; original_text: string; passed: boolean; action: string; reason: string; transformed_text: string }>;
  should_block: boolean;
  block_reason: string;
  filter_passed: boolean;
  filter_reason: string;
  rule_set_version_id: number | null;
  rule_set_name: string;
  transformed_text: string;
  target_summary: string;
  target_routes: Array<{ group_id: number; title: string; status: string; can_send_account_count: number; account_strategy: string }>;
  account_strategy: string;
  rate_limit_summary: string;
};

export default function RulesCenterView({ onOpenSystemConfig }: { onOpenSystemConfig: () => void }) {
  void onOpenSystemConfig;
  const [summary, setSummary] = React.useState<RuleSummary>({ system_rule_count: 0, keyword_rule_count: 0, relay_task_rule_count: 0, items: [], conflicts: [], execution_metrics: [], target_metrics: [], account_metrics: [], keyword_metrics: [], trend_metrics: [], conversion_metrics: [], cross_metrics: [] });
  const [relayReport, setRelayReport] = React.useState<RelayAttributionReport>({ total_materials: 0, total_source_events: 0, total_actions: 0, rows: [] });
  const [ruleSets, setRuleSets] = React.useState<RuleSet[]>([]);
  const [operationTargets, setOperationTargets] = React.useState<OperationTarget[]>([]);
  const [activeTab, setActiveTab] = React.useState('rule-sets');
  const [testType, setTestType] = React.useState('group_relay');
  const [testMode, setTestMode] = React.useState('rules_only');
  const [sample, setSample] = React.useState('');
  const [candidateSample, setCandidateSample] = React.useState('');
  const [testVersionId, setTestVersionId] = React.useState<number | null>(null);
  const [testSourceGroupId, setTestSourceGroupId] = React.useState('');
  const [testResult, setTestResult] = React.useState<RuleTestResult>({
    result: '未测试',
    test_mode: 'rules_only',
    is_test_data: true,
    hits: [],
    input_hits: [],
    output_candidates: [],
    should_block: false,
    block_reason: '',
    filter_passed: true,
    filter_reason: '',
    rule_set_version_id: null,
    rule_set_name: '',
    transformed_text: '',
    target_summary: '按绑定任务/目标路由',
    target_routes: [],
    account_strategy: '按任务账号策略选择',
    rate_limit_summary: '执行时按账号冷却、小时/日上限校验',
  });
  const [loading, setLoading] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [testerOpen, setTesterOpen] = React.useState(false);
  const [versionTarget, setVersionTarget] = React.useState<RuleSet | null>(null);
  const [versionListTarget, setVersionListTarget] = React.useState<RuleSet | null>(null);
  const [boundTaskTarget, setBoundTaskTarget] = React.useState<RuleSet | null>(null);
  const [boundTasks, setBoundTasks] = React.useState<RuleSetBoundTask[]>([]);
  const [detailRule, setDetailRule] = React.useState<RuleRow | null>(null);
  const [error, setError] = React.useState('');
  const [createForm] = Form.useForm();
  const [versionForm] = Form.useForm();

  async function load() {
    setLoading(true);
    setError('');
    try {
      const [nextSummary, nextRuleSets, nextTargets, nextRelayReport] = await Promise.all([
        api<RuleSummary>('/rules/summary'),
        api<RuleSet[]>('/rule-sets'),
        api<OperationTarget[]>('/operation-targets?target_type=group').catch(() => [] as OperationTarget[]),
        api<RelayAttributionReport>('/rules/relay-attribution/report').catch(() => ({ total_materials: 0, total_source_events: 0, total_actions: 0, rows: [] })),
      ]);
      setSummary(nextSummary);
      setRuleSets(nextRuleSets);
      setOperationTargets(nextTargets);
      setRelayReport(nextRelayReport);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  async function runRuleTest() {
    setTesting(true);
    setError('');
    try {
      setTestResult(await api<RuleTestResult>('/rules/test', {
        method: 'POST',
        body: JSON.stringify({
          text: sample,
          test_type: testType,
          test_mode: testMode,
          candidates: candidateSample.split(/\n+/).map((item) => item.trim()).filter(Boolean),
          rule_set_version_id: testVersionId,
          source_group_id: testSourceGroupId.trim() ? Number(testSourceGroupId) : null,
        }),
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTesting(false);
    }
  }

  async function exportRelayAttribution() {
    setError('');
    try {
      const token = localStorage.getItem('tg_ops_token');
      const response = await fetch(`${API_BASE}/rules/relay-attribution/export`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'relay-attribution.csv';
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function ruleConfig(values: Record<string, string>) {
    const read = (key: string) => {
      const raw = values[key]?.trim() || '{}';
      const parsed = JSON.parse(raw);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${key} 必须是 JSON 对象`);
      }
      return parsed;
    };
    return {
      filters: read('filters'),
      transforms: read('transforms'),
      routing: read('routing'),
      account_strategy: read('account_strategy'),
      rate_limits: read('rate_limits'),
      retry_policy: read('retry_policy'),
      output_checks: read('output_checks'),
    };
  }

  async function createRuleSet() {
    setSaving(true);
    setError('');
    try {
      const values = await createForm.validateFields();
      await api<RuleSet>('/rule-sets', {
        method: 'POST',
        body: JSON.stringify({
          name: values.name,
          description: values.description ?? '',
          task_types: values.task_types ?? [],
          default_policy: { input_failure: values.input_failure ?? 'skip', output_failure: values.output_failure ?? 'transform_once_drop', version_binding: values.version_binding ?? 'follow_current' },
          ...ruleConfig(values),
        }),
      });
      setCreateOpen(false);
      createForm.resetFields();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function createRuleSetVersion() {
    if (!versionTarget) return;
    setSaving(true);
    setError('');
    try {
      const values = await versionForm.validateFields();
      await api<RuleSet>(`/rule-sets/${versionTarget.id}/versions`, { method: 'POST', body: JSON.stringify(ruleConfig(values)) });
      setVersionTarget(null);
      versionForm.resetFields();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function publishRuleSetVersion(ruleSet: RuleSet, versionId: number) {
    setSaving(true);
    setError('');
    try {
      await api<RuleSet>(`/rule-sets/${ruleSet.id}/versions/${versionId}/publish`, { method: 'POST' });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function copyRuleSetVersion(ruleSet: RuleSet, versionId: number) {
    setSaving(true);
    setError('');
    try {
      await api<RuleSet>(`/rule-sets/${ruleSet.id}/versions/${versionId}/copy`, { method: 'POST' });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function rollbackRuleSetVersion(ruleSet: RuleSet, versionId: number) {
    setSaving(true);
    setError('');
    try {
      await api<RuleSet>(`/rule-sets/${ruleSet.id}/versions/${versionId}/rollback`, { method: 'POST' });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function openBoundTasks(ruleSet: RuleSet) {
    setBoundTaskTarget(ruleSet);
    setError('');
    try {
      setBoundTasks(await api<RuleSetBoundTask[]>(`/rule-sets/${ruleSet.id}/tasks`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBoundTasks([]);
    }
  }

  const table = useAntdTableControls<RuleRow>({
    rows: summary.items,
    placeholder: '搜索规则 / 版本 / 状态',
    search: ['category', 'name', 'status', 'detail', 'version'],
  });
  const columns: ColumnsType<RuleRow> = [
    { title: '规则类别', dataIndex: 'category', width: 150 },
    {
      title: '规则',
      key: 'rule',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">版本 {row.version}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '处理口径', dataIndex: 'detail' },
    { title: '操作', key: 'actions', width: 90, render: (_, row) => <Button size="small" onClick={() => setDetailRule(row)}>详情</Button> },
  ];
  const ruleSetTable = useAntdTableControls<RuleSet>({
    rows: ruleSets,
    placeholder: '搜索规则集 / 状态 / 描述',
    search: ['name', 'description', 'status'],
  });
  const groupTargets = operationTargets.filter((target) => target.target_type === 'group' && target.linked_group_id);
  const ruleSetColumns: ColumnsType<RuleSet> = [
    {
      title: '规则集',
      key: 'name',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">{row.description || '过滤、转换、路由、账号策略、限速、重试'}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '适用任务', dataIndex: 'task_types', width: 220, render: (value: string[]) => taskTypeLabels(value).join(' / ') || '未限定' },
    { title: '活动版本', dataIndex: 'active_version_id', width: 120, render: (_, row) => row.versions.find((version) => version.id === row.active_version_id)?.version ? `v${row.versions.find((version) => version.id === row.active_version_id)?.version}` : '-' },
    { title: '版本数', key: 'versions', width: 100, render: (_, row) => row.versions.length },
    { title: '更新时间', dataIndex: 'updated_at', width: 180, render: (value) => formatBeijingDateTime(value) },
    {
      title: '操作',
      key: 'actions',
      width: 280,
      render: (_, row) => (
        <Space size={8}>
          <Button size="small" onClick={() => setVersionListTarget(row)}>查看版本</Button>
          <Button size="small" onClick={() => openBoundTasks(row)}>绑定任务</Button>
          <Button size="small" icon={<CheckCircle2 size={14} />} onClick={() => { setVersionTarget(row); versionForm.setFieldsValue(ruleFormValuesFromVersion(row, groupTargets)); }}>新建版本</Button>
        </Space>
      ),
    },
  ];
  const versionRows: RuleVersionRow[] = ruleSets.flatMap((ruleSet) => ruleSet.versions.map((version) => ({
    ...version,
    rule_set_name: ruleSet.name,
    ruleSet,
  })));
  const versionColumns: ColumnsType<RuleVersionRow> = [
    { title: '规则集', dataIndex: 'rule_set_name', width: 180, ellipsis: true },
    { title: '版本', key: 'version', width: 90, render: (_, version) => `v${version.version}` },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '过滤', dataIndex: 'filters', render: (value) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
    { title: '输出校验', dataIndex: 'output_checks', render: (value) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
    { title: '转换', dataIndex: 'transforms', render: (value) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
    { title: '路由', dataIndex: 'routing', render: (value) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_, version) => (
        <Space size={8}>
          {version.status === 'published' ? <Typography.Text type="secondary">当前发布</Typography.Text> : <Button size="small" loading={saving} onClick={() => publishRuleSetVersion(version.ruleSet, version.id)}>发布</Button>}
          <Button size="small" loading={saving} onClick={() => copyRuleSetVersion(version.ruleSet, version.id)}>复制草稿</Button>
          {version.status === 'archived' && <Button size="small" danger loading={saving} onClick={() => rollbackRuleSetVersion(version.ruleSet, version.id)}>回滚到此版本</Button>}
        </Space>
      ),
    },
  ];
  const versionOptions = ruleSets.flatMap((ruleSet) => ruleSet.versions.map((version) => ({
    value: version.id,
    label: `${ruleSet.name} / v${version.version} / ${version.status === 'published' ? '已发布' : '草稿'}`,
  })));
  const routeColumns: ColumnsType<RuleTestResult['target_routes'][number]> = [
    { title: '目标群', dataIndex: 'title' },
    { title: '状态', dataIndex: 'status', width: 140, render: (value) => <StatusBadge status={value} /> },
    { title: '可发送账号', dataIndex: 'can_send_account_count', width: 120 },
    { title: '账号策略', dataIndex: 'account_strategy', width: 180 },
  ];
  const metricColumns: ColumnsType<RuleExecutionMetric> = [
    { title: '规则版本', key: 'rule', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.rule_set_name || `规则集 #${row.rule_set_id ?? '-'}`}</Typography.Text><Typography.Text type="secondary">{row.version ? `v${row.version}` : row.rule_set_version_id ? `#${row.rule_set_version_id}` : '-'}</Typography.Text></Space> },
    { title: '关联任务', dataIndex: 'task_count', width: 100 },
    { title: '执行项', dataIndex: 'action_count', width: 100 },
    { title: '成功', dataIndex: 'success_count', width: 90 },
    { title: '失败', dataIndex: 'failed_count', width: 90 },
    { title: '跳过', dataIndex: 'skipped_count', width: 90 },
    { title: '待执行', dataIndex: 'pending_count', width: 90 },
    { title: '最近命中', dataIndex: 'last_used_at', width: 180, render: (value) => formatBeijingDateTime(value) },
  ];
  const dimensionMetricColumns: ColumnsType<RuleDimensionMetric> = [
    { title: '对象', key: 'name', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.name}</Typography.Text><Typography.Text type="secondary">#{row.related_id}</Typography.Text></Space> },
    { title: '执行项', dataIndex: 'action_count', width: 90 },
    { title: '成功', dataIndex: 'success_count', width: 80 },
    { title: '失败', dataIndex: 'failed_count', width: 80 },
    { title: '跳过', dataIndex: 'skipped_count', width: 80 },
    { title: '待执行', dataIndex: 'pending_count', width: 90 },
    { title: '最近命中', dataIndex: 'last_used_at', width: 170, render: (value) => formatBeijingDateTime(value) },
  ];
  const trendColumns: ColumnsType<RuleTrendMetric> = [
    { title: '日期', dataIndex: 'date', width: 140 },
    { title: '执行项', dataIndex: 'action_count', width: 90 },
    { title: '成功', dataIndex: 'success_count', width: 80 },
    { title: '失败', dataIndex: 'failed_count', width: 80 },
    { title: '跳过', dataIndex: 'skipped_count', width: 80 },
    { title: '待执行', dataIndex: 'pending_count', width: 90 },
    { title: '成功率', key: 'success_rate', width: 100, render: (_, row) => row.action_count ? `${Math.round((row.success_count / row.action_count) * 100)}%` : '-' },
  ];
  const conversionColumns: ColumnsType<RuleConversionMetric> = [
    { title: '规则版本', key: 'rule', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.rule_set_name || `规则集 #${row.rule_set_id ?? '-'}`}</Typography.Text><Typography.Text type="secondary">{row.version ? `v${row.version}` : row.rule_set_version_id ? `#${row.rule_set_version_id}` : '-'}</Typography.Text></Space> },
    { title: '近 7 天执行', dataIndex: 'current_action_count', width: 110 },
    { title: '近 7 天成功率', key: 'current_rate', width: 130, render: (_, row) => row.current_action_count ? `${row.current_success_rate}%` : '-' },
    { title: '前 7 天执行', dataIndex: 'previous_action_count', width: 110 },
    { title: '前 7 天成功率', key: 'previous_rate', width: 130, render: (_, row) => row.previous_action_count ? `${row.previous_success_rate}%` : '-' },
    { title: '变化', key: 'delta', width: 100, render: (_, row) => row.previous_action_count || row.current_action_count ? `${row.success_rate_delta > 0 ? '+' : ''}${row.success_rate_delta}%` : '-' },
  ];
  const crossColumns: ColumnsType<RuleCrossMetric> = [
    { title: '规则版本', key: 'rule', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text strong>{row.rule_set_name || `规则集 #${row.rule_set_id ?? '-'}`}</Typography.Text><Typography.Text type="secondary">{row.version ? `v${row.version}` : row.rule_set_version_id ? `#${row.rule_set_version_id}` : '-'}</Typography.Text></Space> },
    { title: '目标群', key: 'target', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text>{row.target_name}</Typography.Text><Typography.Text type="secondary">#{row.target_group_id ?? '-'}</Typography.Text></Space> },
    { title: '发送账号', key: 'account', render: (_, row) => <Space direction="vertical" size={0}><Typography.Text>{row.account_name}</Typography.Text><Typography.Text type="secondary">#{row.account_id ?? '-'}</Typography.Text></Space> },
    { title: '执行项', dataIndex: 'action_count', width: 90 },
    { title: '成功率', key: 'rate', width: 90, render: (_, row) => row.action_count ? `${row.success_rate}%` : '-' },
    { title: '失败/跳过', key: 'bad', width: 110, render: (_, row) => `${row.failed_count}/${row.skipped_count}` },
    { title: '最近命中', dataIndex: 'last_used_at', width: 170, render: (value) => formatBeijingDateTime(value) },
  ];
  const relayMaterialColumns: ColumnsType<RelayMaterialAttribution> = [
    { title: '素材指纹', dataIndex: 'material_fingerprint', width: 150, ellipsis: true },
    { title: '素材预览', dataIndex: 'sample_text', ellipsis: true },
    { title: '源事件', dataIndex: 'source_event_count', width: 90 },
    { title: '目标/账号', key: 'target_account', width: 110, render: (_, row) => `${row.target_count}/${row.account_count}` },
    { title: '执行项', dataIndex: 'action_count', width: 90 },
    { title: '成功率', key: 'success_rate', width: 90, render: (_, row) => row.action_count ? `${row.success_rate}%` : '-' },
    { title: '失败/跳过', key: 'bad', width: 110, render: (_, row) => `${row.failed_count}/${row.skipped_count}` },
    { title: '重试', dataIndex: 'retry_count', width: 80 },
    { title: '最近使用', dataIndex: 'last_used_at', width: 170, render: (value) => formatBeijingDateTime(value) },
  ];
  const ruleTesterPanel = (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div className="form-grid">
        <label>测试模式<Select value={testMode} onChange={setTestMode} options={TEST_MODE_OPTIONS} /></label>
        <label>测试类型<Select value={testType} onChange={setTestType} options={TASK_TYPE_OPTIONS} /></label>
        <label>规则版本<Select allowClear value={testVersionId ?? undefined} onChange={(value) => setTestVersionId(value ?? null)} options={versionOptions} placeholder="选择已发布或草稿版本" /></label>
        <label>源群 ID<Input value={testSourceGroupId} onChange={(event) => setTestSourceGroupId(event.target.value)} placeholder="用于 source_group_map / routes 预览" /></label>
      </div>
      <Input.TextArea rows={4} value={sample} onChange={(event) => setSample(event.target.value)} placeholder="输入源消息、用户消息或频道评论上下文，预览规则命中情况" />
      {['group_ai_chat', 'channel_comment', 'message_send'].includes(testType) && (
        <Input.TextArea rows={4} value={candidateSample} onChange={(event) => setCandidateSample(event.target.value)} placeholder="候选输出，每行一条；用于逐条输出校验" />
      )}
      <Space className="modal-actions">
        <Button type="primary" loading={testing} onClick={runRuleTest}>测试规则</Button>
      </Space>
      <Descriptions
        className="rule-test-result"
        bordered
        size="small"
        column={2}
        items={[
          { key: 'result', label: '过滤结果', children: testResult.result },
          { key: 'mode', label: '测试模式', children: TEST_MODE_OPTIONS.find((item) => item.value === testResult.test_mode)?.label ?? testResult.test_mode },
          { key: 'test-data', label: '测试数据', children: testResult.is_test_data ? '是，不进入真实队列' : '否' },
          { key: 'hits', label: '命中规则', children: testResult.hits.map((rule) => rule.keyword).join('、') || '无' },
          { key: 'input-hits', label: '输入命中', children: testResult.input_hits.join('、') || '默认通过' },
          { key: 'block', label: '阻断原因', children: testResult.should_block ? testResult.block_reason || '命中拦截规则' : '不阻断' },
          { key: 'rule-set', label: '规则版本', children: testResult.rule_set_name ? `${testResult.rule_set_name} / #${testResult.rule_set_version_id}` : '未选择' },
          { key: 'filter-reason', label: '过滤说明', span: 2, children: testResult.filter_passed ? '通过' : testResult.filter_reason || '未通过过滤' },
          { key: 'transform', label: '转换后内容', span: 2, children: testResult.transformed_text || sample || '-' },
          { key: 'target', label: '目标路由', children: testResult.target_summary },
          { key: 'account', label: '预计账号', children: testResult.account_strategy },
          { key: 'rate', label: '限流判断', span: 2, children: testResult.rate_limit_summary },
        ]}
      />
      <Table
        className="tg-table"
        rowKey="group_id"
        size="small"
        columns={routeColumns}
        dataSource={testResult.target_routes}
        pagination={false}
        locale={{ emptyText: '选择规则版本后可预览目标群路由。' }}
      />
      <Table
        className="tg-table"
        rowKey="index"
        size="small"
        columns={[
          { title: '候选', dataIndex: 'original_text', ellipsis: true },
          { title: '结果', dataIndex: 'passed', width: 90, render: (value) => <StatusBadge status={value ? '通过' : '丢弃'} /> },
          { title: '动作', dataIndex: 'action', width: 120 },
          { title: '转换后', dataIndex: 'transformed_text', ellipsis: true },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
        ]}
        dataSource={testResult.output_candidates}
        pagination={false}
        locale={{ emptyText: 'AI 回复 / AI 评论 / 普通发送测试会展示逐条输出校验。' }}
      />
    </Space>
  );
  const ruleWorkbenchItems = [
    {
      key: 'rule-sets',
      label: '规则集',
      children: (
        <div className="rules-tab-stack">
          <Space className="toolbar-row" wrap>{ruleSetTable.searchInput}</Space>
          <Table<RuleSet>
            className="tg-table"
            rowKey="id"
            columns={ruleSetColumns}
            dataSource={ruleSetTable.filteredRows}
            pagination={ruleSetTable.pagination}
            scroll={{ x: 980 }}
            loading={loading}
          />
        </div>
      ),
    },
    {
      key: 'versions',
      label: '规则版本',
      children: (
        <div className="rules-tab-stack">
          <Table<RuleVersionRow>
            className="tg-table"
            rowKey="id"
            columns={versionColumns}
            dataSource={versionRows}
            pagination={{ pageSize: 8 }}
            scroll={{ x: 1080 }}
            loading={loading}
            locale={{ emptyText: '暂无规则版本。' }}
          />
        </div>
      ),
    },
    {
      key: 'system-rules',
      label: '规则配置',
      children: (
        <div className="rules-tab-stack">
          <Space className="toolbar-row" wrap>{table.searchInput}</Space>
          <Table<RuleRow>
            className="tg-table"
            rowKey="key"
            columns={columns}
            dataSource={table.filteredRows}
            pagination={table.pagination}
            scroll={{ x: 960 }}
            loading={loading}
          />
        </div>
      ),
    },
    {
      key: 'tester',
      label: '规则测试',
      children: ruleTesterPanel,
    },
    {
      key: 'monitor',
      label: '命中记录',
      children: (
        <div className="rules-tab-stack">
          <div className="rules-section-header">
            <Typography.Text strong>规则冲突与缺口</Typography.Text>
            <Typography.Text type="secondary">重复、未发布、绑定失效会在这里提示</Typography.Text>
          </div>
          {summary.conflicts.length ? (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {summary.conflicts.map((conflict) => (
                <Alert
                  key={conflict.key}
                  type={conflict.level === '高' ? 'error' : conflict.level === '中' ? 'warning' : 'info'}
                  showIcon
                  message={`${conflict.level} / ${conflict.title}`}
                  description={`${conflict.detail}${conflict.related_ids.length ? ` / 关联：${conflict.related_ids.join('、')}` : ''}`}
                />
              ))}
            </Space>
          ) : (
            <Typography.Text type="secondary">暂无规则冲突或绑定缺口。</Typography.Text>
          )}
          <div className="rules-section-header">
            <Typography.Text strong>规则命中统计</Typography.Text>
            <Typography.Text type="secondary">按转发监听执行项聚合规则版本命中</Typography.Text>
          </div>
          <Table<RuleExecutionMetric>
            className="tg-table"
            rowKey="key"
            columns={metricColumns}
            dataSource={summary.execution_metrics}
            pagination={{ pageSize: 6 }}
            scroll={{ x: 980 }}
            locale={{ emptyText: '暂无规则执行记录。转发监听任务生成执行项后会在这里统计。' }}
          />
        </div>
      ),
    },
    {
      key: 'drilldown',
      label: '效果钻取',
      children: (
        <Tabs
          className="rules-inner-tabs"
          items={[
            {
              key: 'target',
              label: '目标群',
              children: <Table<RuleDimensionMetric> size="small" rowKey="key" columns={dimensionMetricColumns} dataSource={summary.target_metrics} pagination={{ pageSize: 5 }} scroll={{ x: 840 }} locale={{ emptyText: '暂无目标群命中记录。' }} />,
            },
            {
              key: 'account',
              label: '发送账号',
              children: <Table<RuleDimensionMetric> size="small" rowKey="key" columns={dimensionMetricColumns} dataSource={summary.account_metrics} pagination={{ pageSize: 5 }} scroll={{ x: 840 }} locale={{ emptyText: '暂无账号命中记录。' }} />,
            },
            {
              key: 'keyword',
              label: '条件词',
              children: <Table<RuleDimensionMetric> size="small" rowKey="key" columns={dimensionMetricColumns} dataSource={summary.keyword_metrics} pagination={{ pageSize: 5 }} scroll={{ x: 840 }} locale={{ emptyText: '暂无规则条件命中记录。' }} />,
            },
            {
              key: 'trend',
              label: '长期趋势',
              children: <Table<RuleTrendMetric> className="tg-table" rowKey="date" columns={trendColumns} dataSource={summary.trend_metrics} pagination={false} scroll={{ x: 680 }} locale={{ emptyText: '暂无趋势数据。' }} />,
            },
            {
              key: 'conversion',
              label: '转化对比',
              children: <Table<RuleConversionMetric> className="tg-table" rowKey="key" columns={conversionColumns} dataSource={summary.conversion_metrics} pagination={{ pageSize: 6 }} scroll={{ x: 820 }} locale={{ emptyText: '暂无可对比的规则执行记录。' }} />,
            },
            {
              key: 'cross',
              label: '跨维归因',
              children: <Table<RuleCrossMetric> className="tg-table" rowKey="key" columns={crossColumns} dataSource={summary.cross_metrics} pagination={{ pageSize: 6 }} scroll={{ x: 980 }} locale={{ emptyText: '暂无跨维归因数据。' }} />,
            },
          ]}
        />
      ),
    },
    {
      key: 'materials',
      label: '素材归因',
      children: (
        <div className="rules-tab-stack">
          <div className="rules-section-header">
            <Typography.Text strong>素材归因报表</Typography.Text>
            <Space>
              <Typography.Text type="secondary">素材 {relayReport.total_materials} / 源事件 {relayReport.total_source_events} / 执行项 {relayReport.total_actions}</Typography.Text>
              <Button size="small" icon={<Database size={14} />} onClick={exportRelayAttribution}>导出归因 CSV</Button>
            </Space>
          </div>
          <Table<RelayMaterialAttribution>
            className="tg-table"
            rowKey="key"
            columns={relayMaterialColumns}
            dataSource={relayReport.rows}
            pagination={{ pageSize: 6 }}
            scroll={{ x: 1040 }}
            locale={{ emptyText: '暂无素材归因数据。转发监听执行后会按素材指纹聚合。' }}
          />
        </div>
      ),
    },
  ];

  return (
    <section className="view-grid">
      <Space className="stats-grid" wrap>
        <StatCard label="系统规则" value={summary.system_rule_count} detail="自动校验、路由、账号、重试" icon={<ShieldAlert size={20} />} />
        <StatCard label="规则集" value={ruleSets.length} detail="过滤、转换、路由版本化" icon={<CheckCircle2 size={20} />} />
        <StatCard label="条件词规则" value={summary.keyword_rule_count} detail="规则集条件统一管理" icon={<Database size={20} />} />
        <StatCard label="转发任务规则" value={summary.relay_task_rule_count} detail="任务绑定的过滤/转换配置" icon={<CheckCircle2 size={20} />} />
      </Space>
      <Card
        className="panel rules-workbench"
        title="规则中心"
        extra={(
          <Space wrap>
            <Button icon={<ShieldAlert size={16} />} onClick={() => setActiveTab('monitor')}>查看命中记录</Button>
            <Button onClick={() => setTesterOpen(true)}>规则测试器</Button>
            <Button type="primary" icon={<CheckCircle2 size={16} />} onClick={() => { createForm.setFieldsValue(defaultRuleFormValues()); setCreateOpen(true); }}>新建规则集</Button>
            <Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button>
          </Space>
        )}
      >
        {error && <Alert className="form-alert" type="error" showIcon message={error} />}
        <Tabs className="rules-workbench-tabs" activeKey={activeTab} onChange={setActiveTab} items={ruleWorkbenchItems} />
      </Card>
      <Modal className="tg-modal large" title="规则测试器" open={testerOpen} width={920} footer={null} onCancel={() => setTesterOpen(false)} destroyOnHidden centered>
        {ruleTesterPanel}
      </Modal>
      <Modal className="tg-modal large" title={versionListTarget ? `版本列表：${versionListTarget.name}` : '版本列表'} open={Boolean(versionListTarget)} width={980} footer={null} onCancel={() => setVersionListTarget(null)} destroyOnHidden centered>
        <Table<RuleVersionRow>
          className="tg-table"
          rowKey="id"
          columns={versionColumns}
          dataSource={versionListTarget?.versions.map((version) => ({ ...version, rule_set_name: versionListTarget.name, ruleSet: versionListTarget })) ?? []}
          pagination={false}
          scroll={{ x: 980 }}
        />
      </Modal>
      <Modal className="tg-modal large" title={boundTaskTarget ? `绑定任务：${boundTaskTarget.name}` : '绑定任务'} open={Boolean(boundTaskTarget)} width={980} footer={null} onCancel={() => { setBoundTaskTarget(null); setBoundTasks([]); }} destroyOnHidden centered>
        <Table<RuleSetBoundTask>
          className="tg-table"
          rowKey="id"
          columns={[
            { title: '任务', dataIndex: 'name', ellipsis: true },
            { title: '类型', dataIndex: 'type', width: 140, render: (value) => taskTypeLabels([value]).join('') || value },
            { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
            { title: '绑定方式', dataIndex: 'binding_mode', width: 140, render: (value) => value === 'fixed_version' ? '固定版本' : '跟随当前发布' },
            { title: '绑定版本', dataIndex: 'rule_set_version_id', width: 110, render: (value) => value ? `#${value}` : '当前发布' },
            { title: '执行解析版本', dataIndex: 'resolved_rule_set_version_id', width: 130, render: (value) => value ? `#${value}` : '-' },
            { title: '更新时间', dataIndex: 'updated_at', width: 180, render: (value) => formatBeijingDateTime(value) },
          ]}
          dataSource={boundTasks}
          pagination={{ pageSize: 8 }}
          scroll={{ x: 940 }}
          locale={{ emptyText: '暂无任务绑定该规则集。' }}
        />
      </Modal>
      <Modal className="tg-modal large" title={detailRule?.name ?? '规则详情'} open={Boolean(detailRule)} width={760} footer={null} onCancel={() => setDetailRule(null)} destroyOnHidden centered>
        {detailRule && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                { key: 'category', label: '规则类别', children: detailRule.category },
                { key: 'status', label: '状态', children: <StatusBadge status={detailRule.status} /> },
                { key: 'version', label: '版本', children: detailRule.version },
                { key: 'source', label: '来源', children: detailRule.source || '-' },
                { key: 'detail', label: '处理口径', span: 2, children: detailRule.detail || '-' },
              ]}
            />
            <Input.TextArea rows={10} value={formatJson(detailRule.metadata ?? {})} readOnly />
          </Space>
        )}
      </Modal>
      <Modal className="tg-modal large" title="新建规则集" open={createOpen} width={840} confirmLoading={saving} okText="创建并发布 v1" cancelText="取消" onOk={createRuleSet} onCancel={() => setCreateOpen(false)} destroyOnHidden centered>
        <RuleSetForm form={createForm} includeBasics groupTargets={groupTargets} />
      </Modal>
      <Modal className="tg-modal large" title={versionTarget ? `新建版本：${versionTarget.name}` : '新建版本'} open={Boolean(versionTarget)} width={840} confirmLoading={saving} okText="保存未发布版本" cancelText="取消" onOk={createRuleSetVersion} onCancel={() => setVersionTarget(null)} destroyOnHidden centered>
        <RuleSetForm form={versionForm} groupTargets={groupTargets} />
      </Modal>
    </section>
  );
}

function defaultRuleJson() {
  return {
    filters: '{}',
    output_checks: '{"failure_strategy":"transform_once_drop"}',
    transforms: '{}',
    routing: '{}',
    account_strategy: '{"mode":"target_sticky"}',
    rate_limits: '{}',
    retry_policy: '{"max_retries":3}',
  };
}

const TASK_TYPE_OPTIONS = [
  { value: 'group_relay', label: '监听转发' },
  { value: 'group_ai_chat', label: 'AI 回复' },
  { value: 'channel_comment', label: 'AI 评论' },
  { value: 'message_send', label: '普通消息发送' },
];

const TEST_MODE_OPTIONS = [
  { value: 'rules_only', label: '仅规则测试' },
  { value: 'ai_dry_run', label: 'AI 干跑测试' },
  { value: 'history_replay', label: '历史样本回放' },
];

const OUTPUT_FAILURE_OPTIONS = [
  { value: 'transform_once_drop', label: '转换一次，仍失败则丢弃' },
  { value: 'drop', label: '直接丢弃' },
  { value: 'rewrite_once', label: '重新生成一次' },
  { value: 'fixed_reply', label: '固定回复' },
];

function taskTypeLabels(values: string[] | undefined) {
  const labels = new Map(TASK_TYPE_OPTIONS.map((item) => [item.value, item.label]));
  return (values ?? []).map((value) => labels.get(value) ?? value);
}

function defaultRuleFormValues() {
  return {
    ...defaultRuleJson(),
    visual_keyword_whitelist: '',
    visual_keyword_blacklist: '',
    visual_min_message_length: null,
    visual_max_message_length: null,
    visual_allowed_media_types: '',
    visual_blocked_user_ids: '',
    visual_message_type_filter: 'all',
    visual_expression_mode: 'all',
    visual_expression_conditions: '',
    visual_prefix: '',
    visual_suffix: '',
    visual_remove_mentions: false,
    visual_remove_links: false,
    visual_default_operation_target_ids: [],
    visual_default_target_group_ids: '',
    visual_source_group_map: '',
    visual_keyword_routes: '',
    visual_routes: '',
    visual_account_mode: 'target_sticky',
    visual_fixed_account_id: null,
    visual_account_weights: '',
    visual_per_target_per_hour: null,
    visual_cooldown_seconds: null,
    visual_max_retries: 3,
    task_types: ['group_relay'],
    input_failure: 'skip',
    output_failure: 'transform_once_drop',
    version_binding: 'follow_current',
    visual_forbidden_keywords: '',
    visual_forbid_links: false,
    visual_forbid_mentions: true,
    visual_forbid_contacts: false,
    visual_output_min_length: null,
    visual_output_max_length: null,
    visual_output_failure_strategy: 'transform_once_drop',
  };
}

function ruleFormValuesFromVersion(ruleSet: RuleSet, groupTargets: OperationTarget[] = []) {
  const version = ruleSet.versions.find((item) => item.id === ruleSet.active_version_id) ?? ruleSet.versions[0];
  if (!version) return defaultRuleFormValues();
  const filters = version.filters ?? {};
  const transforms = version.transforms ?? {};
  const outputChecks = version.output_checks ?? {};
  const routing = version.routing ?? {};
  const defaultGroupIds = numberList(routing.target_group_ids ?? routing.default_target_group_ids);
  const mappedTargetIds = operationTargetIdsForGroupIds(defaultGroupIds, groupTargets);
  const unmappedGroupIds = defaultGroupIds.filter((id) => !groupTargets.some((target) => target.linked_group_id === id));
  const accountStrategy = version.account_strategy ?? {};
  const rateLimits = version.rate_limits ?? {};
  const retryPolicy = version.retry_policy ?? {};
  return {
    filters: formatJson(filters),
    output_checks: formatJson(outputChecks),
    transforms: formatJson(transforms),
    routing: formatJson(routing),
    account_strategy: formatJson(accountStrategy),
    rate_limits: formatJson(rateLimits),
    retry_policy: formatJson(retryPolicy),
    visual_keyword_whitelist: (filters.keyword_whitelist ?? []).join(','),
    visual_keyword_blacklist: (filters.keyword_blacklist ?? []).join(','),
    visual_min_message_length: filters.min_message_length ?? null,
    visual_max_message_length: filters.max_message_length ?? null,
    visual_allowed_media_types: (filters.allowed_media_types ?? []).join(','),
    visual_blocked_user_ids: (filters.blocked_user_ids ?? []).join(','),
    visual_message_type_filter: filters.only_with_media ? 'media' : filters.only_text ? 'text' : 'all',
    visual_expression_mode: filters.expression?.mode ?? filters.expression?.logic ?? 'all',
    visual_expression_conditions: formatFilterExpression(filters.expression),
    visual_prefix: transforms.prefix ?? '',
    visual_suffix: transforms.suffix ?? '',
    visual_remove_mentions: Boolean(transforms.remove_mentions),
    visual_remove_links: Boolean(transforms.remove_links),
    visual_default_operation_target_ids: mappedTargetIds,
    visual_default_target_group_ids: unmappedGroupIds.join(','),
    visual_source_group_map: formatSourceGroupMap(routing.source_group_map ?? routing.source_to_targets),
    visual_keyword_routes: formatKeywordRoutes(routing.keyword_routes),
    visual_routes: formatRoutes(routing.routes),
    visual_account_mode: accountStrategy.mode ?? 'target_sticky',
    visual_fixed_account_id: accountStrategy.account_id ?? accountStrategy.fixed_account_id ?? null,
    visual_account_weights: formatAccountWeights(accountStrategy.weights),
    visual_per_target_per_hour: rateLimits.per_target_per_hour ?? null,
    visual_cooldown_seconds: rateLimits.cooldown_seconds ?? null,
    visual_max_retries: retryPolicy.max_retries ?? 3,
    visual_forbidden_keywords: (outputChecks.forbidden_keywords ?? outputChecks.blocked_keywords ?? []).join(','),
    visual_forbid_links: Boolean(outputChecks.forbid_links ?? outputChecks.no_links),
    visual_forbid_mentions: Boolean(outputChecks.forbid_mentions ?? outputChecks.no_mentions),
    visual_forbid_contacts: Boolean(outputChecks.forbid_contacts ?? outputChecks.no_contacts),
    visual_output_min_length: outputChecks.min_length ?? null,
    visual_output_max_length: outputChecks.max_length ?? null,
    visual_output_failure_strategy: outputChecks.failure_strategy ?? outputChecks.on_failure ?? 'transform_once_drop',
  };
}

function RuleSetForm({ form, includeBasics = false, groupTargets = [] }: { form: ReturnType<typeof Form.useForm>[0]; includeBasics?: boolean; groupTargets?: OperationTarget[] }) {
  const operationTargetOptions = groupTargets.map((target) => ({
    value: target.id,
    label: `${target.title} / 目标#${target.id} / 群#${target.linked_group_id}`,
  }));

  function applyVisualTemplate() {
    const values = form.getFieldsValue() as Record<string, any>;
    const filters = readJsonObject(values.filters);
    const outputChecks = readJsonObject(values.output_checks);
    const transforms = readJsonObject(values.transforms);
    const routing = readJsonObject(values.routing);
    const accountStrategy = readJsonObject(values.account_strategy);
    const rateLimits = readJsonObject(values.rate_limits);
    const retryPolicy = readJsonObject(values.retry_policy);

    filters.keyword_whitelist = words(values.visual_keyword_whitelist);
    filters.keyword_blacklist = words(values.visual_keyword_blacklist);
    setOptionalNumber(filters, 'min_message_length', values.visual_min_message_length);
    setOptionalNumber(filters, 'max_message_length', values.visual_max_message_length);
    setOptionalList(filters, 'allowed_media_types', words(values.visual_allowed_media_types));
    setOptionalList(filters, 'blocked_user_ids', words(values.visual_blocked_user_ids));
    filters.only_with_media = values.visual_message_type_filter === 'media';
    filters.only_text = values.visual_message_type_filter === 'text';
    const expression = parseFilterExpression(values.visual_expression_mode, values.visual_expression_conditions);
    if (expression.conditions.length) {
      filters.expression = expression;
    } else {
      delete filters.expression;
    }
    transforms.prefix = values.visual_prefix || '';
    transforms.suffix = values.visual_suffix || '';
    transforms.remove_mentions = Boolean(values.visual_remove_mentions);
    transforms.remove_links = Boolean(values.visual_remove_links);
    outputChecks.forbidden_keywords = words(values.visual_forbidden_keywords);
    outputChecks.forbid_links = Boolean(values.visual_forbid_links);
    outputChecks.forbid_mentions = Boolean(values.visual_forbid_mentions);
    outputChecks.forbid_contacts = Boolean(values.visual_forbid_contacts);
    outputChecks.failure_strategy = values.visual_output_failure_strategy || 'transform_once_drop';
    setOptionalNumber(outputChecks, 'min_length', values.visual_output_min_length);
    setOptionalNumber(outputChecks, 'max_length', values.visual_output_max_length);
    routing.target_group_ids = uniqueNumbers([
      ...groupIdsForOperationTargetIds(values.visual_default_operation_target_ids, groupTargets),
      ...numberList(values.visual_default_target_group_ids),
    ]);
    const sourceGroupMap = parseSourceGroupMap(values.visual_source_group_map);
    if (Object.keys(sourceGroupMap).length) {
      routing.source_group_map = sourceGroupMap;
    } else {
      delete routing.source_group_map;
    }
    const keywordRoutes = parseKeywordRoutes(values.visual_keyword_routes);
    if (keywordRoutes.length) {
      routing.keyword_routes = keywordRoutes;
    } else {
      delete routing.keyword_routes;
    }
    const routes = parseRoutes(values.visual_routes);
    if (routes.length) {
      routing.routes = routes;
    } else {
      delete routing.routes;
    }
    accountStrategy.mode = values.visual_account_mode || 'target_sticky';
    if (values.visual_fixed_account_id) {
      accountStrategy.account_id = values.visual_fixed_account_id;
    } else {
      delete accountStrategy.account_id;
      delete accountStrategy.fixed_account_id;
    }
    const accountWeights = parseAccountWeights(values.visual_account_weights);
    if (Object.keys(accountWeights).length) {
      accountStrategy.weights = accountWeights;
    } else {
      delete accountStrategy.weights;
    }
    if (values.visual_per_target_per_hour) {
      rateLimits.per_target_per_hour = values.visual_per_target_per_hour;
    } else {
      delete rateLimits.per_target_per_hour;
    }
    if (values.visual_cooldown_seconds) {
      rateLimits.cooldown_seconds = values.visual_cooldown_seconds;
    } else {
      delete rateLimits.cooldown_seconds;
    }
    retryPolicy.max_retries = values.visual_max_retries ?? 3;

    form.setFieldsValue({
      filters: formatJson(filters),
      output_checks: formatJson(outputChecks),
      transforms: formatJson(transforms),
      routing: formatJson(routing),
      account_strategy: formatJson(accountStrategy),
      rate_limits: formatJson(rateLimits),
      retry_policy: formatJson(retryPolicy),
    });
  }

  return (
    <Form form={form} layout="vertical" initialValues={defaultRuleFormValues()}>
      {includeBasics && (
        <div className="form-grid">
          <Form.Item name="name" label="规则集名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input /></Form.Item>
          <Form.Item name="task_types" label="适用任务类型" rules={[{ required: true }]}><Select mode="multiple" options={TASK_TYPE_OPTIONS} /></Form.Item>
          <Form.Item name="input_failure" label="输入失败处理"><Select options={[{ value: 'skip', label: '跳过' }, { value: 'block', label: '拦截' }, { value: 'mark', label: '标记风险' }]} /></Form.Item>
          <Form.Item name="output_failure" label="输出失败处理"><Select options={OUTPUT_FAILURE_OPTIONS} /></Form.Item>
          <Form.Item name="version_binding" label="任务绑定方式"><Select options={[{ value: 'fixed_version', label: '固定版本' }, { value: 'follow_current', label: '跟随当前发布版本' }]} /></Form.Item>
        </div>
      )}
      <Card size="small" title="常用规则模板" extra={<Button size="small" onClick={applyVisualTemplate}>生成 JSON</Button>}>
        <div className="form-grid">
          <Form.Item name="visual_keyword_whitelist" label="白名单关键词"><Input placeholder="公告, 活动" /></Form.Item>
          <Form.Item name="visual_keyword_blacklist" label="黑名单关键词"><Input placeholder="广告, 禁止" /></Form.Item>
          <Form.Item name="visual_min_message_length" label="最小长度"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_max_message_length" label="最大长度"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_allowed_media_types" label="允许媒体类型"><Input placeholder="text, photo, video" /></Form.Item>
          <Form.Item name="visual_blocked_user_ids" label="屏蔽用户 ID"><Input placeholder="12345, 67890" /></Form.Item>
          <Form.Item name="visual_message_type_filter" label="消息类型"><Select options={[{ value: 'all', label: '不限' }, { value: 'text', label: '仅文本' }, { value: 'media', label: '仅媒体' }]} /></Form.Item>
          <Form.Item name="visual_expression_mode" label="组合条件模式"><Select options={[{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }]} /></Form.Item>
          <Form.Item name="visual_expression_conditions" label="组合条件">
            <Input.TextArea rows={3} placeholder="content contains 公告,活动&#10;content not_contains 禁止&#10;length gte 10" />
          </Form.Item>
          <Form.Item name="visual_prefix" label="转发前缀"><Input /></Form.Item>
          <Form.Item name="visual_suffix" label="转发后缀"><Input /></Form.Item>
          <Form.Item name="visual_remove_mentions" label="@ 提及"><Select options={[{ value: false, label: '保留' }, { value: true, label: '移除' }]} /></Form.Item>
          <Form.Item name="visual_remove_links" label="链接"><Select options={[{ value: false, label: '保留' }, { value: true, label: '移除' }]} /></Form.Item>
          <Form.Item name="visual_forbidden_keywords" label="输出禁止关键词"><Input placeholder="引流, 联系方式" /></Form.Item>
          <Form.Item name="visual_forbid_links" label="输出链接"><Select options={[{ value: false, label: '允许' }, { value: true, label: '禁止' }]} /></Form.Item>
          <Form.Item name="visual_forbid_mentions" label="输出 @"><Select options={[{ value: true, label: '禁止' }, { value: false, label: '允许' }]} /></Form.Item>
          <Form.Item name="visual_forbid_contacts" label="联系方式"><Select options={[{ value: false, label: '允许' }, { value: true, label: '禁止' }]} /></Form.Item>
          <Form.Item name="visual_output_min_length" label="输出最小长度"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_output_max_length" label="输出最大长度"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_output_failure_strategy" label="输出失败策略"><Select options={OUTPUT_FAILURE_OPTIONS} /></Form.Item>
          <Form.Item name="visual_default_operation_target_ids" label="默认运营目标">
            <Select mode="multiple" allowClear placeholder="选择转发目标" options={operationTargetOptions} />
          </Form.Item>
          <Form.Item name="visual_default_target_group_ids" label="兼容目标群 ID"><Input placeholder="仅旧数据或未建运营目标时填写，如 9, 10" /></Form.Item>
          <Form.Item name="visual_source_group_map" label="源群映射">
            <Input.TextArea rows={2} placeholder="7 -> 9,10&#10;8 -> 11" />
          </Form.Item>
          <Form.Item name="visual_keyword_routes" label="关键词路由">
            <Input.TextArea rows={2} placeholder="公告 -> 9,10&#10;活动,报名 -> 11" />
          </Form.Item>
          <Form.Item name="visual_routes" label="源群+关键词路由">
            <Input.TextArea rows={2} placeholder="7 | 公告,活动 -> 9,10&#10;8 | 报名 -> 11" />
          </Form.Item>
          <Form.Item name="visual_account_mode" label="账号策略"><Select options={[{ value: 'target_sticky', label: '目标群粘性' }, { value: 'source_target_sticky', label: '源群+目标群粘性' }, { value: 'round_robin', label: '轮询' }, { value: 'random', label: '随机' }, { value: 'weighted_random', label: '权重随机' }, { value: 'fixed', label: '固定账号' }]} /></Form.Item>
          <Form.Item name="visual_fixed_account_id" label="固定账号 ID"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_account_weights" label="账号权重">
            <Input.TextArea rows={2} placeholder="101=5&#10;102=1" />
          </Form.Item>
          <Form.Item name="visual_per_target_per_hour" label="每目标每小时"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_cooldown_seconds" label="冷却秒数"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_max_retries" label="最大重试"><InputNumber min={0} max={10} style={{ width: '100%' }} /></Form.Item>
        </div>
      </Card>
      <div className="form-grid">
        <Form.Item name="filters" label="过滤规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="output_checks" label="输出校验 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="transforms" label="转换规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="routing" label="路由规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="account_strategy" label="账号策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="rate_limits" label="限速策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="retry_policy" label="重试策略 JSON"><Input.TextArea rows={4} /></Form.Item>
      </div>
    </Form>
  );
}

function readJsonObject(raw: string): Record<string, any> {
  try {
    const parsed = JSON.parse((raw || '').trim() || '{}');
    return parsed && !Array.isArray(parsed) && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function formatJson(value: Record<string, any>) {
  return JSON.stringify(value, null, 2);
}

function setOptionalNumber(target: Record<string, any>, key: string, value: unknown) {
  const number = Number(value);
  if (Number.isFinite(number) && number >= 0) {
    target[key] = number;
  } else {
    delete target[key];
  }
}

function setOptionalList(target: Record<string, any>, key: string, value: string[]) {
  if (value.length) {
    target[key] = value;
  } else {
    delete target[key];
  }
}

function words(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return String(value ?? '').split(/[,，\n\s]+/).map((item) => item.trim()).filter(Boolean);
}

function numberList(value: unknown): number[] {
  return words(value).map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0);
}

function uniqueNumbers(values: number[]): number[] {
  return Array.from(new Set(values.filter((item) => Number.isFinite(item) && item > 0)));
}

function groupIdsForOperationTargetIds(value: unknown, groupTargets: OperationTarget[]): number[] {
  const ids = numberList(value);
  return uniqueNumbers(ids.flatMap((id) => {
    const target = groupTargets.find((item) => item.id === id);
    return target?.linked_group_id ? [target.linked_group_id] : [];
  }));
}

function operationTargetIdsForGroupIds(groupIds: number[], groupTargets: OperationTarget[]): number[] {
  return uniqueNumbers(groupIds.flatMap((groupId) => {
    const target = groupTargets.find((item) => item.linked_group_id === groupId);
    return target ? [target.id] : [];
  }));
}

function parseArrowLine(line: string): [string, string] | null {
  const [left, ...rightParts] = line.split(/->|=>|→/);
  const right = rightParts.join('->');
  if (!left?.trim() || !right?.trim()) return null;
  return [left.trim(), right.trim()];
}

function parseSourceGroupMap(value: unknown): Record<string, number[]> {
  const map: Record<string, number[]> = {};
  String(value ?? '').split(/\n+/).forEach((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return;
    const sourceIds = numberList(parsed[0]);
    const targetIds = numberList(parsed[1]);
    sourceIds.forEach((sourceId) => {
      if (targetIds.length) map[String(sourceId)] = targetIds;
    });
  });
  return map;
}

function parseKeywordRoutes(value: unknown): Array<{ keywords: string[]; target_group_ids: number[] }> {
  return String(value ?? '').split(/\n+/).flatMap((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return [];
    const keywords = words(parsed[0]);
    const targetGroupIds = numberList(parsed[1]);
    return keywords.length && targetGroupIds.length ? [{ keywords, target_group_ids: targetGroupIds }] : [];
  });
}

function parseRoutes(value: unknown): Array<{ source_group_ids: number[]; keywords: string[]; target_group_ids: number[] }> {
  return String(value ?? '').split(/\n+/).flatMap((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return [];
    const [sourceRaw, keywordRaw = ''] = parsed[0].split('|');
    const sourceGroupIds = numberList(sourceRaw);
    const keywords = words(keywordRaw);
    const targetGroupIds = numberList(parsed[1]);
    return sourceGroupIds.length && targetGroupIds.length ? [{ source_group_ids: sourceGroupIds, keywords, target_group_ids: targetGroupIds }] : [];
  });
}

function parseFilterExpression(mode: unknown, value: unknown): { mode: string; conditions: Array<{ field: string; operator: string; value: string | string[] | number }> } {
  const conditions = String(value ?? '').split(/\n+/).flatMap((line) => {
    const cleaned = line.trim();
    if (!cleaned) return [];
    const match = cleaned.match(/^(\S+)\s+(\S+)\s+(.+)$/);
    if (!match) return [];
    const [, rawField, rawOperator, rawValue] = match;
    const field = normalizeExpressionField(rawField);
    const operator = normalizeExpressionOperator(rawOperator);
    const valueText = rawValue.trim();
    const parsedValue = field === 'length' ? Number(valueText) : words(valueText);
    if (field === 'length' && !Number.isFinite(parsedValue as number)) return [];
    return [{ field, operator, value: parsedValue }];
  });
  return { mode: String(mode || 'all'), conditions };
}

function normalizeExpressionField(value: string): string {
  const field = value.trim().toLowerCase();
  const map: Record<string, string> = {
    文本: 'content',
    内容: 'content',
    content: 'content',
    text: 'content',
    sender: 'sender_id',
    sender_id: 'sender_id',
    发送者: 'sender_id',
    type: 'message_type',
    message_type: 'message_type',
    类型: 'message_type',
    length: 'length',
    长度: 'length',
  };
  return map[field] || field;
}

function normalizeExpressionOperator(value: string): string {
  const operator = value.trim().toLowerCase();
  const map: Record<string, string> = {
    包含: 'contains',
    不包含: 'not_contains',
    等于: 'eq',
    不等于: 'neq',
    属于: 'in',
    不属于: 'not_in',
    至少: 'gte',
    至多: 'lte',
  };
  return map[operator] || operator;
}

function formatFilterExpression(expression: unknown): string {
  if (!expression || Array.isArray(expression) || typeof expression !== 'object') return '';
  const conditions = (expression as Record<string, any>).conditions;
  if (!Array.isArray(conditions)) return '';
  return conditions.map((condition) => {
    if (!condition || typeof condition !== 'object') return '';
    const value = Array.isArray(condition.value) ? condition.value.join(',') : String(condition.value ?? '');
    return `${condition.field || 'content'} ${condition.operator || 'contains'} ${value}`.trim();
  }).filter(Boolean).join('\n');
}

function parseAccountWeights(value: unknown): Record<string, number> {
  const weights: Record<string, number> = {};
  String(value ?? '').split(/\n+/).forEach((line) => {
    const [accountRaw, weightRaw] = line.split(/=|:|：/);
    const accountId = Number(accountRaw?.trim());
    const weight = Number(weightRaw?.trim());
    if (Number.isFinite(accountId) && accountId > 0 && Number.isFinite(weight) && weight > 0) {
      weights[String(accountId)] = Math.round(weight);
    }
  });
  return weights;
}

function formatSourceGroupMap(value: unknown): string {
  if (!value || Array.isArray(value) || typeof value !== 'object') return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([sourceId, targetIds]) => `${sourceId} -> ${numberList(targetIds).join(',')}`)
    .filter((line) => !line.endsWith('-> '))
    .join('\n');
}

function formatKeywordRoutes(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value.map((route) => {
    const keywords = words(route?.keywords ?? route?.keyword).join(',');
    const targetIds = numberList(route?.target_group_ids ?? route?.targets).join(',');
    return keywords && targetIds ? `${keywords} -> ${targetIds}` : '';
  }).filter(Boolean).join('\n');
}

function formatRoutes(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value.map((route) => {
    const sourceIds = numberList(route?.source_group_ids ?? route?.source_groups).join(',');
    const keywords = words(route?.keywords ?? route?.keyword).join(',');
    const targetIds = numberList(route?.target_group_ids ?? route?.targets).join(',');
    return sourceIds && targetIds ? `${sourceIds} | ${keywords} -> ${targetIds}` : '';
  }).filter(Boolean).join('\n');
}

function formatAccountWeights(value: unknown): string {
  if (!value || Array.isArray(value) || typeof value !== 'object') return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([accountId, weight]) => `${accountId}=${Number(weight)}`)
    .filter((line) => !line.endsWith('=NaN'))
    .join('\n');
}
