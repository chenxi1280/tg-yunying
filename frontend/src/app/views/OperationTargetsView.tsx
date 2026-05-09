import React from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api, ApiError } from '../../shared/api/client';
import type { OperationTarget } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

export default function OperationTargetsView() {
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [formError, setFormError] = React.useState('');
  const [editingTarget, setEditingTarget] = React.useState<OperationTarget | null>(null);
  const [form] = Form.useForm();

  function errorMessage(error: unknown) {
    if (error instanceof ApiError) {
      try {
        const parsed = JSON.parse(error.body) as { detail?: unknown };
        if (typeof parsed.detail === 'string') return parsed.detail;
      } catch {
        return error.body || error.message;
      }
      return error.body || error.message;
    }
    return error instanceof Error ? error.message : String(error);
  }

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

  async function saveTarget(values: any) {
    setSaving(true);
    setFormError('');
    try {
      const body = {
        target_type: values.target_type,
        tg_peer_id: values.tg_peer_id,
        title: values.title,
        username: values.username ?? '',
        member_count: values.member_count ?? 0,
        can_send: values.can_send ?? true,
        auth_status: values.auth_status ?? '已授权运营',
      };
      await api<OperationTarget>(editingTarget ? `/operation-targets/${editingTarget.id}` : '/operation-targets', {
        method: editingTarget ? 'PATCH' : 'POST',
        body: JSON.stringify(body),
      });
      setEditingTarget(null);
      form.resetFields();
      await load();
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  function startEdit(target: OperationTarget) {
    setEditingTarget(target);
    setFormError('');
    form.setFieldsValue({
      target_type: target.target_type,
      tg_peer_id: target.tg_peer_id,
      title: target.title,
      username: target.username,
      member_count: target.member_count,
      can_send: target.can_send,
      auth_status: target.auth_status,
    });
  }

  function cancelEdit() {
    setEditingTarget(null);
    setFormError('');
    form.resetFields();
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
    { title: '操作', key: 'actions', width: 100, render: (_, target) => <Button size="small" onClick={() => startEdit(target)}>编辑</Button> },
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
      <Card className="panel" title={editingTarget ? `编辑目标 #${editingTarget.id}` : '新增目标'}>
        {formError && <Alert className="form-alert" type="error" showIcon message={formError} />}
        <Form form={form} layout="vertical" onFinish={saveTarget} initialValues={{ target_type: 'group', can_send: true, auth_status: '已授权运营', member_count: 0 }}>
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
          <Space>
            <Button type="primary" htmlType="submit" loading={saving}>保存目标</Button>
            {editingTarget && <Button onClick={cancelEdit} disabled={saving}>取消编辑</Button>}
          </Space>
        </Form>
      </Card>
    </section>
  );
}
