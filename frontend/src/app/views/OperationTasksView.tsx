import React from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api, ApiError } from '../../shared/api/client';
import type { Account, ChannelMessage, OperationTarget, OperationTask } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

type Props = {
  accounts: Account[];
};

const TASK_TYPES = [
  { value: 'MESSAGE_SEND', label: '消息发送' },
  { value: 'CHANNEL_VIEW', label: '频道查看' },
  { value: 'CHANNEL_REACTION', label: '频道点赞' },
  { value: 'CHANNEL_REPLY', label: '频道回复' },
];

export default function OperationTasksView({ accounts }: Props) {
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [messages, setMessages] = React.useState<ChannelMessage[]>([]);
  const [tasks, setTasks] = React.useState<OperationTask[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [actionError, setActionError] = React.useState('');
  const [busyTaskId, setBusyTaskId] = React.useState<number | null>(null);
  const [form] = Form.useForm();
  const [messageForm] = Form.useForm();
  const taskType = Form.useWatch('task_type', form) ?? 'MESSAGE_SEND';
  const contentMode = Form.useWatch('content_mode', form) ?? 'literal';
  const channelTargets = targets.filter((target) => target.target_type === 'channel');

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
      const [targetData, messageData, taskData] = await Promise.all([
        api<OperationTarget[]>('/operation-targets'),
        api<ChannelMessage[]>('/channel-messages'),
        api<OperationTask[]>('/operation-tasks'),
      ]);
      setTargets(targetData);
      setMessages(messageData);
      setTasks(taskData);
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  async function createChannelMessage(values: any) {
    setActionError('');
    try {
      await api<ChannelMessage>('/channel-messages', { method: 'POST', body: JSON.stringify(values) });
      messageForm.resetFields();
      await load();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function createTask(values: any) {
    setActionError('');
    try {
      await api<OperationTask>('/operation-tasks', {
        method: 'POST',
        body: JSON.stringify({
          task_type: values.task_type,
          target_id: values.task_type === 'MESSAGE_SEND' ? values.target_id : null,
          channel_message_id: values.task_type === 'MESSAGE_SEND' ? null : values.channel_message_id,
          title: values.title ?? '',
          content: values.content ?? '',
          reaction: values.reaction ?? '',
          account_ids: values.account_ids ?? [],
          quantity: values.quantity ?? 1,
          quantity_jitter_ratio: 0.15,
          content_mode: values.task_type === 'CHANNEL_REPLY' ? 'ai' : values.content_mode ?? 'literal',
          interval_seconds: values.interval_seconds ?? 0,
        }),
      });
      form.resetFields();
      await load();
    } catch (error) {
      setActionError(errorMessage(error));
    }
  }

  async function dispatch(task: OperationTask) {
    await runTaskAction(task, 'dispatch');
  }

  async function retry(task: OperationTask) {
    await runTaskAction(task, 'retry');
  }

  async function cancel(task: OperationTask) {
    await runTaskAction(task, 'cancel');
  }

  async function runTaskAction(task: OperationTask, action: 'dispatch' | 'retry' | 'cancel') {
    setBusyTaskId(task.id);
    setActionError('');
    try {
      await api<OperationTask>(`/operation-tasks/${task.id}/${action}`, { method: 'POST' });
      await load();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyTaskId(null);
    }
  }

  const table = useAntdTableControls<OperationTask>({
    rows: tasks,
    placeholder: '搜索任务 / 类型 / 内容 / 状态',
    search: [(task) => [task.id, task.title, task.task_type, task.content, task.reaction, task.status, task.failure_detail]],
  });

  const columns: ColumnsType<OperationTask> = [
    {
      title: '任务',
      key: 'task',
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>#{task.id} {TASK_TYPES.find((item) => item.value === task.task_type)?.label ?? task.task_type}</Typography.Text>
          <Typography.Text type="secondary">{task.title || '未命名'} / 目标 {task.quantity} / 实际 {task.actual_quantity ?? task.quantity} / 完成 {task.completed_count}</Typography.Text>
          <Typography.Text>{task.task_type === 'CHANNEL_REACTION' ? task.reaction : task.content || '无内容'}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', key: 'status', width: 120, render: (_, task) => <StatusBadge status={task.status} /> },
    { title: '失败', key: 'failure', width: 180, render: (_, task) => task.failure_type ? <StatusBadge status={task.failure_type} label={task.failure_detail || task.failure_type} /> : '无失败' },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_, task) => (
        <Space>
              <Button size="small" type="primary" loading={busyTaskId === task.id} disabled={task.status === '已完成'} onClick={() => dispatch(task)}>执行</Button>
              <Button size="small" disabled={task.status !== '失败' || busyTaskId === task.id} onClick={() => retry(task)}>重试</Button>
              <Button size="small" danger disabled={['执行中', '已完成', '已取消'].includes(task.status) || busyTaskId === task.id} onClick={() => cancel(task)}>取消</Button>
            </Space>
          ),
    },
  ];

  return (
    <section className="view-grid">
      <Card className="panel" title="运营任务">
        <Typography.Text type="secondary">消息发送支持普通文案或 AI 生成；频道查看、点赞、AI 回复分别是独立任务。</Typography.Text>
        {actionError && <Alert className="form-alert" type="error" showIcon message={actionError} />}
        <Space className="toolbar-row" wrap>
          {table.searchInput}
          <Button loading={loading} onClick={load}>刷新</Button>
        </Space>
        <Table<OperationTask> className="tg-table" rowKey="id" columns={columns} dataSource={table.filteredRows} pagination={table.pagination} scroll={{ x: 960 }} loading={loading} />
      </Card>
      <Card className="panel" title="新建任务">
        <Form form={form} layout="vertical" onFinish={createTask} initialValues={{ task_type: 'MESSAGE_SEND', content_mode: 'literal', quantity: 1, interval_seconds: 0, account_ids: [] }}>
          <Form.Item name="task_type" label="任务类型" rules={[{ required: true }]}>
            <Select options={TASK_TYPES} />
          </Form.Item>
          <Form.Item name="title" label="任务名称">
            <Input />
          </Form.Item>
          {taskType === 'MESSAGE_SEND' && (
            <Form.Item name="content_mode" label="内容模式">
              <Select options={[
                { value: 'literal', label: '固定文案' },
                { value: 'ai', label: 'AI 生成' },
              ]} />
            </Form.Item>
          )}
          {taskType === 'MESSAGE_SEND' ? (
            <Form.Item name="target_id" label="群/频道目标" rules={[{ required: true }]}>
              <Select options={targets.map((target) => ({ value: target.id, label: `${target.target_type === 'channel' ? '频道' : '群聊'} / ${target.title}` }))} />
            </Form.Item>
          ) : (
            <Form.Item name="channel_message_id" label="频道消息" rules={[{ required: true }]}>
              <Select options={messages.map((message) => ({ value: message.id, label: `#${message.message_id} / ${message.content_preview || message.message_url || message.id}` }))} />
            </Form.Item>
          )}
          {taskType !== 'CHANNEL_VIEW' && taskType !== 'CHANNEL_REACTION' && (
            <Form.Item name="content" label={taskType === 'CHANNEL_REPLY' || contentMode === 'ai' ? 'AI 生成要求/主题' : '发送内容'} rules={[{ required: taskType !== 'CHANNEL_VIEW' }]}>
              <Input.TextArea rows={3} />
            </Form.Item>
          )}
          {taskType === 'CHANNEL_REACTION' && (
            <Form.Item name="reaction" label="点赞 Reaction" rules={[{ required: true }]}>
              <Input placeholder="👍" />
            </Form.Item>
          )}
          <Form.Item name="account_ids" label="执行账号">
            <Select mode="multiple" options={accounts.map((account) => ({ value: account.id, label: `${account.display_name} / ${account.status}`, disabled: account.status !== '在线' }))} />
          </Form.Item>
          <Form.Item name="quantity" label="目标数量">
            <InputNumber min={1} max={500} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="interval_seconds" label="执行间隔秒">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Button type="primary" htmlType="submit">创建任务</Button>
        </Form>
      </Card>
      <Card className="panel" title="登记频道消息">
        <Form form={messageForm} layout="vertical" onFinish={createChannelMessage}>
          <Form.Item name="channel_target_id" label="频道" rules={[{ required: true }]}>
            <Select options={channelTargets.map((target) => ({ value: target.id, label: target.title }))} />
          </Form.Item>
          <Form.Item name="message_id" label="消息 ID" rules={[{ required: true }]}>
            <InputNumber min={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="message_url" label="消息链接">
            <Input />
          </Form.Item>
          <Form.Item name="content_preview" label="内容预览">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Button htmlType="submit">保存频道消息</Button>
        </Form>
      </Card>
    </section>
  );
}
