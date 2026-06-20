import React from 'react';
import { Activity, Bot, CheckCircle2, Database, RefreshCcw } from 'lucide-react';
import { Alert, Button, Card, List, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { CurrentUser, MetricBucket, OperationMetricDetail, OperationMetricsSummary, UsageLedger, UsageSummary } from '../types';
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
      <Card className="panel" title="运营数据总览" extra={<Button size="small" icon={<RefreshCcw size={16} />} loading={loadingMetrics} onClick={loadMetrics}>刷新</Button>}>
        <MetricSection title="账号数据" items={metrics?.accounts ?? []} />
        <LoginDropRateSection rows={metrics?.account_pool_login_drop_rates ?? []} />
        <MetricSection title="目标数据" items={metrics?.targets ?? []} />
        <MetricSection title="消息发送数据" items={metrics?.messages ?? []} />
        <MetricSection title="频道互动数据" items={metrics?.channel_interactions ?? []} />
        <MetricSection title="AI 活跃群数据" items={metrics?.ai_activity ?? []} />
        <MetricSection title="转发监听数据" items={metrics?.relay ?? []} />
        <MetricSection title="归档数据" items={metrics?.archives ?? []} />
        <MetricSection title="失败与风险数据" items={metrics?.failures ?? []} />
        <MetricSection title="风控统计" items={metrics?.risk_control ?? []} />
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

function LoginDropRateSection({ rows }: { rows: OperationMetricDetail[] }) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 16 }}>
      <Typography.Title level={5} style={{ margin: 0 }}>账号分组登录掉号比例</Typography.Title>
      <div className="stats-grid">
        {rows.map((item) => (
          <StatCard key={item.key} label={item.title} value={item.status} detail={`登录问题账号：${item.detail}`} icon={<Activity size={22} />} />
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
