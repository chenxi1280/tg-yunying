import React from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag } from 'antd';
import { api } from '../../shared/api/client';
import type { ProxyAirportSubscription } from '../types';

interface Props {
  canManageSystem?: boolean;
}

type NewSubscriptionForm = {
  name: string;
  subscription_url: string;
  priority: number;
  enabled: boolean;
  failover_policy: string;
  auto_failback_enabled: boolean;
  failback_cooldown_minutes: number;
};

type EditSubscriptionForm = {
  name: string;
  subscription_url?: string;
  priority: number;
  enabled: boolean;
  failover_policy: string;
  auto_failback_enabled: boolean;
  failback_cooldown_minutes: number;
};

const DEFAULT_SUBSCRIPTION_PRIORITY = 10;
const SUBSCRIPTION_PRIORITY_STEP = 10;
const MIN_SUBSCRIPTION_PRIORITY = 1;
const MAX_SUBSCRIPTION_PRIORITY = 9999;

function errorText(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes('proxy_airport_subscription_priority_conflict')) {
    return '启用订阅的优先级不能重复，请改为未使用的优先级后重试。';
  }
  return message;
}

function enabledPrioritySet(rows: ProxyAirportSubscription[]) {
  return new Set(rows.filter((row) => row.enabled).map((row) => row.priority));
}

function nextAvailablePriority(rows: ProxyAirportSubscription[]) {
  const usedPriorities = enabledPrioritySet(rows);
  for (let priority = DEFAULT_SUBSCRIPTION_PRIORITY; priority <= MAX_SUBSCRIPTION_PRIORITY; priority += SUBSCRIPTION_PRIORITY_STEP) {
    if (!usedPriorities.has(priority)) return priority;
  }
  for (let priority = MIN_SUBSCRIPTION_PRIORITY; priority <= MAX_SUBSCRIPTION_PRIORITY; priority += 1) {
    if (!usedPriorities.has(priority)) return priority;
  }
  return MAX_SUBSCRIPTION_PRIORITY;
}

function proxyAirportReadinessLabel(row: ProxyAirportSubscription | null) {
  if (!row?.subscription_url_configured) return '未配置';
  if (!row.enabled) return '已停用';
  if (row.last_error) return '节点同步失败';
  if (row.healthy_node_count > 0) return '健康节点可用';
  if (row.node_count > 0) return '同步成功但健康节点为 0';
  if (row.sync_status === 'test_pending') return '节点同步中';
  return '配置已保存，等待节点同步';
}

function statusColor(row: ProxyAirportSubscription) {
  if (!row.enabled) return 'default';
  if (row.last_error) return 'red';
  if (row.healthy_node_count > 0) return 'green';
  if (row.node_count > 0) return 'orange';
  return 'blue';
}

function summarySubscription(rows: ProxyAirportSubscription[]) {
  return rows.find((row) => row.enabled && row.healthy_node_count > 0)
    ?? rows.find((row) => row.enabled && !row.last_error)
    ?? rows.find((row) => row.enabled)
    ?? null;
}

