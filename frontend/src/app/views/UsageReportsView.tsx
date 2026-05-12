import React from 'react';
import { Activity, Bot, CheckCircle2, Database, RefreshCcw } from 'lucide-react';
import { Alert, Button, Card, Drawer, Input, InputNumber, List, Select, Space, Switch, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { ContentKeywordRule, CurrentUser, MetricBucket, OperationMetricDetail, OperationMetricsSummary, SchedulingSetting, UsageLedger, UsageSummary } from '../types';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';
import { formatBeijingDateTime } from '../time';

interface Props {
  usageLedgers: UsageLedger[];
  usageSummary: UsageSummary | null;
  currentUser?: CurrentUser | null;
}

export default function UsageReportsView({ usageLedgers, usageSummary, currentUser }: Props) {
  const [metrics, setMetrics] = React.useState<OperationMetricsSummary | null>(null);
  const [loadingMetrics, setLoadingMetrics] = React.useState(false);
  const [metricsError, setMetricsError] = React.useState('');
  const [policyOpen, setPolicyOpen] = React.useState(false);
  const [policySaving, setPolicySaving] = React.useState(false);
  const [schedulingSetting, setSchedulingSetting] = React.useState<SchedulingSetting | null>(null);
  const [keywordRules, setKeywordRules] = React.useState<ContentKeywordRule[]>([]);
  const [policyForm, setPolicyForm] = React.useState({
    jitter_min_seconds: 5,
    jitter_max_seconds: 30,
    batch_interval_seconds: 60,
    respect_send_window: true,
    quiet_hours_enabled: false,
    quiet_start: '02:00',
    quiet_end: '08:00',
    quiet_timezone: 'Asia/Shanghai',
    default_max_retries: 3,
    default_retry_delay_seconds: 60,
    default_retry_backoff: 'exponential' as SchedulingSetting['default_retry_backoff'],
    default_on_account_banned: 'skip_account' as SchedulingSetting['default_on_account_banned'],
    default_on_api_rate_limit: 'wait_and_retry' as SchedulingSetting['default_on_api_rate_limit'],
    default_on_content_rejected: 'skip_message' as SchedulingSetting['default_on_content_rejected'],
    default_account_hour_limit: 0,
    default_account_day_limit: 0,
    default_account_cooldown_seconds: 0,
    new_keyword: '',
  });

  async function loadMetrics() {
    setLoadingMetrics(true);
    setMetricsError('');
    try {
      setMetrics(await api<OperationMetricsSummary>('/operation-metrics/summary'));
    } catch (error) {
      setMetricsError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoadingMetrics(false);
    }
  }

  React.useEffect(() => {
    void loadMetrics();
  }, []);

  function applyPolicyForm(setting: SchedulingSetting) {
    setPolicyForm((current) => ({
      ...current,
      jitter_min_seconds: setting.jitter_min_seconds ?? 5,
      jitter_max_seconds: setting.jitter_max_seconds ?? 30,
      batch_interval_seconds: setting.batch_interval_seconds ?? 60,
      respect_send_window: Boolean(setting.respect_send_window),
      quiet_hours_enabled: Boolean(setting.quiet_hours_enabled),
      quiet_start: setting.quiet_start || '02:00',
      quiet_end: setting.quiet_end || '08:00',
      quiet_timezone: setting.quiet_timezone || 'Asia/Shanghai',
      default_max_retries: setting.default_max_retries ?? 3,
      default_retry_delay_seconds: setting.default_retry_delay_seconds ?? 60,
      default_retry_backoff: setting.default_retry_backoff || 'exponential',
      default_on_account_banned: setting.default_on_account_banned || 'skip_account',
      default_on_api_rate_limit: setting.default_on_api_rate_limit || 'wait_and_retry',
      default_on_content_rejected: setting.default_on_content_rejected || 'skip_message',
      default_account_hour_limit: setting.default_account_hour_limit ?? 0,
      default_account_day_limit: setting.default_account_day_limit ?? 0,
      default_account_cooldown_seconds: setting.default_account_cooldown_seconds ?? 0,
    }));
  }

  async function loadRiskPolicy() {
    const [setting, rules] = await Promise.all([
      api<SchedulingSetting>('/scheduling-settings'),
      api<ContentKeywordRule[]>('/content-keyword-rules'),
    ]);
    setSchedulingSetting(setting);
    setKeywordRules(rules);
    applyPolicyForm(setting);
  }

  async function openRiskPolicy() {
    setPolicyOpen(true);
    setMetricsError('');
    try {
      await loadRiskPolicy();
    } catch (error) {
      setMetricsError(error instanceof Error ? error.message : String(error));
    }
  }

  async function saveRiskPolicy() {
    setPolicySaving(true);
    setMetricsError('');
    try {
      const updated = await api<SchedulingSetting>('/scheduling-settings', {
        method: 'PATCH',
        body: JSON.stringify({
          jitter_min_seconds: policyForm.jitter_min_seconds,
          jitter_max_seconds: policyForm.jitter_max_seconds,
          batch_interval_seconds: policyForm.batch_interval_seconds,
          respect_send_window: policyForm.respect_send_window,
          quiet_hours_enabled: policyForm.quiet_hours_enabled,
          quiet_start: policyForm.quiet_start,
          quiet_end: policyForm.quiet_end,
          quiet_timezone: policyForm.quiet_timezone,
          default_max_retries: policyForm.default_max_retries,
          default_retry_delay_seconds: policyForm.default_retry_delay_seconds,
          default_retry_backoff: policyForm.default_retry_backoff,
          default_on_account_banned: policyForm.default_on_account_banned,
          default_on_api_rate_limit: policyForm.default_on_api_rate_limit,
          default_on_content_rejected: policyForm.default_on_content_rejected,
          default_account_hour_limit: policyForm.default_account_hour_limit,
          default_account_day_limit: policyForm.default_account_day_limit,
          default_account_cooldown_seconds: policyForm.default_account_cooldown_seconds,
        }),
      });
      setSchedulingSetting(updated);
      applyPolicyForm(updated);
      await loadMetrics();
    } catch (error) {
      setMetricsError(error instanceof Error ? error.message : String(error));
    } finally {
      setPolicySaving(false);
    }
  }

  async function toggleKeywordRule(rule: ContentKeywordRule, isActive: boolean) {
    setPolicySaving(true);
    setMetricsError('');
    try {
      const updated = await api<ContentKeywordRule>(`/content-keyword-rules/${rule.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: isActive }),
      });
      setKeywordRules((current) => current.map((item) => item.id === updated.id ? updated : item));
      await loadMetrics();
    } catch (error) {
      setMetricsError(error instanceof Error ? error.message : String(error));
    } finally {
      setPolicySaving(false);
    }
  }

  async function addKeywordRule() {
    const keyword = policyForm.new_keyword.trim();
    if (!keyword) return;
    setPolicySaving(true);
    setMetricsError('');
    try {
      const created = await api<ContentKeywordRule>('/content-keyword-rules', {
        method: 'POST',
        body: JSON.stringify({ keyword, match_type: 'contains', is_active: true, note: '运营数据风控中心新增' }),
      });
      setKeywordRules((current) => [created, ...current]);
      setPolicyForm((current) => ({ ...current, new_keyword: '' }));
      await loadMetrics();
    } catch (error) {
      setMetricsError(error instanceof Error ? error.message : String(error));
    } finally {
      setPolicySaving(false);
    }
  }

  const usageTable = useAntdTableControls<UsageLedger>({
    rows: usageLedgers,
    placeholder: '搜索模型 / 任务 / 状态 / 费用',
    search: [
      (item) => [
        item.id,
        item.provider_name,
        item.model_name,
        item.campaign_id,
        item.request_status,
        item.total_tokens,
        item.total_cost,
        item.currency,
        item.created_at,
      ],
    ],
  });

  const columns: ColumnsType<UsageLedger> = [
    {
      title: '模型',
      key: 'model',
      width: 220,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.provider_name || 'Mock'}</Typography.Text>
          <Typography.Text type="secondary">{item.model_name} / 关联来源 #{item.campaign_id ?? '-'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, item) => <StatusBadge status={item.request_status === 'success' ? '已完成' : '失败'} />,
    },
    {
      title: 'Token',
      key: 'tokens',
      width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.total_tokens}</Typography.Text>
          <Typography.Text type="secondary">输入 {item.prompt_tokens} / 输出 {item.completion_tokens}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '费用',
      key: 'cost',
      width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.total_cost} {item.currency}</Typography.Text>
          <Typography.Text type="secondary">{item.created_at}</Typography.Text>
        </Space>
      ),
    },
  ];
  const detailColumns: ColumnsType<OperationMetricDetail> = [
    {
      title: '对象',
      key: 'title',
      width: 260,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.title}</Typography.Text>
          <Typography.Text type="secondary">{item.category} / {item.related_id || '-'}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 130, render: (value) => <StatusBadge status={value} /> },
    { title: '详情', dataIndex: 'detail', ellipsis: true },
    { title: '时间', dataIndex: 'occurred_at', width: 190, render: (value) => formatBeijingDateTime(value) },
  ];

  return (
    <section className="view-grid">
      {metricsError && <Alert className="form-alert" type="error" showIcon message={metricsError} />}
      <Card className="panel" title="运营数据总览" extra={<Space><Button size="small" onClick={openRiskPolicy}>编辑风控策略</Button><Button size="small" icon={<RefreshCcw size={16} />} loading={loadingMetrics} onClick={loadMetrics}>刷新</Button></Space>}>
        <MetricSection title="账号数据" items={metrics?.accounts ?? []} />
        <MetricSection title="目标数据" items={metrics?.targets ?? []} />
        <MetricSection title="消息发送数据" items={metrics?.messages ?? []} />
        <MetricSection title="频道互动数据" items={metrics?.channel_interactions ?? []} />
        <MetricSection title="AI 活跃群数据" items={metrics?.ai_activity ?? []} />
        <MetricSection title="转发监听数据" items={metrics?.relay ?? []} />
        <MetricSection title="归档数据" items={metrics?.archives ?? []} />
        <MetricSection title="失败与风险数据" items={metrics?.failures ?? []} />
        <MetricSection title="全局风控中心" items={metrics?.risk_control ?? []} />
      </Card>
      <Card className="panel" title="运营明细定位" extra={<Typography.Text type="secondary">异常账号、风险目标、最近任务和失败执行项</Typography.Text>}>
        <MetricDetailSection title="异常账号" rows={metrics?.account_details ?? []} columns={detailColumns} />
        <MetricDetailSection title="风险目标" rows={metrics?.target_details ?? []} columns={detailColumns} />
        <MetricDetailSection title="最近任务" rows={metrics?.task_details ?? []} columns={detailColumns} />
        <MetricDetailSection title="失败/跳过执行项" rows={metrics?.failure_details ?? []} columns={detailColumns} />
        <MetricDetailSection title="风控策略与命中" rows={metrics?.risk_details ?? []} columns={detailColumns} />
      </Card>
      <Card className="panel" title="AI 用量汇总" extra={<Typography.Text type="secondary">按运营账号汇总 token 和费用</Typography.Text>}>
        <div className="stats-grid">
          <StatCard label="总请求" value={metricValue(metrics, 'ai_usage.requests', usageSummary?.total_requests ?? 0)} detail="AI 调用次数" icon={<Bot size={22} />} />
          <StatCard label="总 Token" value={metricValue(metrics, 'ai_usage.tokens', usageSummary?.total_tokens ?? 0)} detail="输入输出累计" icon={<Activity size={22} />} />
          <StatCard label="总费用" value={`${metricValue(metrics, 'ai_usage.cost', usageSummary?.total_cost ?? 0)} ${usageSummary?.currency ?? 'CNY'}`} detail="按模型单价结算" icon={<Database size={22} />} />
          <StatCard label="计费请求" value={usageSummary?.billable_requests ?? 0} detail="返回 usage 的真实请求" icon={<CheckCircle2 size={22} />} />
          {currentUser?.role !== '系统管理员' && <StatCard label="我的余额" value={currentUser?.token_balance ?? 0} detail={`累计额度 ${currentUser?.token_quota_total ?? 0}`} icon={<Activity size={22} />} />}
        </div>
        <List
          className="mini-list"
          dataSource={usageSummary?.by_user ?? []}
          locale={{ emptyText: '暂无用户用量汇总。' }}
          renderItem={(item) => (
            <List.Item>
              <List.Item.Meta title={item.user_name} description={`请求 ${item.requests} / Token ${item.total_tokens} / 费用 ${item.total_cost} ${item.currency}`} />
            </List.Item>
          )}
        />
      </Card>
      <Card className="panel" title="调用明细" extra={<Typography.Text type="secondary">记录用户、任务、模型、token 和费用</Typography.Text>}>
        <Space className="toolbar-row" wrap>
          {usageTable.searchInput}
        </Space>
        <Table<UsageLedger>
          className="tg-table"
          rowKey="id"
          columns={columns}
          dataSource={usageTable.filteredRows}
          pagination={usageTable.pagination}
          scroll={{ x: 760 }}
          locale={{ emptyText: '暂无调用明细。' }}
        />
      </Card>
      <Drawer title="全局风控策略" open={policyOpen} width={720} onClose={() => setPolicyOpen(false)} extra={<Button type="primary" loading={policySaving} onClick={saveRiskPolicy}>保存策略</Button>}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Alert type="info" showIcon message="这里编辑的是任务和消息发送共用的账号风控；账号上限填 0 表示不限制，达到上限后优先转派可用账号。" />
          <Card size="small" title="静默时段与默认失败策略">
            <div className="form-grid">
              <label>最小发送抖动秒<InputNumber min={0} value={policyForm.jitter_min_seconds} onChange={(value) => setPolicyForm((current) => ({ ...current, jitter_min_seconds: Number(value ?? 0) }))} /></label>
              <label>最大发送抖动秒<InputNumber min={0} value={policyForm.jitter_max_seconds} onChange={(value) => setPolicyForm((current) => ({ ...current, jitter_max_seconds: Number(value ?? 0) }))} /></label>
              <label>批次间隔秒<InputNumber min={0} value={policyForm.batch_interval_seconds} onChange={(value) => setPolicyForm((current) => ({ ...current, batch_interval_seconds: Number(value ?? 0) }))} /></label>
              <label>遵守发送窗口<Switch checked={policyForm.respect_send_window} onChange={(value) => setPolicyForm((current) => ({ ...current, respect_send_window: value }))} /></label>
              <label>启用静默<Switch checked={policyForm.quiet_hours_enabled} onChange={(value) => setPolicyForm((current) => ({ ...current, quiet_hours_enabled: value }))} /></label>
              <label>静默开始<Input value={policyForm.quiet_start} onChange={(event) => setPolicyForm((current) => ({ ...current, quiet_start: event.target.value }))} /></label>
              <label>静默结束<Input value={policyForm.quiet_end} onChange={(event) => setPolicyForm((current) => ({ ...current, quiet_end: event.target.value }))} /></label>
              <label>时区<Input value={policyForm.quiet_timezone} onChange={(event) => setPolicyForm((current) => ({ ...current, quiet_timezone: event.target.value }))} /></label>
              <label>默认重试次数<InputNumber min={0} max={10} value={policyForm.default_max_retries} onChange={(value) => setPolicyForm((current) => ({ ...current, default_max_retries: Number(value ?? 0) }))} /></label>
              <label>默认重试间隔秒<InputNumber min={0} value={policyForm.default_retry_delay_seconds} onChange={(value) => setPolicyForm((current) => ({ ...current, default_retry_delay_seconds: Number(value ?? 0) }))} /></label>
              <label>退避策略<Select value={policyForm.default_retry_backoff} onChange={(value) => setPolicyForm((current) => ({ ...current, default_retry_backoff: value }))} options={[{ value: 'none', label: '固定' }, { value: 'linear', label: '线性' }, { value: 'exponential', label: '指数' }]} /></label>
              <label>账号异常<Select value={policyForm.default_on_account_banned} onChange={(value) => setPolicyForm((current) => ({ ...current, default_on_account_banned: value }))} options={[{ value: 'skip_account', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'stop_task', label: '停止任务' }]} /></label>
              <label>API 限流<Select value={policyForm.default_on_api_rate_limit} onChange={(value) => setPolicyForm((current) => ({ ...current, default_on_api_rate_limit: value }))} options={[{ value: 'wait_and_retry', label: '等待重试' }, { value: 'skip', label: '跳过' }, { value: 'pause', label: '暂停' }]} /></label>
              <label>内容拦截<Select value={policyForm.default_on_content_rejected} onChange={(value) => setPolicyForm((current) => ({ ...current, default_on_content_rejected: value }))} options={[{ value: 'skip_message', label: '跳过消息' }, { value: 'rewrite_and_retry', label: '改写重试' }, { value: 'pause', label: '暂停' }]} /></label>
              <label>账号每小时上限(0不限制)<InputNumber min={0} value={policyForm.default_account_hour_limit} onChange={(value) => setPolicyForm((current) => ({ ...current, default_account_hour_limit: Number(value ?? 0) }))} /></label>
              <label>账号每日上限(0不限制)<InputNumber min={0} value={policyForm.default_account_day_limit} onChange={(value) => setPolicyForm((current) => ({ ...current, default_account_day_limit: Number(value ?? 0) }))} /></label>
              <label>账号全局冷却秒(0不限制)<InputNumber min={0} value={policyForm.default_account_cooldown_seconds} onChange={(value) => setPolicyForm((current) => ({ ...current, default_account_cooldown_seconds: Number(value ?? 0) }))} /></label>
            </div>
          </Card>
          <Card size="small" title="敏感词规则" extra={<Typography.Text type="secondary">{keywordRules.filter((rule) => rule.is_active).length}/{keywordRules.length} 启用</Typography.Text>}>
            <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
              <Input placeholder="新增敏感词" value={policyForm.new_keyword} onChange={(event) => setPolicyForm((current) => ({ ...current, new_keyword: event.target.value }))} onPressEnter={addKeywordRule} />
              <Button loading={policySaving} onClick={addKeywordRule}>新增</Button>
            </Space.Compact>
            <List
              className="mini-list"
              dataSource={keywordRules}
              locale={{ emptyText: '暂无敏感词规则。' }}
              renderItem={(rule) => (
                <List.Item actions={[<Switch key="switch" size="small" checked={rule.is_active} loading={policySaving} onChange={(checked) => toggleKeywordRule(rule, checked)} />]}>
                  <List.Item.Meta title={<Space><StatusBadge status={rule.is_active ? '已启用' : '已停用'} />{rule.keyword}</Space>} description={`${rule.match_type || 'contains'} / ${rule.note || '无备注'}`} />
                </List.Item>
              )}
            />
          </Card>
          {schedulingSetting && <Typography.Text type="secondary">当前配置更新时间以服务端记录为准，保存后会刷新全局风控指标。</Typography.Text>}
        </Space>
      </Drawer>
    </section>
  );
}

function metricValue(metrics: OperationMetricsSummary | null, key: string, fallback: number | string): number | string {
  const all = metrics ? [
    ...metrics.accounts,
    ...metrics.targets,
    ...metrics.messages,
    ...metrics.channel_interactions,
    ...metrics.ai_activity,
    ...metrics.relay,
    ...metrics.archives,
    ...metrics.ai_usage,
    ...metrics.failures,
    ...metrics.risk_control,
  ] : [];
  return all.find((item) => item.key === key)?.value ?? fallback;
}

function MetricSection({ title, items }: { title: string; items: MetricBucket[] }) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 16 }}>
      <Typography.Title level={5} style={{ margin: 0 }}>{title}</Typography.Title>
      <div className="stats-grid">
        {items.map((item) => (
          <StatCard key={item.key} label={item.label} value={item.value} detail={item.detail} icon={<Activity size={22} />} />
        ))}
      </div>
    </Space>
  );
}

function MetricDetailSection({ title, rows, columns }: { title: string; rows: OperationMetricDetail[]; columns: ColumnsType<OperationMetricDetail> }) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 18 }}>
      <Typography.Title level={5} style={{ margin: 0 }}>{title}</Typography.Title>
      <Table<OperationMetricDetail>
        className="tg-table"
        size="small"
        rowKey="key"
        columns={columns}
        dataSource={rows}
        pagination={false}
        scroll={{ x: 760 }}
        locale={{ emptyText: `暂无${title}` }}
      />
    </Space>
  );
}
