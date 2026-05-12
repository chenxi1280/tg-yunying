import React from 'react';
import { Alert, Button, Card, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, RefreshCcw, Users } from 'lucide-react';
import { api } from '../../shared/api/client';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';

type ListenerRow = {
  key: string;
  object_type: 'channel' | 'group';
  title: string;
  peer_id: string;
  status: string;
  listener_account_count: number;
  subscriber_task_count: number;
  event_backlog_count: number;
  pending_distribution_count: number;
  dedup_event_count: number;
  subscription_event_types: string[];
  last_event_at: string | null;
  last_error: string;
  backup_account: ListenerAccount | null;
  switch_recommended: boolean;
  switch_reason: string;
  task_ids: string[];
  listener_accounts: ListenerAccount[];
  subscriber_tasks: ListenerTask[];
  recent_events: ListenerEvent[];
};

type ListenerAccount = {
  id: number;
  display_name: string;
  username: string | null;
  status: string;
  roles: string[];
  task_ids: string[];
};

type ListenerTask = {
  id: string;
  name: string;
  type: string;
  status: string;
};

type ListenerEvent = {
  id: number;
  event_type: string;
  content: string;
  account_id: number | null;
  sender_name: string;
  occurred_at: string | null;
};

type ListenerSummary = {
  channel_count: number;
  group_count: number;
  subscriber_task_count: number;
  items: ListenerRow[];
};

function objectTypeLabel(value: ListenerRow['object_type']): string {
  return value === 'channel' ? '频道' : '源群/群聊';
}

