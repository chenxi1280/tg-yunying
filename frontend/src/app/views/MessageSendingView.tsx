import React from 'react';
import dayjs from 'dayjs';
import { App as AntdApp, Button, Card, Col, DatePicker, Form, Input, Modal, Radio, Row, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { MessageSquareText, RefreshCcw, Send, ShieldAlert } from 'lucide-react';
import { StatusBadge } from '../components/shared';
import type { Account, AccountGroup, Contact, Material, MessageSendBatchCreate, MessageSendTarget, MessageTask, OperationTarget } from '../types';
import { api, ApiError } from '../../shared/api/client';

type TargetType = 'private' | 'group' | 'channel';
type MessageType = '文本' | '图片';

type Props = {
  accounts: Account[];
  materials: Material[];
  tasks: MessageTask[];
  createMessageSendTask: (payload: MessageSendBatchCreate) => Promise<MessageTask[]>;
  onCancelTask: (task: MessageTask) => Promise<void>;
  onDispatchTask: (task: MessageTask) => Promise<void>;
  onRetryTask: (task: MessageTask) => Promise<void>;
  onRefresh: () => Promise<void>;
  isActionPending: (key: string) => boolean;
};

type TargetOption = {
  value: string;
  label: React.ReactNode;
  searchText: string;
  target: MessageSendTarget;
};

const targetTypeLabels: Record<TargetType, string> = {
  private: '个人',
  group: '群聊',
  channel: '频道',
};

const statusOptions = ['排队中', '发送中', '已发送', '失败', '已取消'].map((status) => ({ value: status, label: status }));

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { detail?: string };
      return parsed.detail || error.body;
    } catch {
      return error.body;
    }
  }
  return error instanceof Error ? error.message : '未知错误';
}

function optionFilter(input: string, option?: { label?: React.ReactNode; searchText?: string }) {
  const value = option?.searchText ?? option?.label ?? '';
  return String(value).toLowerCase().includes(input.toLowerCase());
}

function formatTime(value?: string | null) {
  if (!value) return '-';
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  return new Date(normalized).toLocaleString();
}

function compactText(value?: string | null) {
  return value && value.trim() ? value : '-';
}

