import React from 'react';
import { App as AntdApp, Button, Card, Col, Form, Input, InputNumber, Modal, Radio, Row, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { MessageSquareText, RefreshCcw, Send, ShieldAlert } from 'lucide-react';
import { StatusBadge } from '../components/shared';
import type { Account, AccountGroup, Contact, Material, MessageSendTaskCreate, MessageTask, OperationTarget } from '../types';
import { api, ApiError } from '../../shared/api/client';

type TargetType = 'private' | 'group' | 'channel';
type MessageType = '文本' | '图片' | '表情包';

type Props = {
  accounts: Account[];
  materials: Material[];
  tasks: MessageTask[];
  createMessageSendTask: (payload: MessageSendTaskCreate) => Promise<MessageTask>;
  onCancelTask: (task: MessageTask) => Promise<void>;
  onDispatchTask: (task: MessageTask) => Promise<void>;
  onRetryTask: (task: MessageTask) => Promise<void>;
  onRefresh: () => Promise<void>;
  isActionPending: (key: string) => boolean;
};

const targetTypeLabels: Record<TargetType, string> = {
  private: '个人',
  group: '群聊',
  channel: '频道',
};

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

function optionFilter(input: string, option?: { label?: React.ReactNode }) {
  return String(option?.label ?? '').toLowerCase().includes(input.toLowerCase());
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
  const [accountId, setAccountId] = React.useState<number | undefined>();
  const [targetType, setTargetType] = React.useState<TargetType>('private');
  const [targetKey, setTargetKey] = React.useState<string>();
  const [messageType, setMessageType] = React.useState<MessageType>('文本');
  const [content, setContent] = React.useState('');
  const [materialId, setMaterialId] = React.useState<number | undefined>();
  const [jitterMin, setJitterMin] = React.useState(0);
  const [jitterMax, setJitterMax] = React.useState(0);
  const [dispatchNow, setDispatchNow] = React.useState(true);
  const [contacts, setContacts] = React.useState<Contact[]>([]);
  const [accountGroups, setAccountGroups] = React.useState<AccountGroup[]>([]);
  const [operationTargets, setOperationTargets] = React.useState<OperationTarget[]>([]);
  const [localMaterials, setLocalMaterials] = React.useState<Material[]>(materials);
  const [statusFilter, setStatusFilter] = React.useState<string>('');
  const [manualOpen, setManualOpen] = React.useState(false);
  const [manualPeer, setManualPeer] = React.useState('');
  const [manualDisplay, setManualDisplay] = React.useState('');
  const [materialOpen, setMaterialOpen] = React.useState(false);
  const [materialForm, setMaterialForm] = React.useState({ title: '', material_type: '图片' as MessageType, content: '', tags: '' });
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
    api<OperationTarget[]>('/operation-targets?target_type=channel')
      .then((items) => setOperationTargets(items))
      .catch(() => setOperationTargets([]));
  }, []);

  const onlineAccounts = accounts.filter((account) => account.status === '在线' && !account.deleted_at);
  const selectedAccount = onlineAccounts.find((account) => account.id === accountId);
  const materialOptions = localMaterials
    .filter((material) => material.review_status === '已审核' && material.material_type === messageType)
    .map((material) => ({
      value: material.id,
      label: `${material.title} / ${material.material_type} / ${material.content}`,
    }));

  const targetOptions = React.useMemo(() => {
    if (targetType === 'private') {
      return contacts.map((contact) => {
        const peer = contact.username ? `@${contact.username}` : contact.peer_id;
        return {
          value: `peer:${peer}`,
          label: `${contact.display_name || peer} / ${contact.username || contact.peer_id} / ${contact.phone_masked || ''}`,
        };
      });
    }
    if (targetType === 'group') {
      return accountGroups
        .filter((group) => group.account_can_send && group.auth_status === '已授权运营' && group.can_send)
        .map((group) => ({
          value: `group:${group.id}`,
          label: `${group.title} / ${group.group_type} / ${group.auth_status}`,
        }));
    }
    return operationTargets
      .filter((target) => target.target_type === 'channel' && target.can_send && target.auth_status === '已授权运营')
      .map((target) => ({
        value: `target:${target.id}`,
        label: `${target.title} / ${target.username || target.tg_peer_id} / ${target.member_count}`,
      }));
  }, [accountGroups, contacts, operationTargets, targetType]);

  function resetTarget(nextType: TargetType) {
    setTargetType(nextType);
    setTargetKey(undefined);
    setManualPeer('');
    setManualDisplay('');
    setError('');
  }

  function buildPayload(): MessageSendTaskCreate {
    if (!accountId) throw new Error('请选择发送账号');
    let target_peer_id: string | null = null;
    let target_display = '';
    let group_id: number | null = null;
    let operation_target_id: number | null = null;
    if (targetKey?.startsWith('peer:')) {
      target_peer_id = targetKey.slice(5);
      const contact = contacts.find((item) => (item.username ? `@${item.username}` : item.peer_id) === target_peer_id);
      target_display = contact?.display_name || target_peer_id;
    } else if (targetKey?.startsWith('group:')) {
      group_id = Number(targetKey.slice(6));
      target_display = accountGroups.find((group) => group.id === group_id)?.title || '';
    } else if (targetKey?.startsWith('target:')) {
      operation_target_id = Number(targetKey.slice(7));
      target_display = operationTargets.find((target) => target.id === operation_target_id)?.title || '';
    } else if (targetKey === 'manual') {
      target_peer_id = manualPeer.trim();
      target_display = manualDisplay.trim() || target_peer_id;
    }
    if (!target_peer_id && !group_id && !operation_target_id) throw new Error(`请选择${targetTypeLabels[targetType]}目标`);
    if (messageType === '文本' && !content.trim()) throw new Error('请输入消息内容');
    if (messageType !== '文本' && !materialId) throw new Error(`请选择${messageType}素材`);
    return {
      account_id: accountId,
      target_type: targetType,
      target_peer_id,
      target_display,
      group_id,
      operation_target_id,
      content: content.trim(),
      message_type: messageType,
      material_id: messageType === '文本' ? null : materialId ?? null,
      jitter_min_seconds: jitterMin,
      jitter_max_seconds: Math.max(jitterMax, jitterMin),
      dispatch_now: dispatchNow,
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
      const task = await createMessageSendTask(buildPayload());
      void message.success(task.status === '已发送' ? '消息已发送' : `消息任务已创建，状态：${task.status}`);
      setConfirmOpen(false);
      setTargetKey(undefined);
      setContent('');
      setMaterialId(undefined);
      setManualPeer('');
      setManualDisplay('');
      setError('');
    } catch (submitError) {
      const nextError = errorText(submitError);
      setError(nextError);
      void message.error(nextError);
    }
  }

  async function createMaterial() {
    if (!materialForm.title.trim() || !materialForm.content.trim()) {
      setError('请输入素材名称和内容 URL');
      void message.error('请输入素材名称和内容 URL');
      return;
    }
    setSavingMaterial(true);
    try {
      const created = await api<Material>('/materials', {
        method: 'POST',
        body: JSON.stringify({ ...materialForm, material_type: messageType === '文本' ? materialForm.material_type : messageType }),
      });
      setLocalMaterials((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setMaterialId(created.id);
      setMaterialOpen(false);
      setMaterialForm({ title: '', material_type: messageType === '表情包' ? '表情包' : '图片', content: '', tags: '' });
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
    if (!manualPeer.trim()) {
      setError('请输入 peer id 或 username');
      void message.error('请输入 peer id 或 username');
      return;
    }
    setTargetKey('manual');
    setManualOpen(false);
    setError('');
  }

  const recentTasks = tasks
    .filter((task) => !task.campaign_id && ['private', 'group', 'channel'].includes(task.target_type))
    .filter((task) => !statusFilter || task.status === statusFilter);

  const columns: ColumnsType<MessageTask> = [
    { title: '目标', dataIndex: 'target_display', render: (value, task) => <Space direction="vertical" size={0}><span>{value || task.target_peer_id || '-'}</span><Typography.Text type="secondary">{targetTypeLabels[task.target_type as TargetType] || task.target_type}</Typography.Text></Space> },
    { title: '类型', dataIndex: 'message_type', width: 90 },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '计划时间', dataIndex: 'scheduled_at', width: 180, render: (value) => new Date(value).toLocaleString() },
    { title: '内容', dataIndex: 'content', ellipsis: true },
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
      <Card title="发送台" extra={<Tag color="blue">复用 MessageTask 发送链路</Tag>}>
        {error && <Typography.Paragraph type="danger">{error}</Typography.Paragraph>}
        <Form layout="vertical">
          <Row gutter={16}>
            <Col xs={24} lg={8}>
              <Form.Item label="发送账号">
                <Select
                  showSearch
                  allowClear
                  placeholder="搜索账号、username、手机号"
                  value={accountId}
                  onChange={(value) => { setAccountId(value); setTargetKey(undefined); }}
                  filterOption={optionFilter}
                  options={onlineAccounts.map((account) => ({
                    value: account.id,
                    label: `${account.display_name} / ${account.username || '-'} / ${account.phone_masked || ''}`,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} lg={8}>
              <Form.Item label="目标类型">
                <Radio.Group value={targetType} onChange={(event) => resetTarget(event.target.value)}>
                  <Radio.Button value="private">个人</Radio.Button>
                  <Radio.Button value="group">群聊</Radio.Button>
                  <Radio.Button value="channel">频道</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} lg={8}>
              <Form.Item label="目标">
                <Space.Compact style={{ width: '100%' }}>
                  <Select
                    showSearch
                    allowClear
                    style={{ width: '100%' }}
                    loading={loadingTargets}
                    placeholder={accountId ? `搜索${targetTypeLabels[targetType]}目标` : '先选择账号'}
                    value={targetKey}
                    onChange={setTargetKey}
                    filterOption={optionFilter}
                    disabled={!accountId}
                    options={[
                      ...targetOptions,
                      ...(manualPeer ? [{ value: 'manual', label: `${manualDisplay || manualPeer} / 手动输入` }] : []),
                    ]}
                  />
                  <Button disabled={!accountId} onClick={() => setManualOpen(true)}>手动</Button>
                </Space.Compact>
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} lg={8}>
              <Form.Item label="消息类型">
                <Radio.Group value={messageType} onChange={(event) => { setMessageType(event.target.value); setMaterialId(undefined); }}>
                  <Radio.Button value="文本">文本</Radio.Button>
                  <Radio.Button value="图片">图片</Radio.Button>
                  <Radio.Button value="表情包">表情包</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} lg={16}>
              <Form.Item label={messageType === '文本' ? '消息内容' : '配文'}>
                <Input.TextArea value={content} onChange={(event) => setContent(event.target.value)} rows={3} placeholder={messageType === '文本' ? '输入要发送的消息' : '可选，作为图片/表情包配文'} />
              </Form.Item>
            </Col>
          </Row>
          {messageType !== '文本' && (
            <Form.Item label="素材">
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  showSearch
                  allowClear
                  style={{ width: '100%' }}
                  placeholder={`选择已审核${messageType}素材`}
                  value={materialId}
                  onChange={setMaterialId}
                  filterOption={optionFilter}
                  options={materialOptions}
                />
                <Button icon={<MessageSquareText size={16} />} onClick={() => { setMaterialForm((form) => ({ ...form, material_type: messageType })); setMaterialOpen(true); }}>新增素材</Button>
              </Space.Compact>
            </Form.Item>
          )}
          <Row gutter={16}>
            <Col xs={12} md={6}>
              <Form.Item label="最小抖动秒数">
                <InputNumber min={0} value={jitterMin} onChange={(value) => setJitterMin(Number(value) || 0)} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item label="最大抖动秒数">
                <InputNumber min={0} value={jitterMax} onChange={(value) => setJitterMax(Number(value) || 0)} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item label="执行方式">
                <Radio.Group value={dispatchNow} onChange={(event) => setDispatchNow(event.target.value)}>
                  <Radio.Button value>立即</Radio.Button>
                  <Radio.Button value={false}>排队</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item label=" ">
                <Button type="primary" block icon={<Send size={16} />} onClick={openConfirm} loading={isActionPending('message-send:create')}>提交发送</Button>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Card>

      <Card
        title="最近发送记录"
        extra={(
          <Space>
            <Select allowClear placeholder="状态筛选" value={statusFilter || undefined} onChange={(value) => setStatusFilter(value || '')} style={{ width: 140 }} options={['排队中', '发送中', '已发送', '失败', '已取消'].map((status) => ({ value: status, label: status }))} />
            <Button icon={<RefreshCcw size={16} />} onClick={onRefresh}>刷新</Button>
          </Space>
        )}
      >
        <Table<MessageTask> rowKey="id" columns={columns} dataSource={recentTasks} pagination={{ pageSize: 8 }} />
      </Card>

      <Modal title="手动输入目标" open={manualOpen} onCancel={() => setManualOpen(false)} onOk={applyManualTarget} okText="选择">
        <Form layout="vertical">
          <Form.Item label="Peer ID / username">
            <Input value={manualPeer} onChange={(event) => setManualPeer(event.target.value)} placeholder="@username 或 peer id" />
          </Form.Item>
          <Form.Item label="显示名称">
            <Input value={manualDisplay} onChange={(event) => setManualDisplay(event.target.value)} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="新增素材" open={materialOpen} onCancel={() => setMaterialOpen(false)} onOk={createMaterial} confirmLoading={savingMaterial} okText="创建并选中">
        <Form layout="vertical">
          <Form.Item label="素材名称"><Input value={materialForm.title} onChange={(event) => setMaterialForm((form) => ({ ...form, title: event.target.value }))} /></Form.Item>
          <Form.Item label="素材类型">
            <Select value={materialForm.material_type} onChange={(value) => setMaterialForm((form) => ({ ...form, material_type: value }))} options={[{ value: '图片', label: '图片' }, { value: '表情包', label: '表情包' }]} />
          </Form.Item>
          <Form.Item label="URL / 内容"><Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm((form) => ({ ...form, content: event.target.value }))} rows={3} /></Form.Item>
          <Form.Item label="标签"><Input value={materialForm.tags} onChange={(event) => setMaterialForm((form) => ({ ...form, tags: event.target.value }))} /></Form.Item>
        </Form>
      </Modal>

      <Modal title="确认发送" open={confirmOpen} onCancel={() => setConfirmOpen(false)} onOk={submit} confirmLoading={isActionPending('message-send:create')} okText="确认提交">
        <Space direction="vertical">
          <Typography.Text>账号：{selectedAccount?.display_name || accountId}</Typography.Text>
          <Typography.Text>目标类型：{targetTypeLabels[targetType]}</Typography.Text>
          <Typography.Text>消息类型：{messageType}</Typography.Text>
          <Typography.Text>抖动：{jitterMin}-{Math.max(jitterMax, jitterMin)} 秒</Typography.Text>
          <Typography.Paragraph>{content || '无文本配文'}</Typography.Paragraph>
        </Space>
      </Modal>

      <Modal title={`任务 #${detailTask?.id ?? ''}`} open={Boolean(detailTask)} onCancel={() => setDetailTask(null)} footer={null}>
        {detailTask && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text>目标：{detailTask.target_display || detailTask.target_peer_id}</Typography.Text>
            <Typography.Text>状态：{detailTask.status}</Typography.Text>
            <Typography.Text>失败类型：{detailTask.failure_type || '-'}</Typography.Text>
            <Typography.Paragraph>失败详情：{detailTask.failure_detail || '-'}</Typography.Paragraph>
            <Typography.Paragraph>内容：{detailTask.content || '-'}</Typography.Paragraph>
          </Space>
        )}
      </Modal>
    </Space>
  );
}