export default function ProxyAirportSubscriptionView({ canManageSystem = false }: Props) {
  const [rows, setRows] = React.useState<ProxyAirportSubscription[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState('');
  const [notice, setNotice] = React.useState('');
  const [form] = Form.useForm<NewSubscriptionForm>();
  const [editForm] = Form.useForm<EditSubscriptionForm>();
  const [editingRow, setEditingRow] = React.useState<ProxyAirportSubscription | null>(null);

  React.useEffect(() => {
    void loadConfig();
  }, []);

  React.useEffect(() => {
    const currentPriority = form.getFieldValue('priority');
    if (!currentPriority || enabledPrioritySet(rows).has(currentPriority)) {
      form.setFieldsValue({ priority: nextAvailablePriority(rows) });
    }
  }, [form, rows]);

  async function loadConfig() {
    setLoading(true);
    setError('');
    try {
      setRows(await api<ProxyAirportSubscription[]>('/proxy-airport-subscriptions'));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }

  async function saveConfig(values: NewSubscriptionForm) {
    setSaving(true);
    setError('');
    try {
      await api<ProxyAirportSubscription>('/proxy-airport-subscriptions', {
        method: 'POST',
        body: JSON.stringify(values),
      });
      form.resetFields();
      setNotice('Clash 订阅源已保存');
      await loadConfig();
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(row: ProxyAirportSubscription, enabled: boolean) {
    setSaving(true);
    setError('');
    try {
      await api<ProxyAirportSubscription>(`/proxy-airport-subscriptions/${row.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      });
      setNotice(enabled ? '订阅源已启用' : '订阅源已停用');
      await loadConfig();
    } catch (toggleError) {
      setError(errorText(toggleError));
    } finally {
      setSaving(false);
    }
  }

  async function syncRow(row: ProxyAirportSubscription) {
    setSaving(true);
    setError('');
    try {
      await api<ProxyAirportSubscription>(`/proxy-airport-subscriptions/${row.id}/sync`, { method: 'POST' });
      setNotice('订阅节点已解析，健康探测完成前不可作为可用代理池');
      await loadConfig();
    } catch (syncError) {
      setError(errorText(syncError));
    } finally {
      setSaving(false);
    }
  }

  function openEdit(row: ProxyAirportSubscription) {
    setEditingRow(row);
    editForm.setFieldsValue({
      name: row.name,
      subscription_url: '',
      priority: row.priority,
      enabled: row.enabled,
      failover_policy: row.failover_policy,
      auto_failback_enabled: row.auto_failback_enabled,
      failback_cooldown_minutes: row.failback_cooldown_minutes,
    });
  }

  async function saveExisting(values: EditSubscriptionForm) {
    if (!editingRow?.id) return;
    setSaving(true);
    setError('');
    const subscriptionUrl = values.subscription_url?.trim();
    const payload = {
      name: values.name,
      priority: values.priority,
      enabled: values.enabled,
      failover_policy: values.failover_policy,
      auto_failback_enabled: values.auto_failback_enabled,
      failback_cooldown_minutes: values.failback_cooldown_minutes,
      ...(subscriptionUrl ? { subscription_url: subscriptionUrl } : {}),
    };
    try {
      await api<ProxyAirportSubscription>(`/proxy-airport-subscriptions/${editingRow.id}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      setNotice(subscriptionUrl ? '订阅源已更新，需重新同步节点' : '订阅源配置已更新');
      setEditingRow(null);
      await loadConfig();
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSaving(false);
    }
  }

  const primary = summarySubscription(rows);

  return (
    <Card className="panel" title="Clash 订阅源池" extra={<Button size="small" onClick={loadConfig} loading={loading}>刷新</Button>}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {notice && <Alert type="success" showIcon message={notice} style={{ marginBottom: 12 }} />}
      <Descriptions
        bordered
        size="small"
        column={2}
        items={[
          { key: 'readiness', label: '代理池状态', children: proxyAirportReadinessLabel(primary) },
          { key: 'sources', label: '启用订阅数', children: rows.filter((row) => row.enabled).length },
          { key: 'nodes', label: '同步节点数', children: rows.reduce((sum, row) => sum + row.node_count, 0) },
          { key: 'healthy-nodes', label: '健康节点数', children: rows.reduce((sum, row) => sum + row.healthy_node_count, 0) },
          { key: 'last-sync', label: '最近同步时间', children: primary?.last_sync_at ? primary.last_sync_at.replace('T', ' ').slice(0, 16) : '-' },
          { key: 'error', label: '最近错误', children: primary?.last_error || '-' },
        ]}
      />
      <Table
        style={{ marginTop: 16 }}
        rowKey={(row) => row.id ?? row.subscription_url_preview}
        dataSource={rows}
        loading={loading}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '优先级', dataIndex: 'priority', sorter: (a, b) => a.priority - b.priority },
          {
            title: '切换策略',
            dataIndex: 'failover_policy',
            render: (value) => value === 'same_subscription_first' ? '同订阅优先' : value,
          },
          {
            title: '自动切回',
            dataIndex: 'auto_failback_enabled',
            render: (value) => (value ? '开启' : '关闭'),
          },
          {
            title: '启用',
            dataIndex: 'enabled',
            render: (_value, row) => (
              <Switch checked={row.enabled} disabled={!canManageSystem || saving} onChange={(checked) => toggleEnabled(row, checked)} />
            ),
          },
          { title: '订阅地址', dataIndex: 'subscription_url_preview' },
          { title: '同步状态', dataIndex: 'sync_status' },
          { title: '同步节点数', dataIndex: 'node_count' },
          { title: '健康节点数', dataIndex: 'healthy_node_count' },
          {
            title: '最近同步时间',
            dataIndex: 'last_sync_at',
            render: (_value, row) => (row.last_sync_at ? row.last_sync_at.replace('T', ' ').slice(0, 16) : '-'),
          },
          {
            title: '状态',
            render: (_value, row) => <Tag color={statusColor(row)}>{proxyAirportReadinessLabel(row)}</Tag>,
          },
          { title: '最近错误', dataIndex: 'last_error', render: (value) => value || '-' },
          {
            title: '操作',
            render: (_value, row) => (
              <Space>
                <Button size="small" onClick={() => openEdit(row)} disabled={!canManageSystem || saving}>
                  编辑
                </Button>
                <Button size="small" onClick={() => syncRow(row)} loading={saving} disabled={!canManageSystem || !row.subscription_url_configured}>
                  同步
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Form
        form={form}
        layout="inline"
        style={{ marginTop: 16 }}
        initialValues={{
          name: '',
          priority: DEFAULT_SUBSCRIPTION_PRIORITY,
          enabled: true,
          failover_policy: 'same_subscription_first',
          auto_failback_enabled: false,
          failback_cooldown_minutes: 1440,
        }}
        onFinish={saveConfig}
        disabled={!canManageSystem}
      >
        <Form.Item name="name" rules={[{ required: true, message: '请输入名称' }]}>
          <Input placeholder="主订阅" />
        </Form.Item>
        <Form.Item name="subscription_url" rules={[{ required: true, message: '请输入订阅地址' }]}>
          <Input.Password placeholder="https://example.com/clash/subscription" />
        </Form.Item>
        <Form.Item name="priority" rules={[{ required: true, message: '请输入优先级' }]}>
          <InputNumber min={MIN_SUBSCRIPTION_PRIORITY} max={MAX_SUBSCRIPTION_PRIORITY} />
        </Form.Item>
        <Form.Item name="failover_policy" rules={[{ required: true, message: '请选择切换策略' }]}>
          <Select
            style={{ width: 128 }}
            options={[{ value: 'same_subscription_first', label: '同订阅优先' }]}
          />
        </Form.Item>
        <Form.Item name="auto_failback_enabled" valuePropName="checked">
          <Switch disabled checkedChildren="切回" unCheckedChildren="不切回" />
        </Form.Item>
        <Form.Item name="failback_cooldown_minutes" rules={[{ required: true, message: '请输入切回冷却' }]}>
          <InputNumber min={0} max={10080} addonAfter="分钟" />
        </Form.Item>
        <Form.Item name="enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={saving} disabled={!canManageSystem}>
              新增
            </Button>
          </Space>
        </Form.Item>
      </Form>
      <Modal
        title="编辑 Clash 订阅源"
        open={Boolean(editingRow)}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onCancel={() => setEditingRow(null)}
        onOk={() => editForm.submit()}
      >
        <Form form={editForm} layout="vertical" onFinish={saveExisting} disabled={!canManageSystem}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="主订阅" />
          </Form.Item>
          <Form.Item name="subscription_url" label="订阅地址">
            <Input.Password placeholder="留空则不修改已保存地址" />
          </Form.Item>
          <Form.Item name="priority" label="优先级" rules={[{ required: true, message: '请输入优先级' }]}>
            <InputNumber min={MIN_SUBSCRIPTION_PRIORITY} max={MAX_SUBSCRIPTION_PRIORITY} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="failover_policy" label="切换策略" rules={[{ required: true, message: '请选择切换策略' }]}>
            <Select options={[{ value: 'same_subscription_first', label: '同订阅优先' }]} />
          </Form.Item>
          <Form.Item name="auto_failback_enabled" label="主订阅恢复后自动切回" valuePropName="checked">
            <Switch disabled />
          </Form.Item>
          <Form.Item name="failback_cooldown_minutes" label="自动切回冷却分钟" rules={[{ required: true, message: '请输入切回冷却' }]}>
            <InputNumber min={0} max={10080} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