export default function MessageSendingView({
  accounts,
  materials,
  tasks,
  createMessageSendTask,
  onCancelTask,
  onDispatchTask,
  onRetryTask,
  onRefresh,
  isActionPending,
}: Props) {
  const { message } = AntdApp.useApp();
  const [taskOpen, setTaskOpen] = React.useState(false);
  const [accountId, setAccountId] = React.useState<number | undefined>();
  const [targetKeys, setTargetKeys] = React.useState<string[]>([]);
  const [manualTargets, setManualTargets] = React.useState<TargetOption[]>([]);
  const [messageType, setMessageType] = React.useState<MessageType>('文本');
  const [content, setContent] = React.useState('');
  const [materialId, setMaterialId] = React.useState<number | undefined>();
  const [dispatchNow, setDispatchNow] = React.useState(true);
  const [scheduledAt, setScheduledAt] = React.useState<string | null>(null);
  const [contacts, setContacts] = React.useState<Contact[]>([]);
  const [accountGroups, setAccountGroups] = React.useState<AccountGroup[]>([]);
  const [operationTargets, setOperationTargets] = React.useState<OperationTarget[]>([]);
  const [localMaterials, setLocalMaterials] = React.useState<Material[]>(materials);
  const [statusFilter, setStatusFilter] = React.useState<string>('');
  const [recordAccountId, setRecordAccountId] = React.useState<number | undefined>();
  const [targetQuery, setTargetQuery] = React.useState('');
  const [contentQuery, setContentQuery] = React.useState('');
  const [manualOpen, setManualOpen] = React.useState(false);
  const [manualTargetType, setManualTargetType] = React.useState<TargetType>('private');
  const [manualPeer, setManualPeer] = React.useState('');
  const [manualDisplay, setManualDisplay] = React.useState('');
  const [materialOpen, setMaterialOpen] = React.useState(false);
  const [materialForm, setMaterialForm] = React.useState({ title: '', content: '', tags: '' });
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [detailTask, setDetailTask] = React.useState<MessageTask | null>(null);
  const [error, setError] = React.useState('');
  const [loadingTargets, setLoadingTargets] = React.useState(false);
  const [savingMaterial, setSavingMaterial] = React.useState(false);

  React.useEffect(() => setLocalMaterials(materials), [materials]);

  React.useEffect(() => {
    if (!accountId) {
      setContacts([]);
      setAccountGroups([]);
      return;
    }
    let active = true;
    setLoadingTargets(true);
    Promise.all([
      api<Contact[]>(`/tg-accounts/${accountId}/contacts`).catch(() => []),
      api<AccountGroup[]>(`/tg-accounts/${accountId}/groups`).catch(() => []),
    ]).then(([nextContacts, nextGroups]) => {
      if (!active) return;
      setContacts(nextContacts);
      setAccountGroups(nextGroups);
    }).finally(() => {
      if (active) setLoadingTargets(false);
    });
    return () => { active = false; };
  }, [accountId]);

  React.useEffect(() => {
    function loadOperationTargets() {
      api<OperationTarget[]>('/operation-targets?target_type=channel')
        .then((items) => setOperationTargets(items))
        .catch(() => setOperationTargets([]));
    }

    loadOperationTargets();
    const timer = window.setInterval(loadOperationTargets, 60000);
    return () => window.clearInterval(timer);
  }, []);

  React.useEffect(() => {
    const timer = window.setInterval(() => void onRefresh(), 60000);
    return () => window.clearInterval(timer);
  }, [onRefresh]);

  const onlineAccounts = accounts.filter((account) => account.status === '在线' && !account.deleted_at);
  const selectedAccount = onlineAccounts.find((account) => account.id === accountId);
  const accountLabel = React.useCallback((id?: number | null) => {
    const account = accounts.find((item) => item.id === id);
    return account ? `${account.display_name}${account.username ? ` / @${account.username}` : ''}` : '-';
  }, [accounts]);

  const materialOptions = localMaterials
    .filter((material) => material.review_status === '已审核' && material.material_type === '图片')
    .map((material) => ({
      value: material.id,
      label: `${material.title} / ${material.content}`,
      searchText: `${material.title} ${material.content} ${material.tags}`,
    }));

  const targetOptions = React.useMemo<TargetOption[]>(() => {
    const privateOptions = contacts.map((contact) => {
      const peer = contact.username ? `@${contact.username}` : contact.peer_id;
      const title = contact.display_name || peer;
      return {
        value: `private:${peer}`,
        searchText: `${title} ${contact.username || ''} ${contact.peer_id} ${contact.phone_masked || ''}`,
        label: <Space size={6}><Tag color="blue">个人</Tag><span>{title}</span><Typography.Text type="secondary">{contact.username || contact.peer_id}</Typography.Text></Space>,
        target: { target_type: 'private', target_peer_id: peer, target_display: title },
      } satisfies TargetOption;
    });
    const groupOptions = accountGroups
      .filter((group) => group.account_can_send && group.auth_status === '已授权运营' && group.can_send)
      .map((group) => ({
        value: `group:${group.id}`,
        searchText: `${group.title} ${group.group_type} ${group.auth_status}`,
        label: <Space size={6}><Tag color="green">群聊</Tag><span>{group.title}</span><Typography.Text type="secondary">{group.group_type}</Typography.Text></Space>,
        target: { target_type: 'group', group_id: group.id, target_display: group.title },
      } satisfies TargetOption));
    const channelOptions = operationTargets
      .filter((target) => target.target_type === 'channel' && target.can_send && target.auth_status === '已授权运营')
      .map((target) => ({
        value: `channel:${target.id}`,
        searchText: `${target.title} ${target.username || ''} ${target.tg_peer_id}`,
        label: <Space size={6}><Tag color="purple">频道</Tag><span>{target.title}</span><Typography.Text type="secondary">{target.username || target.tg_peer_id}</Typography.Text></Space>,
        target: { target_type: 'channel', operation_target_id: target.id, target_display: target.title },
      } satisfies TargetOption));
    const builtinValues = new Set([...privateOptions, ...groupOptions, ...channelOptions].map((option) => option.value));
    return [...privateOptions, ...groupOptions, ...channelOptions, ...manualTargets.filter((option) => !builtinValues.has(option.value))];
  }, [accountGroups, contacts, manualTargets, operationTargets]);

  const selectedTargets = React.useMemo(
    () => targetKeys.map((key) => targetOptions.find((option) => option.value === key)).filter((option): option is TargetOption => Boolean(option)),
    [targetKeys, targetOptions],
  );

  function resetComposer() {
    setAccountId(undefined);
    setTargetKeys([]);
    setManualTargets([]);
    setMessageType('文本');
    setContent('');
    setMaterialId(undefined);
    setDispatchNow(true);
    setScheduledAt(null);
    setError('');
  }

  function buildPayload(): MessageSendBatchCreate {
    if (!accountId) throw new Error('请选择发送账号');
    if (!selectedTargets.length) throw new Error('请选择发送目标');
    if (messageType === '文本' && !content.trim()) throw new Error('请输入消息内容');
    if (messageType === '图片' && !materialId) throw new Error('请选择图片素材');
    if (!dispatchNow && !scheduledAt) throw new Error('请选择定时发送时间');
    return {
      account_id: accountId,
      targets: selectedTargets.map((option) => option.target),
      content: content.trim(),
      message_type: messageType,
      material_id: messageType === '图片' ? materialId ?? null : null,
      dispatch_now: dispatchNow,
      scheduled_at: dispatchNow ? null : scheduledAt,
    };
  }

  function openConfirm() {
    try {
      buildPayload();
      setError('');
      setConfirmOpen(true);
    } catch (validationError) {
      const nextError = errorText(validationError);
      setError(nextError);
      void message.error(nextError);
    }
  }

  async function submit() {
    try {
      const created = await createMessageSendTask(buildPayload());
      void message.success(`已创建 ${created.length} 条发送任务`);
      setConfirmOpen(false);
      setTaskOpen(false);
      resetComposer();
    } catch (submitError) {
      const nextError = errorText(submitError);
      setError(nextError);
      void message.error(nextError);
    }
  }

  async function createMaterial() {
    if (!materialForm.title.trim() || !materialForm.content.trim()) {
      setError('请输入素材名称和图片 URL');
      void message.error('请输入素材名称和图片 URL');
      return;
    }
    setSavingMaterial(true);
    try {
      const created = await api<Material>('/materials', {
        method: 'POST',
        body: JSON.stringify({ ...materialForm, material_type: '图片' }),
      });
      setLocalMaterials((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setMaterialId(created.id);
      setMaterialOpen(false);
      setMaterialForm({ title: '', content: '', tags: '' });
      await onRefresh();
    } catch (materialError) {
      const nextError = `创建素材失败：${errorText(materialError)}`;
      setError(nextError);
      void message.error(nextError);
    } finally {
      setSavingMaterial(false);
    }
  }

  function applyManualTarget() {
    const peer = manualPeer.trim();
    if (!peer) {
      setError('请输入 peer id 或 username');
      void message.error('请输入 peer id 或 username');
      return;
    }
    const display = manualDisplay.trim() || peer;
    const key = `manual:${manualTargetType}:${peer}`;
    const option: TargetOption = {
      value: key,
      searchText: `${targetTypeLabels[manualTargetType]} ${display} ${peer}`,
      label: <Space size={6}><Tag>{targetTypeLabels[manualTargetType]}</Tag><span>{display}</span><Typography.Text type="secondary">手动输入</Typography.Text></Space>,
      target: { target_type: manualTargetType, target_peer_id: peer, target_display: display },
    };
    setManualTargets((current) => [option, ...current.filter((item) => item.value !== key)]);
    setTargetKeys((current) => Array.from(new Set([...current, key])));
    setManualOpen(false);
    setManualPeer('');
    setManualDisplay('');
    setError('');
  }

  const recentTasks = tasks
    .filter((task) => !task.campaign_id && ['private', 'group', 'channel'].includes(task.target_type))
    .filter((task) => !recordAccountId || task.account_id === recordAccountId)
    .filter((task) => !statusFilter || task.status === statusFilter)
    .filter((task) => {
      const query = targetQuery.trim().toLowerCase();
      if (!query) return true;
      return [task.target_display, task.target_peer_id, targetTypeLabels[task.target_type as TargetType]].some((value) => String(value || '').toLowerCase().includes(query));
    })
    .filter((task) => !contentQuery.trim() || task.content.toLowerCase().includes(contentQuery.trim().toLowerCase()));

  const columns: ColumnsType<MessageTask> = [
    { title: '发送账号', dataIndex: 'account_id', width: 180, render: (value) => accountLabel(value) },
    { title: '目标', dataIndex: 'target_display', width: 220, render: (value, task) => <Space direction="vertical" size={0}><span>{value || task.target_peer_id || '-'}</span><Typography.Text type="secondary">{targetTypeLabels[task.target_type as TargetType] || task.target_type}</Typography.Text></Space> },
    { title: '类型', dataIndex: 'message_type', width: 90 },
    { title: '内容', dataIndex: 'content', ellipsis: true, render: (value) => compactText(value) },
    { title: '计划时间', dataIndex: 'scheduled_at', width: 180, render: formatTime },
    { title: '发送时间', dataIndex: 'sent_at', width: 180, render: formatTime },
    {
      title: '状态',
      dataIndex: 'status',
      width: 170,
      render: (value, task) => (
        <Space direction="vertical" size={2}>
          <StatusBadge status={value} />
          {value === '失败' && <Typography.Text type="danger">{task.failure_detail || task.failure_type || '发送失败'}</Typography.Text>}
        </Space>
      ),
    },
    {
      title: '操作',
      width: 260,
      render: (_, task) => {
        const cancellable = !['已发送', '发送中', '已取消'].includes(task.status);
        return (
          <Space wrap>
            <Button size="small" icon={<Send size={14} />} disabled={task.status === '已发送'} loading={isActionPending(`task:${task.id}:dispatch`)} onClick={() => onDispatchTask(task)}>立即执行</Button>
            <Button size="small" icon={<RefreshCcw size={14} />} disabled={!['失败', '已取消'].includes(task.status)} loading={isActionPending(`task:${task.id}:retry`)} onClick={() => onRetryTask(task)}>重试</Button>
            <Button size="small" danger icon={<ShieldAlert size={14} />} disabled={!cancellable} loading={isActionPending(`task:${task.id}:cancel`)} onClick={() => onCancelTask(task)}>取消</Button>
            <Button size="small" onClick={() => setDetailTask(task)}>详情</Button>
          </Space>
        );
      },
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="发送记录"
        extra={(
          <Space wrap>
            <Button icon={<RefreshCcw size={16} />} onClick={onRefresh}>刷新</Button>
            <Button type="primary" icon={<MessageSquareText size={16} />} onClick={() => setTaskOpen(true)}>新建发送</Button>
          </Space>
        )}
      >
        <Form layout="vertical">
          <Row gutter={12}>
            <Col xs={24} md={6}>
              <Form.Item label="账号">
                <Select
                  showSearch
                  allowClear
                  placeholder="筛选发送账号"
                  value={recordAccountId}
                  onChange={setRecordAccountId}
                  filterOption={optionFilter}
                  options={accounts.map((account) => ({
                    value: account.id,
                    label: `${account.display_name} / ${account.username || '-'} / ${account.phone_masked || ''}`,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={5}>
              <Form.Item label="状态">
                <Select allowClear placeholder="筛选状态" value={statusFilter || undefined} onChange={(value) => setStatusFilter(value || '')} options={statusOptions} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item label="目标/接收人">
                <Input allowClear value={targetQuery} onChange={(event) => setTargetQuery(event.target.value)} placeholder="搜索目标名称、peer" />
              </Form.Item>
            </Col>
            <Col xs={24} md={7}>
              <Form.Item label="发送内容">
                <Input.Search allowClear value={contentQuery} onChange={(event) => setContentQuery(event.target.value)} onSearch={(value) => setContentQuery(value.trim())} placeholder="搜索消息内容" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
        <Table<MessageTask> rowKey="id" columns={columns} dataSource={recentTasks} pagination={{ pageSize: 10, showSizeChanger: true }} scroll={{ x: 1320 }} />
      </Card>

      <Modal
        title="新建发送"
        open={taskOpen}
        onCancel={() => { setTaskOpen(false); setConfirmOpen(false); resetComposer(); }}
        footer={null}
        width={900}
        destroyOnHidden
      >
        {error && <Typography.Paragraph type="danger">{error}</Typography.Paragraph>}
        <Form layout="vertical">
          <Row gutter={16}>
            <Col xs={24} md={10}>
              <Form.Item label="发送账号">
                <Select
                  showSearch
                  allowClear
                  placeholder="搜索账号、username、手机号"
                  value={accountId}
                  onChange={(value) => {
                    setAccountId(value);
                    setTargetKeys([]);
                    setManualTargets([]);
                  }}
                  filterOption={optionFilter}
                  options={onlineAccounts.map((account) => ({
                    value: account.id,
                    label: `${account.display_name} / ${account.username || '-'} / ${account.phone_masked || ''}`,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={14}>
              <Form.Item label="发送目标">
                <Space.Compact style={{ width: '100%' }}>
                  <Select
                    mode="multiple"
                    showSearch
                    allowClear
                    style={{ width: '100%' }}
                    loading={loadingTargets}
                    placeholder={accountId ? '搜索并多选个人、群聊、频道' : '先选择账号'}
                    value={targetKeys}
                    onChange={setTargetKeys}
                    filterOption={optionFilter}
                    disabled={!accountId}
                    options={targetOptions}
                  />
                  <Button disabled={!accountId} onClick={() => setManualOpen(true)}>手动</Button>
                </Space.Compact>
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item label="消息类型">
                <Radio.Group value={messageType} onChange={(event) => { setMessageType(event.target.value); setMaterialId(undefined); }}>
                  <Radio.Button value="文本">文本</Radio.Button>
                  <Radio.Button value="图片">图片</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} md={16}>
              <Form.Item label={messageType === '文本' ? '消息内容' : '图片配文'}>
                <Input.TextArea value={content} onChange={(event) => setContent(event.target.value)} rows={3} placeholder={messageType === '文本' ? '输入要发送的消息' : '可选，作为图片配文'} />
              </Form.Item>
            </Col>
          </Row>
          {messageType === '图片' && (
            <Form.Item label="图片素材">
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  showSearch
                  allowClear
                  style={{ width: '100%' }}
                  placeholder="选择已审核图片素材"
                  value={materialId}
                  onChange={setMaterialId}
                  filterOption={optionFilter}
                  options={materialOptions}
                />
                <Button icon={<MessageSquareText size={16} />} onClick={() => setMaterialOpen(true)}>新增素材</Button>
              </Space.Compact>
            </Form.Item>
          )}
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item label="发送方式">
                <Radio.Group value={dispatchNow} onChange={(event) => setDispatchNow(event.target.value)}>
                  <Radio.Button value>立即发送</Radio.Button>
                  <Radio.Button value={false}>定时发送</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            {!dispatchNow && (
              <Col xs={24} md={10}>
                <Form.Item label="开始发送时间">
                  <DatePicker
                    showTime
                    style={{ width: '100%' }}
                    value={scheduledAt ? dayjs(scheduledAt) : null}
                    onChange={(value) => setScheduledAt(value ? value.toISOString() : null)}
                    placeholder="选择开始发送时间"
                  />
                </Form.Item>
              </Col>
            )}
            <Col xs={24} md={dispatchNow ? 16 : 6}>
              <Form.Item label=" ">
                <Button type="primary" block icon={<Send size={16} />} onClick={openConfirm} loading={isActionPending('message-send:create')}>提交发送</Button>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Modal title="手动输入目标" open={manualOpen} onCancel={() => setManualOpen(false)} onOk={applyManualTarget} okText="选择">
        <Form layout="vertical">
          <Form.Item label="目标类型">
            <Radio.Group value={manualTargetType} onChange={(event) => setManualTargetType(event.target.value)}>
              <Radio.Button value="private">个人</Radio.Button>
              <Radio.Button value="group">群聊</Radio.Button>
              <Radio.Button value="channel">频道</Radio.Button>
            </Radio.Group>
          </Form.Item>
          <Form.Item label="Peer ID / username">
            <Input value={manualPeer} onChange={(event) => setManualPeer(event.target.value)} placeholder="@username 或 peer id" />
          </Form.Item>
          <Form.Item label="显示名称">
            <Input value={manualDisplay} onChange={(event) => setManualDisplay(event.target.value)} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="新增图片素材" open={materialOpen} onCancel={() => setMaterialOpen(false)} onOk={createMaterial} confirmLoading={savingMaterial} okText="创建并选中">
        <Form layout="vertical">
          <Form.Item label="素材名称"><Input value={materialForm.title} onChange={(event) => setMaterialForm((form) => ({ ...form, title: event.target.value }))} /></Form.Item>
          <Form.Item label="图片 URL"><Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm((form) => ({ ...form, content: event.target.value }))} rows={3} placeholder="支持常见图片 URL / JPEG URL" /></Form.Item>
          <Form.Item label="标签"><Input value={materialForm.tags} onChange={(event) => setMaterialForm((form) => ({ ...form, tags: event.target.value }))} /></Form.Item>
        </Form>
      </Modal>

      <Modal title="确认发送" open={confirmOpen} onCancel={() => setConfirmOpen(false)} onOk={submit} confirmLoading={isActionPending('message-send:create')} okText="确认提交">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>账号：{selectedAccount?.display_name || accountId}</Typography.Text>
          <Typography.Text>目标：{selectedTargets.map((option) => option.searchText.split(' ').slice(0, 2).join(' ')).join('、')}</Typography.Text>
          <Typography.Text>消息类型：{messageType}</Typography.Text>
          <Typography.Text>发送方式：{dispatchNow ? '立即发送' : `定时发送 / ${formatTime(scheduledAt)}`}</Typography.Text>
          <Typography.Paragraph>{content || '无文本配文'}</Typography.Paragraph>
        </Space>
      </Modal>

      <Modal title={`任务 #${detailTask?.id ?? ''}`} open={Boolean(detailTask)} onCancel={() => setDetailTask(null)} footer={null}>
        {detailTask && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>账号：{accountLabel(detailTask.account_id)}</Typography.Text>
            <Typography.Text>目标：{detailTask.target_display || detailTask.target_peer_id}</Typography.Text>
            <Typography.Text>计划时间：{formatTime(detailTask.scheduled_at)}</Typography.Text>
            <Typography.Text>发送时间：{formatTime(detailTask.sent_at)}</Typography.Text>
            <Typography.Text>状态：{detailTask.status}</Typography.Text>
            {detailTask.status === '失败' && <Typography.Paragraph type="danger">失败原因：{detailTask.failure_detail || detailTask.failure_type || '发送失败'}</Typography.Paragraph>}
            <Typography.Paragraph>内容：{detailTask.content || '-'}</Typography.Paragraph>
          </Space>
        )}
      </Modal>
    </Space>
  );
}
