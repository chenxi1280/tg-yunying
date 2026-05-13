import React from 'react';
import { Activity, CheckCircle2, Database, RefreshCcw, ShieldAlert } from 'lucide-react';
import { Button, Card, Descriptions, Empty, Progress, Space, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { AccountProxy, RiskControlAccountScore, RiskControlSummary, RiskDispositionItem, RiskHitRecord, RiskProxyAlert } from '../types';
import { formatBeijingDateTime } from '../time';
import { Badge, StatCard, StatusBadge, useAntdTableControls } from '../components/shared';

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

function formatLimit(used: number, limit: number) {
  return limit > 0 ? `${used}/${limit}` : `${used}/不限`;
}

interface Props {
  onOpenAccounts: () => void;
  onOpenSystemConfig: () => void;
}

export default function RiskControlView({ onOpenAccounts, onOpenSystemConfig }: Props) {
  const [summary, setSummary] = React.useState<RiskControlSummary | null>(null);
  const [proxies, setProxies] = React.useState<AccountProxy[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [checkingProxyId, setCheckingProxyId] = React.useState<number | null>(null);
  const [error, setError] = React.useState('');

  const loadSummary = React.useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextSummary, nextProxies] = await Promise.all([
        api<RiskControlSummary>('/risk-control/summary'),
        api<AccountProxy[]>('/account-proxies'),
      ]);
      setSummary(nextSummary);
      setProxies(nextProxies);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '读取风控中心失败');
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const accountTable = useAntdTableControls<RiskControlAccountScore>({
    rows: summary?.account_scores ?? [],
    placeholder: '搜索账号 / 分组 / 状态 / 风险 / 代理',
    search: [(row) => [row.account_id, row.display_name, row.username, row.pool_name, row.login_status, row.risk_level, row.recent_risk, row.blocked_reason, row.proxy_name, row.proxy_local_address, row.proxy_status, row.proxy_alert_status]],
  });
  const queueTable = useAntdTableControls<RiskDispositionItem>({
    rows: summary?.disposition_queue ?? [],
    placeholder: '搜索处置类型 / 账号 / 原因',
    search: [(row) => [row.item_type, row.account_name, row.target, row.reason, row.suggested_action, row.status]],
  });
  const hitTable = useAntdTableControls<RiskHitRecord>({
    rows: summary?.hit_records ?? [],
    placeholder: '搜索命中来源 / 账号 / 策略 / 任务',
    search: [(row) => [row.source, row.account_name, row.task_id, row.target, row.policy, row.action, row.detail]],
  });

  async function checkProxy(proxyId: number) {
    setCheckingProxyId(proxyId);
    setError('');
    try {
      await api(`/account-proxies/${proxyId}/check`, {
        method: 'POST',
        body: JSON.stringify({ check_type: 'quick', reason: '风控中心手动检查' }),
      });
      await loadSummary();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '代理检查失败');
    } finally {
      setCheckingProxyId(null);
    }
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
          <Typography.Text type="secondary">@{row.username ?? '未设置'} / {row.phone_masked}</Typography.Text>
          <Typography.Text type="secondary">{row.pool_name}</Typography.Text>
        </Space>
      ),
    },
    { title: '登录状态', dataIndex: 'login_status', width: 130, render: (value: string) => <StatusBadge status={value} /> },
    { title: '等级', dataIndex: 'risk_level', width: 90, render: (value: string) => <Badge tone={riskLevelTone(value)}>{value}</Badge> },
    { title: '健康分', dataIndex: 'health_score', width: 150, render: (value: number) => <Progress percent={value} size="small" status={value < 55 ? 'exception' : value < 85 ? 'normal' : 'success'} /> },
    {
      title: '本地代理',
      key: 'proxy',
      width: 240,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.proxy_name || '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">{row.proxy_local_address || '建议绑定后执行高频发送'}</Typography.Text>
          {row.proxy_status && <StatusBadge status={row.proxy_status} label={row.proxy_alert_status ? `${row.proxy_status} / ${row.proxy_alert_status}` : row.proxy_status} />}
        </Space>
      ),
    },
    { title: '当前策略', dataIndex: 'current_policy', width: 130 },
    { title: '小时用量', key: 'hour', width: 110, render: (_, row) => formatLimit(row.hour_usage, row.hour_limit) },
    { title: '日用量', key: 'day', width: 110, render: (_, row) => formatLimit(row.day_usage, row.day_limit) },
    { title: '冷却到', dataIndex: 'cooldown_until', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '最近风险', dataIndex: 'recent_risk', width: 180, render: (value: string) => value || '-' },
    { title: '准入', dataIndex: 'can_join_task', width: 100, render: (value: boolean, row) => value ? <StatusBadge status="可用" /> : <StatusBadge status={row.blocked_reason || '容量不足'} /> },
  ];

  const queueColumns: ColumnsType<RiskDispositionItem> = [
    { title: '类型', dataIndex: 'item_type', width: 150, fixed: 'left' },
    { title: '级别', dataIndex: 'severity', width: 100, render: (value: string) => <Badge tone={severityTone(value)}>{value}</Badge> },
    { title: '账号', dataIndex: 'account_name', width: 160, render: (value: string) => value || '-' },
    { title: '原因', dataIndex: 'reason', width: 280 },
    { title: '处置动作', dataIndex: 'suggested_action', width: 240 },
    { title: '时间', dataIndex: 'occurred_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '状态', dataIndex: 'status', width: 100, render: (value: string) => <StatusBadge status={value} /> },
  ];

  const hitColumns: ColumnsType<RiskHitRecord> = [
    { title: '来源', dataIndex: 'source', width: 120 },
    { title: '账号', dataIndex: 'account_name', width: 160, render: (value: string) => value || '-' },
    { title: '任务/记录', dataIndex: 'task_id', width: 130 },
    { title: '命中策略', dataIndex: 'policy', width: 160, render: (value: string) => <StatusBadge status={value} /> },
    { title: '系统动作', dataIndex: 'action', width: 170 },
    { title: '详情', dataIndex: 'detail', width: 280 },
    { title: '时间', dataIndex: 'occurred_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
  ];

  const proxyColumns: ColumnsType<RiskProxyAlert> = [
    { title: 'proxy_id', dataIndex: 'proxy_id', width: 140 },
    { title: '本地代理地址', dataIndex: 'local_address', width: 220 },
    { title: '告警状态', dataIndex: 'alert_status', width: 140, render: (value: string) => <StatusBadge status={value} /> },
    { title: '绑定账号', dataIndex: 'bound_accounts', width: 110 },
    { title: '最近错误', dataIndex: 'last_error', width: 260 },
    { title: '处置动作', dataIndex: 'suggested_action', width: 220 },
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
    { title: '状态', key: 'status', width: 160, render: (_, row) => <StatusBadge status={row.status} label={`${row.status} / ${row.alert_status}`} /> },
    { title: '绑定账号', dataIndex: 'bound_account_count', width: 110 },
    { title: '容量', key: 'capacity', width: 150, render: (_, row) => `${row.bound_account_count}/${row.max_bound_accounts || '不限'} 账号` },
    { title: '检查间隔', dataIndex: 'check_interval_seconds', width: 120, render: (value: number) => `${value}s` },
    { title: '最近检查', dataIndex: 'last_check_at', width: 170, render: (value: string | null) => value ? formatBeijingDateTime(value) : '-' },
    { title: '最近错误', dataIndex: 'last_error', width: 260, render: (value: string) => value || '-' },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      fixed: 'right',
      render: (_, row) => <Button size="small" icon={<RefreshCcw size={14} />} loading={checkingProxyId === row.id} onClick={() => void checkProxy(row.id)}>检查</Button>,
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
          <StatCard key={metric.key} label={metric.label} value={metric.value} detail={metric.detail} icon={metric.key.includes('proxy') ? <Database size={22} /> : metric.key.includes('blocked') ? <ShieldAlert size={22} /> : <Activity size={22} />} />
        ))}
      </div>

      <Card
        className="panel"
        title="风控中心"
        extra={<Button icon={<RefreshCcw size={16} />} loading={loading} onClick={loadSummary}>刷新</Button>}
      >
        <Tabs
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
                    scroll={{ x: 1220 }}
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
                  <Button icon={<Database size={16} />} onClick={onOpenSystemConfig}>编辑策略</Button>
                  <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
                    <Descriptions.Item label="发送抖动">{policy.jitter_min_seconds}s - {policy.jitter_max_seconds}s</Descriptions.Item>
                    <Descriptions.Item label="批次间隔">{policy.batch_interval_seconds}s</Descriptions.Item>
                    <Descriptions.Item label="发送窗口">{policy.respect_send_window ? '遵守' : '不限制'}</Descriptions.Item>
                    <Descriptions.Item label="静默时段">{policy.quiet_hours_enabled ? `${policy.quiet_start}-${policy.quiet_end}` : '关闭'}</Descriptions.Item>
                    <Descriptions.Item label="时区">{policy.quiet_timezone}</Descriptions.Item>
                    <Descriptions.Item label="默认重试">{policy.default_max_retries} 次 / {policy.default_retry_delay_seconds}s / {policy.default_retry_backoff}</Descriptions.Item>
                    <Descriptions.Item label="账号异常">{policy.default_on_account_banned}</Descriptions.Item>
                    <Descriptions.Item label="API 限流">{policy.default_on_api_rate_limit}</Descriptions.Item>
                    <Descriptions.Item label="内容拦截">{policy.default_on_content_rejected}</Descriptions.Item>
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
                    <Button onClick={onOpenAccounts}>去账号中心</Button>
                  </Space>
                  <Table<RiskControlAccountScore>
                    className="tg-table"
                    rowKey="account_id"
                    columns={accountColumns}
                    dataSource={accountTable.filteredRows}
                    pagination={accountTable.pagination}
                    loading={loading}
                    scroll={{ x: 1530 }}
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
                    scroll={{ x: 1220 }}
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
                    scroll={{ x: 1230 }}
                    locale={{ emptyText: '暂无策略命中记录。' }}
                  />
                </>
              ),
            },
            {
              key: 'proxy',
              label: '代理资源',
              children: (
                <Space direction="vertical" size={16} className="full-width">
                  <Table<AccountProxy>
                    className="tg-table"
                    rowKey="id"
                    columns={proxyResourceColumns}
                    dataSource={proxies}
                    loading={loading}
                    scroll={{ x: 1390 }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无本地代理资源。" /> }}
                  />
                  <Table<RiskProxyAlert>
                    className="tg-table"
                    rowKey={(row) => row.id ?? row.proxy_id ?? row.local_address}
                    columns={proxyColumns}
                    dataSource={summary?.proxy_alerts ?? []}
                    loading={loading}
                    scroll={{ x: 1100 }}
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
    </section>
  );
}
