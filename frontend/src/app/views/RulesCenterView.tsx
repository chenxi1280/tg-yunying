import React from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircle2, Database, RefreshCcw, ShieldAlert } from 'lucide-react';
import { API_BASE, api } from '../../shared/api/client';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';
import type { OperationTarget, RuleSet, RuleSetBoundTask } from '../types';
import { formatBeijingDateTime } from '../time';
import {
  MEDIA_SIMULATION_OPTIONS,
  RuleSetForm,
  TASK_TYPE_OPTIONS,
  TEST_MODE_OPTIONS,
  composeRuleConfig,
  defaultRuleFormValues,
  formatJson,
  preferredRuleSet,
  preferredVersion,
  ruleFormValuesFromVersion,
  taskTypeLabels,
} from './RulesCenterConfig';

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
  simulation_scenario: string;
  simulation_steps: Array<{ step: string; status: string; action: string; reason: string }>;
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
  material_candidate_count: number;
  material_selected_id: number | null;
  material_action: string;
  material_failure_reason: string;
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
  const [simulationScenario, setSimulationScenario] = React.useState('');
  const [sample, setSample] = React.useState('');
  const [candidateSample, setCandidateSample] = React.useState('');
  const [testVersionId, setTestVersionId] = React.useState<number | null>(null);
  const [testSourceGroupId, setTestSourceGroupId] = React.useState('');
  const [testResult, setTestResult] = React.useState<RuleTestResult>({
    result: '未测试',
    test_mode: 'rules_only',
    is_test_data: true,
    simulation_scenario: '',
    simulation_steps: [],
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
    material_candidate_count: 0,
    material_selected_id: null,
    material_action: '',
    material_failure_reason: '',
    target_summary: '按绑定任务/目标路由',
    target_routes: [],
    account_strategy: '按任务账号策略选择',
    rate_limit_summary: '执行时按账号冷却、小时/日上限校验',
  });
  const [loading, setLoading] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [configTarget, setConfigTarget] = React.useState<RuleSet | null>(null);
  const [versionListTarget, setVersionListTarget] = React.useState<RuleSet | null>(null);
  const [boundTaskTarget, setBoundTaskTarget] = React.useState<RuleSet | null>(null);
  const [boundTasks, setBoundTasks] = React.useState<RuleSetBoundTask[]>([]);
  const [detailRule, setDetailRule] = React.useState<RuleRow | null>(null);
  const [error, setError] = React.useState('');
  const [createForm] = Form.useForm();
  const [configForm] = Form.useForm();

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

  React.useEffect(() => {
    if (testVersionId || !ruleSets.length) return;
    const ruleSet = preferredRuleSet(ruleSets);
    const version = preferredVersion(ruleSet);
    if (!version) return;
    setTestVersionId(version.id);
    setTestType(ruleSet.task_types?.[0] ?? 'group_relay');
  }, [ruleSets, testVersionId]);

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
          simulation_scenario: simulationScenario,
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

  function ruleConfig(values: Record<string, any>) {
    const config = composeRuleConfig(values, groupTargets);
    return {
      ...config,
      version_note: values.version_note || '配置编辑自动生成',
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

  async function saveRuleSetConfig() {
    if (!configTarget) return;
    setSaving(true);
    setError('');
    try {
      const values = await configForm.validateFields();
      await api<RuleSet>(`/rule-sets/${configTarget.id}/config`, { method: 'PUT', body: JSON.stringify(ruleConfig(values)) });
      setConfigTarget(null);
      configForm.resetFields();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  function openConfigEditor(ruleSet: RuleSet, version?: RuleVersionRow | RuleSet['versions'][number]) {
    setConfigTarget(ruleSet);
    configForm.setFieldsValue({
      ...ruleFormValuesFromVersion(ruleSet, groupTargets, version),
      version_note: version ? `基于 v${version.version} 编辑` : '配置编辑自动生成',
    });
  }

  function openRuleTester(ruleSet: RuleSet) {
    const version = preferredVersion(ruleSet);
    if (version) {
      setTestVersionId(version.id);
    }
    setTestType(ruleSet.task_types?.[0] ?? 'group_relay');
    setActiveTab('tester');
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
      width: 330,
      render: (_, row) => (
        <Space size={8}>
          <Button size="small" type="primary" icon={<CheckCircle2 size={14} />} onClick={() => openConfigEditor(row)}>编辑配置</Button>
          <Button size="small" onClick={() => openRuleTester(row)}>测试</Button>
          <Button size="small" onClick={() => setVersionListTarget(row)}>版本记录</Button>
          <Button size="small" onClick={() => openBoundTasks(row)}>绑定任务</Button>
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
          <Button size="small" onClick={() => openConfigEditor(version.ruleSet, version)}>{version.status === 'published' ? '编辑配置' : '基于此版编辑'}</Button>
          {version.status === 'published' ? <Typography.Text type="secondary">当前发布</Typography.Text> : <Button size="small" loading={saving} onClick={() => publishRuleSetVersion(version.ruleSet, version.id)}>发布</Button>}
          {version.status === 'archived' && <Button size="small" danger loading={saving} onClick={() => rollbackRuleSetVersion(version.ruleSet, version.id)}>回滚到此版本</Button>}
        </Space>
      ),
    },
  ];
  const versionOptions = ruleSets.flatMap((ruleSet) => ruleSet.versions.map((version) => ({
    value: version.id,
    label: `${ruleSet.name} / v${version.version} / ${version.status === 'published' ? '当前发布' : version.status}`,
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
        <label>媒体场景<Select value={simulationScenario} onChange={setSimulationScenario} options={MEDIA_SIMULATION_OPTIONS} /></label>
        <label>规则版本<Select allowClear value={testVersionId ?? undefined} onChange={(value) => setTestVersionId(value ?? null)} options={versionOptions} placeholder="默认使用当前发布版本" /></label>
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
          { key: 'material', label: '素材动作', children: testResult.material_selected_id ? `#${testResult.material_selected_id} / ${testResult.material_action}` : testResult.material_failure_reason || `候选 ${testResult.material_candidate_count}` },
          { key: 'target', label: '目标路由', children: testResult.target_summary },
          { key: 'account', label: '预计账号', children: testResult.account_strategy },
          { key: 'rate', label: '限流判断', span: 2, children: testResult.rate_limit_summary },
        ]}
      />
      <Table
        className="tg-table"
        rowKey="step"
        size="small"
        columns={[
          { title: '媒体步骤', dataIndex: 'step', width: 150 },
          { title: '状态', dataIndex: 'status', width: 180 },
          { title: '动作', dataIndex: 'action', width: 220 },
          { title: '原因', dataIndex: 'reason' },
        ]}
        dataSource={testResult.simulation_steps}
        pagination={false}
        locale={{ emptyText: '选择媒体场景后可模拟待缓存、迟到事件、相册失败和队列超量。' }}
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
      key: 'system-rules',
      label: '规则配置',
      children: (
        <div className="rules-tab-stack">
          <Table<RuleSet>
            className="tg-table"
            rowKey="id"
            columns={ruleSetColumns}
            dataSource={ruleSets}
            pagination={{ pageSize: 8 }}
            scroll={{ x: 980 }}
            loading={loading}
            locale={{ emptyText: '默认运营规则集正在初始化。' }}
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
        <StatCard label="发布版本" value={ruleSets.filter((ruleSet) => ruleSet.versions.some((version) => version.id === ruleSet.active_version_id && version.status === 'published')).length} detail="当前生效的规则配置" icon={<Database size={20} />} />
        <StatCard label="转发任务规则" value={summary.relay_task_rule_count} detail="任务绑定的过滤/转换配置" icon={<CheckCircle2 size={20} />} />
      </Space>
      <Card
        className="panel rules-workbench"
        title="规则中心"
        extra={(
          <Space wrap>
            <Button icon={<ShieldAlert size={16} />} onClick={() => setActiveTab('monitor')}>查看命中记录</Button>
            <Button onClick={() => setActiveTab('tester')}>规则测试器</Button>
            <Button type="primary" icon={<CheckCircle2 size={16} />} onClick={() => { createForm.setFieldsValue(defaultRuleFormValues()); setCreateOpen(true); }}>新建规则集</Button>
            <Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button>
          </Space>
        )}
      >
        {error && <Alert className="form-alert" type="error" showIcon message={error} />}
        <Tabs className="rules-workbench-tabs" activeKey={activeTab} onChange={setActiveTab} items={ruleWorkbenchItems} />
      </Card>
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
      <Modal className="tg-modal large" title={configTarget ? `编辑规则配置：${configTarget.name}` : '编辑规则配置'} open={Boolean(configTarget)} width={840} confirmLoading={saving} okText="保存并发布新版本" cancelText="取消" onOk={saveRuleSetConfig} onCancel={() => setConfigTarget(null)} destroyOnHidden centered>
        <RuleSetForm form={configForm} groupTargets={groupTargets} />
      </Modal>
    </section>
  );
}
