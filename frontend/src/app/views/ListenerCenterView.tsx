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
  last_event_at: string | null;
  last_error: string;
  task_ids: string[];
  listener_accounts: ListenerAccount[];
  subscriber_tasks: ListenerTask[];
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
    { title: '事件积压', dataIndex: 'event_backlog_count', width: 110, render: (value) => value || '-' },
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
          scroll={{ x: 1080 }}
          loading={loading}
          expandable={{ expandedRowRender: renderListenerDetail, rowExpandable: (row) => Boolean(row.listener_accounts.length || row.subscriber_tasks.length) }}
          locale={{ emptyText: '暂无监听关联。启动运行中的频道互动、AI 活跃群或转发监听任务后会出现在这里。' }}
        />
      </Card>
    </section>
  );
}

function renderListenerDetail(row: ListenerRow) {
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
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
    </Space>
  );
}