export default function ListenerCenterView() {
  const [summary, setSummary] = React.useState<ListenerSummary>({ channel_count: 0, group_count: 0, subscriber_task_count: 0, items: [] });
  const [loading, setLoading] = React.useState(false);
  const [switchingKey, setSwitchingKey] = React.useState('');
  const [error, setError] = React.useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      setSummary(await api<ListenerSummary>('/listeners/summary'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(timer);
  }, []);

  async function switchListener(row: ListenerRow) {
    if (!row.backup_account) return;
    setSwitchingKey(row.key);
    setError('');
    try {
      const [, rawId] = row.key.split(':');
      setSummary(await api<ListenerSummary>(`/listeners/${row.object_type}/${rawId}/switch`, { method: 'POST', body: JSON.stringify({ backup_account_id: row.backup_account.id }) }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSwitchingKey('');
    }
  }

  const table = useAntdTableControls<ListenerRow>({
    rows: summary.items,
    placeholder: '搜索监听对象 / peer / 状态 / 错误',
    search: [
      (row) => [
        row.title,
        row.peer_id,
        objectTypeLabel(row.object_type),
        row.status,
        row.last_error,
        row.switch_reason,
        row.backup_account ? `${row.backup_account.display_name} ${row.backup_account.username ?? ''}` : '',
        row.recent_events.map((event) => `${event.content} ${event.sender_name}`).join(' '),
        row.task_ids.join(' '),
        row.listener_accounts.map((account) => `${account.display_name} ${account.username ?? ''} ${account.status}`).join(' '),
        row.subscriber_tasks.map((task) => `${task.name} ${task.type} ${task.status}`).join(' '),
      ],
    ],
  });

  const columns: ColumnsType<ListenerRow> = [
    {
      title: '监听对象',
      key: 'object',
      width: 280,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.title}</Typography.Text>
          <Typography.Text type="secondary">{objectTypeLabel(row.object_type)} / {row.peer_id}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 130, render: (value) => <StatusBadge status={value} /> },
    { title: '关联任务', dataIndex: 'subscriber_task_count', width: 110 },
    {
      title: '订阅事件',
      key: 'subscription_event_types',
      width: 190,
      render: (_, row) => row.subscription_event_types.length ? (
        <Space size={[4, 4]} wrap>{row.subscription_event_types.map((item) => <Tag key={item}>{item}</Tag>)}</Space>
      ) : '-',
    },
    {
      title: '监听账号',
      dataIndex: 'listener_account_count',
      width: 130,
      render: (value, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{value || '-'}</Typography.Text>
          {!!row.listener_accounts.length && <Typography.Text type="secondary">展开查看</Typography.Text>}
        </Space>
      ),
    },
    { title: '待分发', dataIndex: 'pending_distribution_count', width: 100, render: (value) => value || '-' },
    { title: '去重键', dataIndex: 'dedup_event_count', width: 100, render: (value) => value || '-' },
    {
      title: '切换建议',
      key: 'switch',
      width: 180,
      render: (_, row) => row.switch_recommended ? (
        <Space direction="vertical" size={0}>
          <StatusBadge status="需切换" />
          <Typography.Text type="secondary">{row.backup_account?.display_name ?? '暂无备用账号'}</Typography.Text>
        </Space>
      ) : (
        row.backup_account ? <Typography.Text type="secondary">备用：{row.backup_account.display_name}</Typography.Text> : '-'
      ),
    },
    { title: '最后事件', dataIndex: 'last_event_at', width: 190, render: (value) => value ? new Date(value).toLocaleString() : '-' },
    { title: '最近错误', dataIndex: 'last_error', render: (value) => value || '无' },
  ];

  return (
    <section className="view-grid">
      <Space className="stats-grid" wrap>
        <StatCard label="频道监听对象" value={summary.channel_count} detail="按频道聚合关联" icon={<Activity size={20} />} />
        <StatCard label="群监听对象" value={summary.group_count} detail="按源群聚合关联" icon={<Users size={20} />} />
        <StatCard label="关联任务数" value={summary.subscriber_task_count} detail="多个任务共享事件" icon={<RefreshCcw size={20} />} />
      </Space>
      <Card className="panel" title="监听中心" extra={<Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button>}>
        {error && <Alert className="form-alert" type="error" showIcon message={error} />}
        <Space className="toolbar-row" wrap>{table.searchInput}</Space>
        <Table<ListenerRow>
          className="tg-table"
          rowKey="key"
          columns={columns}
          dataSource={table.filteredRows}
          pagination={table.pagination}
          scroll={{ x: 1320 }}
          loading={loading}
          expandable={{ expandedRowRender: (row) => renderListenerDetail(row, switchListener, switchingKey), rowExpandable: (row) => Boolean(row.listener_accounts.length || row.subscriber_tasks.length || row.recent_events.length || row.backup_account) }}
          locale={{ emptyText: '暂无监听关联。启动运行中的频道互动、AI 活跃群或转发监听任务后会出现在这里。' }}
        />
      </Card>
    </section>
  );
}

function renderListenerDetail(row: ListenerRow, onSwitch: (row: ListenerRow) => void, switchingKey: string) {
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>事件订阅与分发</Typography.Text>
        <Space wrap>
          {row.subscription_event_types.map((item) => <Tag key={item}>{item}</Tag>)}
          <Typography.Text type="secondary">待分发 {row.pending_distribution_count || 0}</Typography.Text>
          <Typography.Text type="secondary">去重键 {row.dedup_event_count || 0}</Typography.Text>
        </Space>
      </Space>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>监听账号</Typography.Text>
        {row.listener_accounts.length ? (
          <Space wrap>
            {row.listener_accounts.map((account) => (
              <Space key={account.id} className="inline-meta" wrap>
                <Typography.Text strong>{account.display_name}</Typography.Text>
                {account.username && <Typography.Text type="secondary">@{account.username}</Typography.Text>}
                <StatusBadge status={account.status} />
                {account.roles.map((role) => <Tag key={role}>{role}</Tag>)}
                <Typography.Text type="secondary">任务 {account.task_ids.length}</Typography.Text>
              </Space>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无账号明细</Typography.Text>
        )}
      </Space>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>备用与切换</Typography.Text>
        <Space wrap>
          {row.backup_account ? (
            <Space className="inline-meta" wrap>
              <Typography.Text strong>{row.backup_account.display_name}</Typography.Text>
              {row.backup_account.username && <Typography.Text type="secondary">@{row.backup_account.username}</Typography.Text>}
              <StatusBadge status={row.backup_account.status} />
              {row.backup_account.roles.map((role) => <Tag key={role}>{role}</Tag>)}
            </Space>
          ) : (
            <Typography.Text type="secondary">暂无备用账号</Typography.Text>
          )}
          {row.switch_reason && <Alert type={row.switch_recommended ? 'warning' : 'info'} showIcon message={row.switch_reason} />}
          {row.backup_account && (
            <Button size="small" type={row.switch_recommended ? 'primary' : 'default'} loading={switchingKey === row.key} onClick={() => onSwitch(row)}>
              启用备用监听
            </Button>
          )}
        </Space>
      </Space>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>关联任务</Typography.Text>
        {row.subscriber_tasks.length ? (
          <Space wrap>
            {row.subscriber_tasks.map((task) => (
              <Space key={task.id} className="inline-meta" wrap>
                <Typography.Text>{task.name}</Typography.Text>
                <Tag>{task.type}</Tag>
                <StatusBadge status={task.status} />
              </Space>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无任务明细</Typography.Text>
        )}
      </Space>
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>最近事件</Typography.Text>
        {row.recent_events.length ? (
          <Table<ListenerEvent>
            size="small"
            rowKey={(event) => `${event.event_type}:${event.id}`}
            pagination={false}
            dataSource={row.recent_events}
            columns={[
              { title: '类型', dataIndex: 'event_type', width: 140 },
              { title: '来源', key: 'source', width: 160, render: (_, event) => event.sender_name || (event.account_id ? `账号 ${event.account_id}` : '-') },
              { title: '内容', dataIndex: 'content', ellipsis: true },
              { title: '时间', dataIndex: 'occurred_at', width: 190, render: (value) => value ? new Date(value).toLocaleString() : '-' },
            ]}
          />
        ) : (
          <Typography.Text type="secondary">暂无事件明细</Typography.Text>
        )}
      </Space>
    </Space>
  );
}
