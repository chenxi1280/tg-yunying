import React from 'react';
import { Button, Card, Form, Input, InputNumber, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { OperationTarget } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

export default function OperationTargetsView() {
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      setTargets(await api<OperationTarget[]>('/operation-targets'));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  async function createTarget(values: any) {
    await api<OperationTarget>('/operation-targets', {
      method: 'POST',
      body: JSON.stringify({
        target_type: values.target_type,
        tg_peer_id: values.tg_peer_id,
        title: values.title,
        username: values.username ?? '',
        member_count: values.member_count ?? 0,
        can_send: values.can_send ?? true,
        auth_status: values.auth_status ?? '已授权运营',
      }),
    });
    form.resetFields();
    await load();
  }

  const table = useAntdTableControls<OperationTarget>({
    rows: targets,
    placeholder: '搜索群/频道 / peer / username / 状态',
    search: [(target) => [target.title, target.tg_peer_id, target.username, target.target_type, target.auth_status]],
  });

  const columns: ColumnsType<OperationTarget> = [
    {
      title: '目标',
      key: 'target',
      render: (_, target) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{target.title}</Typography.Text>
          <Typography.Text type="secondary">{target.target_type === 'channel' ? '频道' : '群聊'} / {target.tg_peer_id}{target.username ? ` / @${target.username}` : ''}</Typography.Text>
        </Space>
      ),
    },
    { title: '人数', dataIndex: 'member_count', key: 'member_count', width: 110 },
    { title: '使用范围', key: 'auth_status', width: 140, render: (_, target) => <StatusBadge status={target.auth_status} /> },
    { title: '发送能力', key: 'can_send', width: 140, render: (_, target) => <StatusBadge status={target.can_send ? '可发送' : '只读'} /> },
    { title: '最近同步', key: 'last_sync_at', width: 200, render: (_, target) => target.last_sync_at ? new Date(target.last_sync_at).toLocaleString() : '手动创建' },
  ];

  return (
    <section className="view-grid">
      <Card className="panel" title="群/频道目标">
        <Typography.Text type="secondary">统一维护账号运营目标。群聊用于普通发言，频道用于发帖、查看、点赞和回复任务。</Typography.Text>
        <Space className="toolbar-row" wrap>
          {table.searchInput}
          <Button loading={loading} onClick={load}>刷新</Button>
        </Space>
        <Table<OperationTarget>
          className="tg-table"
          rowKey="id"
          columns={columns}
          dataSource={table.filteredRows}
          pagination={table.pagination}
          scroll={{ x: 880 }}
          loading={loading}
        />
      </Card>
      <Card className="panel" title="新增目标">
        <Form form={form} layout="vertical" onFinish={createTarget} initialValues={{ target_type: 'group', can_send: true, auth_status: '已授权运营', member_count: 0 }}>
          <Form.Item name="target_type" label="目标类型" rules={[{ required: true }]}>
            <Select options={[{ value: 'group', label: '群聊' }, { value: 'channel', label: '频道' }]} />
          </Form.Item>
          <Form.Item name="title" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="tg_peer_id" label="TG Peer ID / username" rules={[{ required: true }]}>
            <Input placeholder="-100..." />
          </Form.Item>
          <Form.Item name="username" label="Username">
            <Input />
          </Form.Item>
          <Form.Item name="member_count" label="人数">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="auth_status" label="使用范围">
            <Select options={[{ value: '已授权运营', label: '已授权运营' }, { value: '只读归档', label: '只读归档' }, { value: '禁止操作', label: '禁止操作' }]} />
          </Form.Item>
          <Form.Item name="can_send" label="发送能力">
            <Select options={[{ value: true, label: '可发送/可发帖' }, { value: false, label: '只读' }]} />
          </Form.Item>
          <Button type="primary" htmlType="submit">保存目标</Button>
        </Form>
      </Card>
    </section>
  );
}
