import React from 'react';
import dayjs from 'dayjs';
import { Alert, App as AntdApp, Button, Card, Col, DatePicker, Form, Input, Modal, Radio, Row, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { MessageSquareText, RefreshCcw, Send, ShieldAlert } from 'lucide-react';
import { StatusBadge } from '../components/shared';
import OperationTargetSelect from '../components/OperationTargetSelect';
import type { Account, Contact, Material, MessageSendBatchCreate, MessageSendTarget, MessageSendingPrefill, MessageTask, MessageType, OperationTarget, RiskPreflight } from '../types';
import { api, ApiError } from '../../shared/api/client';
import { formatBeijingDateTime, fromBeijingDateTimeLocalValue } from '../time';
import { mergeOperationTargets } from '../hooks/useOperationTargetOptions';

type TargetType = 'private' | 'group' | 'channel';
type Props = {
  accounts: Account[];
  materials: Material[];
  tasks: MessageTask[];
  prefill?: MessageSendingPrefill | null;
  createMessageSendTask: (payload: MessageSendBatchCreate) => Promise<MessageTask[]>;
  onCancelTask: (task: MessageTask) => Promise<void>;
  onDispatchTask: (task: MessageTask) => Promise<void>;
  onRetryTask: (task: MessageTask) => Promise<void>;
  onRefresh: () => Promise<void>;
  isActionPending: (key: string) => boolean;
  canManageMessageSending: boolean;
};

type TargetOption = {
  value: string;
  label: React.ReactNode;
  searchText: string;
  target: MessageSendTarget;
};
const accountPhone = (account: Account) => account.phone_number || account.phone_masked;
const contactPhone = (contact: Contact) => contact.phone_number || contact.phone_masked || '';

const targetTypeLabels: Record<TargetType, string> = {
  private: '个人',
  group: '群聊',
  channel: '频道',
};

const statusOptions = ['排队中', '发送中', '已发送', '失败', '已取消'].map((status) => ({ value: status, label: status }));
const messageTypeOptions: MessageType[] = ['文本', '图片', '表情包', '文件', '组合消息'];
const mediaMessageTypes = new Set<MessageType>(['图片', '表情包', '文件', '组合消息']);
const cacheBackedMessageTypes = new Set<MessageType>(['图片', '表情包', '文件']);
const emojiKindOptions = [
  { value: 'image_meme', label: '图片伪表情包' },
  { value: 'static_sticker', label: '静态 sticker' },
  { value: 'animated_sticker', label: 'animated sticker' },
  { value: 'video_sticker', label: 'video sticker' },
  { value: 'custom_emoji', label: 'custom emoji' },
];

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : '未知错误';
}

function optionFilter(input: string, option?: { label?: React.ReactNode; searchText?: string }) {
  const value = option?.searchText ?? option?.label ?? '';
  return String(value).toLowerCase().includes(input.toLowerCase());
}

function formatTime(value?: string | null) {
  return formatBeijingDateTime(value);
}

function compactText(value?: string | null) {
  return value && value.trim() ? value : '-';
}

export default function MessageSendingView({
  accounts,
  materials,
  tasks,
  prefill,
  createMessageSendTask,
  onCancelTask,
  onDispatchTask,
  onRetryTask,
  onRefresh,
  isActionPending,
  canManageMessageSending,
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
  const [operationTargets, setOperationTargets] = React.useState<OperationTarget[]>([]);
  const [localMaterials, setLocalMaterials] = React.useState<Material[]>(materials);
  const [statusFilter, setStatusFilter] = React.useState<string>('');
  const [recordAccountId, setRecordAccountId] = React.useState<number | undefined>();
  const [targetQuery, setTargetQuery] = React.useState('');
  const [contentQuery, setContentQuery] = React.useState('');
  const [manualOpen, setManualOpen] = React.useState(false);
  const [manualPeer, setManualPeer] = React.useState('');
  const [manualDisplay, setManualDisplay] = React.useState('');
  const [materialOpen, setMaterialOpen] = React.useState(false);
  const [materialForm, setMaterialForm] = React.useState({ title: '', content: '', tags: '', material_type: '图片' as MessageType, emoji_asset_kind: 'image_meme', cache_ready_status: 'not_cached' });
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [preflight, setPreflight] = React.useState<RiskPreflight | null>(null);
  const [detailTask, setDetailTask] = React.useState<MessageTask | null>(null);
  const [error, setError] = React.useState('');
  const [savingMaterial, setSavingMaterial] = React.useState(false);
  const [preflightLoading, setPreflightLoading] = React.useState(false);
  const messageContactsRequestRef = React.useRef({ accountId: undefined as number | undefined, seq: 0 });
  const preflightRequestRef = React.useRef({ seq: 0, payloadSignature: '' });
  const confirmedPreflightPayloadRef = React.useRef<{ payload: MessageSendBatchCreate | null; signature: string }>({ payload: null, signature: '' });

  function messageSendPayloadSignature(payload: MessageSendBatchCreate) {
    return JSON.stringify({
      account_id: payload.account_id,
      targets: payload.targets,
      content: payload.content,
      message_type: payload.message_type,
      material_id: payload.material_id,
      dispatch_now: payload.dispatch_now,
      scheduled_at: payload.scheduled_at,
    });
  }

  function beginPreflightRequest(payloadSignature: string) {
    const nextSeq = preflightRequestRef.current.seq + 1;
    preflightRequestRef.current = { seq: nextSeq, payloadSignature };
    return nextSeq;
  }

  function isLatestPreflightRequest(requestSeq: number) {
    return preflightRequestRef.current.seq === requestSeq;
  }

  function isCurrentPreflightRequest(requestSeq: number, payloadSignature: string) {
    if (!isLatestPreflightRequest(requestSeq)) return false;
    try {
      return messageSendPayloadSignature(buildPayload()) === payloadSignature;
    } catch {
      return false;
    }
  }

  function clearConfirmedPreflightPayload() {
    confirmedPreflightPayloadRef.current = { payload: null, signature: '' };
  }

  function setConfirmedPreflightPayload(payload: MessageSendBatchCreate, signature: string) {
    confirmedPreflightPayloadRef.current = { payload, signature };
  }

  function currentConfirmedPreflightPayload() {
    return confirmedPreflightPayloadRef.current;
  }

  function beginMessageContactsRequest(targetAccountId: number | undefined) {
    const nextSeq = messageContactsRequestRef.current.seq + 1;
    messageContactsRequestRef.current = { accountId: targetAccountId, seq: nextSeq };
    return nextSeq;
  }

  function isActiveMessageContactsRequest(targetAccountId: number | undefined, requestSeq: number) {
    return messageContactsRequestRef.current.accountId === targetAccountId && messageContactsRequestRef.current.seq === requestSeq;
  }

  React.useEffect(() => setLocalMaterials(materials), [materials]);

  React.useEffect(() => {
    if (!prefill?.target || !canManageMessageSending) return;
    const target = prefill.target;
    const key = `operation-target:${target.id}`;
    const option: TargetOption = {
      value: key,
      searchText: `${targetTypeLabels[target.target_type]} ${target.title} ${target.username || ''} ${target.tg_peer_id}`,
      label: <Space size={6}><Tag color={target.target_type === 'channel' ? 'purple' : 'green'}>{targetTypeLabels[target.target_type]}</Tag><span>{target.title}</span><Typography.Text type="secondary">{target.username || target.tg_peer_id}</Typography.Text></Space>,
      target: { target_type: target.target_type, operation_target_id: target.id, target_display: target.title },
    };
    setManualTargets((current) => [option, ...current.filter((item) => item.value !== key)]);
    setTargetKeys([key]);
    setTaskOpen(true);
    setError('');
  }, [canManageMessageSending, prefill?.nonce, prefill?.target]);

  React.useEffect(() => {
    if (!accountId) {
      beginMessageContactsRequest(undefined);
      setContacts([]);
      return;
    }
    let active = true;
    const contactRequestSeq = beginMessageContactsRequest(accountId);
    setError('');
    api<Contact[]>(`/tg-accounts/${accountId}/contacts`)
      .then((items) => {
        if (!active || !isActiveMessageContactsRequest(accountId, contactRequestSeq)) return;
        setContacts(items);
      })
      .catch((contactError: unknown) => {
        if (!active || !isActiveMessageContactsRequest(accountId, contactRequestSeq)) return;
        setContacts([]);
        setError(`读取账号联系人失败：${errorText(contactError)}`);
      });
    return () => { active = false; };
  }, [accountId]);

  React.useEffect(() => {
    function refreshMessageSendingData() {
      void onRefresh().catch((error: unknown) => {
        setError(`刷新消息发送数据失败：${errorText(error)}`);
      });
    }

    const timer = window.setInterval(refreshMessageSendingData, 60000);
    return () => window.clearInterval(timer);
  }, [onRefresh]);

  const onlineAccounts = accounts.filter((account) => account.status === '在线' && !account.deleted_at);
  const selectedAccount = onlineAccounts.find((account) => account.id === accountId);
  const selectedOperationTargetIds = React.useMemo(
    () => targetKeys
      .filter((key) => key.startsWith('operation-target:'))
      .map((key) => Number(key.split(':')[1]))
      .filter((id) => Number.isSafeInteger(id) && id > 0),
    [targetKeys],
  );
  const mergeLoadedTargets = React.useCallback((loadedTargets: readonly OperationTarget[]) => {
    setOperationTargets((current) => mergeOperationTargets(current, loadedTargets));
  }, []);
  const updateOperationTargetIds = React.useCallback((value: number | number[] | undefined) => {
    const ids = Array.isArray(value) ? value : value ? [value] : [];
    setTargetKeys((current) => [
      ...current.filter((key) => !key.startsWith('operation-target:')),
      ...ids.map((id) => `operation-target:${id}`),
    ]);
  }, []);
  const accountLabel = React.useCallback((id?: number | null) => {
    const account = accounts.find((item) => item.id === id);
    return account ? `${account.display_name}${account.username ? ` / @${account.username}` : ''}` : '-';
  }, [accounts]);

  const materialOptions = localMaterials
    .filter((material) => material.review_status === '已审核' && material.material_type === messageType && (!cacheBackedMessageTypes.has(messageType) || material.cache_ready_status === 'ready'))
    .map((material) => ({
      value: material.id,
      label: `${material.title} / ${material.cache_ready_status || 'ready'} / ${material.tags || material.content}`,
      searchText: `${material.title} ${material.content} ${material.tags} ${material.cache_ready_status}`,
    }));

  const targetOptions = React.useMemo<TargetOption[]>(() => {
    const privateOptions = contacts.map((contact) => {
      const peer = contact.username ? `@${contact.username}` : contact.peer_id;
      const title = contact.display_name || peer;
      const phone = contactPhone(contact);
      return {
        value: `private:${peer}`,
        searchText: `${title} ${contact.username || ''} ${contact.peer_id} ${phone}`,
        label: <Space size={6}><Tag color="blue">个人</Tag><span>{title}</span><Typography.Text type="secondary">{contact.username || contact.peer_id} {phone ? `/ ${phone}` : ''}</Typography.Text></Space>,
        target: { target_type: 'private', target_peer_id: peer, target_display: title },
      } satisfies TargetOption;
    });
    const groupOptions = operationTargets
      .filter((target) => target.target_type === 'group' && target.can_send && target.auth_status === '已授权运营')
      .map((target) => ({
        value: `operation-target:${target.id}`,
        searchText: `群聊 ${target.title} ${target.username || ''} ${target.tg_peer_id}`,
        label: (
          <Space size={6}>
            <Tag color="green">运营目标</Tag>
            <span>{target.title}</span>
            <Typography.Text type="secondary">可发账号 {target.available_send_account_count}</Typography.Text>
          </Space>
        ),
        target: { target_type: 'group', operation_target_id: target.id, target_display: target.title },
      } satisfies TargetOption));
    const channelOptions = operationTargets
      .filter((target) => target.target_type === 'channel' && target.can_send && target.auth_status === '已授权运营')
      .map((target) => ({
        value: `operation-target:${target.id}`,
        searchText: `${target.title} ${target.username || ''} ${target.tg_peer_id}`,
        label: <Space size={6}><Tag color="purple">运营目标</Tag><span>{target.title}</span><Typography.Text type="secondary">{target.username || target.tg_peer_id}</Typography.Text></Space>,
        target: { target_type: 'channel', operation_target_id: target.id, target_display: target.title },
      } satisfies TargetOption));
    const builtinValues = new Set([...privateOptions, ...groupOptions, ...channelOptions].map((option) => option.value));
    return [...privateOptions, ...groupOptions, ...channelOptions, ...manualTargets.filter((option) => !builtinValues.has(option.value))];
  }, [contacts, manualTargets, operationTargets]);

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
    setPreflight(null);
    clearConfirmedPreflightPayload();
    setError('');
  }

  function buildPayload(): MessageSendBatchCreate {
    if (!accountId) throw new Error('请选择发送账号');
    if (!selectedTargets.length) throw new Error('请选择发送目标');
    if (messageType === '文本' && !content.trim()) throw new Error('请输入消息内容');
    if (mediaMessageTypes.has(messageType) && !materialId) throw new Error(`请选择${messageType}素材`);
    if (!dispatchNow && !scheduledAt) throw new Error('请选择定时发送时间');
    return {
      account_id: accountId,
      targets: selectedTargets.map((option) => option.target),
      content: content.trim(),
      message_type: messageType,
      material_id: mediaMessageTypes.has(messageType) ? materialId ?? null : null,
      dispatch_now: dispatchNow,
      scheduled_at: dispatchNow ? null : scheduledAt,
    };
  }

  async function openConfirm() {
    let requestSeq = 0;
    let payloadSignature = '';
    try {
      const payload = buildPayload();
      payloadSignature = messageSendPayloadSignature(payload);
      requestSeq = beginPreflightRequest(payloadSignature);
      setError('');
      setPreflightLoading(true);
      const result = await api<RiskPreflight>('/risk-control/preflight', {
        method: 'POST',
        body: JSON.stringify({
          scenario: 'manual_send',
          account_ids: [payload.account_id],
          proxy_ids: selectedAccount?.proxy_id ? [selectedAccount.proxy_id] : [],
          target_ids: selectedTargets.map((option) => option.target.operation_target_id).filter((id): id is number => Boolean(id)),
          content_preview: payload.content,
          task_type: 'message_send',
          scheduled_at: payload.scheduled_at ?? null,
        }),
      });
      if (!isCurrentPreflightRequest(requestSeq, payloadSignature)) return;
      setPreflight(result);
      if (result.decision === 'block') {
        const reason = [...result.suggested_actions, ...result.decision_reasons].filter(Boolean).join('；') || '风控预检未通过';
        throw new Error(reason);
      }
      setConfirmedPreflightPayload(payload, payloadSignature);
      setConfirmOpen(true);
    } catch (validationError) {
      if (requestSeq && !isCurrentPreflightRequest(requestSeq, payloadSignature)) return;
      const nextError = errorText(validationError);
      setError(nextError);
      void message.error(nextError);
    } finally {
      if (!requestSeq || isLatestPreflightRequest(requestSeq)) setPreflightLoading(false);
    }
  }

  async function submit() {
    try {
      const currentPayload = buildPayload();
      const confirmed = currentConfirmedPreflightPayload();
      if (!confirmed.payload || messageSendPayloadSignature(currentPayload) !== confirmed.signature) {
        const nextError = '发送内容已变化，请重新进行风控预检';
        setError(nextError);
        void message.error(nextError);
        return;
      }
      const created = await createMessageSendTask(confirmed.payload);
      void message.success(`已创建 ${created.length} 条发送任务`);
      setConfirmOpen(false);
      setTaskOpen(false);
      clearConfirmedPreflightPayload();
      resetComposer();
    } catch (submitError) {
      const nextError = errorText(submitError);
      setError(nextError);
    }
  }

  async function createMaterial() {
    if (!materialForm.title.trim() || !materialForm.content.trim()) {
      setError('请输入素材名称和来源 URL / 缓存引用');
      void message.error('请输入素材名称和来源 URL / 缓存引用');
      return;
    }
    setSavingMaterial(true);
    try {
      let created: Material;
      try {
        created = await api<Material>('/materials', {
          method: 'POST',
          body: JSON.stringify(materialForm),
        });
      } catch (materialError) {
        const nextError = `创建素材失败：${errorText(materialError)}`;
        setError(nextError);
        void message.error(nextError);
        return;
      }
      setLocalMaterials((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      if (created.cache_ready_status === 'ready') {
        setMaterialId(created.id);
      }
      setMaterialOpen(false);
      setMaterialForm({ title: '', content: '', tags: '', material_type: messageType, emoji_asset_kind: messageType === '表情包' ? 'image_meme' : '', cache_ready_status: 'not_cached' });
      if (created.cache_ready_status !== 'ready') {
        void message.info('素材已创建，等待缓存就绪后才能用于发送');
      }
      try {
        await onRefresh();
      } catch (error) {
        const refreshError = `刷新消息发送数据失败：${errorText(error)}`;
        setError(refreshError);
        void message.error(refreshError);
      }
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
    const manualTargetType: TargetType = 'private';
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
    {
      title: '媒体',
      dataIndex: 'message_type',
      width: 140,
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <span>{task.message_type}</span>
          {mediaMessageTypes.has(task.message_type as MessageType) && (
            <Typography.Text type={task.media_sent ? 'success' : task.status === '失败' ? 'danger' : 'secondary'}>
              {task.media_sent ? '图/文件已发' : task.status === '失败' ? (task.media_failure_reason || '媒体失败') : '待发送'}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    { title: '失败原因', dataIndex: 'failure_detail', width: 180, render: (_, task) => task.status === '失败' ? (task.failure_detail || task.failure_type || task.media_failure_reason || '发送失败') : '-' },
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
          {task.operation_issue_rolled_up && <Tag color={task.operation_issue_status === 'open' ? 'red' : 'green'}>{task.operation_issue_status === 'open' ? '已上卷异常' : '异常已处理'}</Tag>}
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
            {canManageMessageSending && (
              <>
                <Button size="small" icon={<Send size={14} />} disabled={task.status === '已发送'} loading={isActionPending(`task:${task.id}:dispatch`)} onClick={() => onDispatchTask(task)}>立即执行</Button>
                <Button size="small" icon={<RefreshCcw size={14} />} disabled={!['失败', '已取消'].includes(task.status)} loading={isActionPending(`task:${task.id}:retry`)} onClick={() => onRetryTask(task)}>重试</Button>
                <Button size="small" danger icon={<ShieldAlert size={14} />} disabled={!cancellable} loading={isActionPending(`task:${task.id}:cancel`)} onClick={() => onCancelTask(task)}>取消</Button>
              </>
            )}
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
            {canManageMessageSending && <Button type="primary" icon={<MessageSquareText size={16} />} onClick={() => setTaskOpen(true)}>新建发送</Button>}
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
                    label: `${account.display_name} / ${account.username || '-'} / ${accountPhone(account) || ''}`,
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
                    setTargetKeys((current) => current.filter((key) => key.startsWith('manual:')));
                    setManualTargets((current) => current.filter((option) => option.value.startsWith('manual:')));
                    setOperationTargets([]);
                  }}
                  filterOption={optionFilter}
                  options={onlineAccounts.map((account) => ({
                    value: account.id,
                    label: `${account.display_name} / ${account.username || '-'} / ${accountPhone(account) || ''}`,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={14}>
              <Form.Item label="发送目标">
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Select
                    mode="multiple"
                    showSearch
                    allowClear
                    style={{ width: '100%' }}
                    placeholder={accountId ? '搜索并多选联系人或手动个人目标' : '先选择账号'}
                    value={targetKeys.filter((key) => !key.startsWith('operation-target:'))}
                    onChange={(keys) => setTargetKeys((current) => [
                      ...current.filter((key) => key.startsWith('operation-target:')),
                      ...keys,
                    ])}
                    filterOption={optionFilter}
                    disabled={!accountId}
                    options={targetOptions.filter((option) => !option.value.startsWith('operation-target:'))}
                  />
                  {accountId && <OperationTargetSelect
                    mode="multiple"
                    allowClear
                    style={{ width: '100%' }}
                    placeholder="远程搜索并多选可发送群聊、频道"
                    value={selectedOperationTargetIds}
                    query={{ accountId, capability: 'send' }}
                    onChange={updateOperationTargetIds}
                    onTargetsLoaded={mergeLoadedTargets}
                  />}
                  <Button disabled={!accountId} onClick={() => setManualOpen(true)}>手动</Button>
                </Space>
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item label="消息类型">
                <Radio.Group value={messageType} onChange={(event) => { setMessageType(event.target.value); setMaterialId(undefined); }}>
                  {messageTypeOptions.map((type) => <Radio.Button key={type} value={type}>{type}</Radio.Button>)}
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col xs={24} md={16}>
              <Form.Item label={messageType === '文本' ? '消息内容' : '配文'}>
                <Input.TextArea value={content} onChange={(event) => setContent(event.target.value)} rows={3} placeholder={messageType === '文本' ? '输入要发送的消息' : '可选，作为媒体配文'} />
              </Form.Item>
            </Col>
          </Row>
          {mediaMessageTypes.has(messageType) && (
            <Form.Item label={`${messageType}素材`}>
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  showSearch
                  allowClear
                  style={{ width: '100%' }}
                  placeholder={`选择可用${messageType}素材`}
                  value={materialId}
                  onChange={setMaterialId}
                  filterOption={optionFilter}
                  options={materialOptions}
                />
                <Button icon={<MessageSquareText size={16} />} onClick={() => { setMaterialForm((form) => ({ ...form, material_type: messageType, emoji_asset_kind: messageType === '表情包' ? 'image_meme' : '' })); setMaterialOpen(true); }}>新增素材</Button>
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
                    onChange={(value) => setScheduledAt(value ? fromBeijingDateTimeLocalValue(value.format('YYYY-MM-DDTHH:mm:ss')) : null)}
                    placeholder="选择开始发送时间"
                  />
                </Form.Item>
              </Col>
            )}
            <Col xs={24} md={dispatchNow ? 16 : 6}>
              <Form.Item label=" ">
                <Button type="primary" block icon={<Send size={16} />} onClick={() => void openConfirm()} loading={preflightLoading || isActionPending('message-send:create')}>提交发送</Button>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      <Modal title="手动输入目标" open={manualOpen} onCancel={() => setManualOpen(false)} onOk={applyManualTarget} okText="选择">
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="手动输入只用于个人目标；群聊和频道请从已授权运营目标中选择。"
        />
        <Form layout="vertical">
          <Form.Item label="Peer ID / username">
            <Input value={manualPeer} onChange={(event) => setManualPeer(event.target.value)} placeholder="@username 或 peer id" />
          </Form.Item>
          <Form.Item label="显示名称">
            <Input value={manualDisplay} onChange={(event) => setManualDisplay(event.target.value)} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="新增媒体素材" open={materialOpen} onCancel={() => setMaterialOpen(false)} onOk={createMaterial} confirmLoading={savingMaterial} okText="创建并选中">
        <Form layout="vertical">
          <Form.Item label="素材名称"><Input value={materialForm.title} onChange={(event) => setMaterialForm((form) => ({ ...form, title: event.target.value }))} /></Form.Item>
          <Form.Item label="素材类型">
            <Select value={materialForm.material_type} onChange={(value) => setMaterialForm((form) => ({ ...form, material_type: value, emoji_asset_kind: value === '表情包' ? 'image_meme' : '' }))} options={messageTypeOptions.filter((type) => type !== '文本').map((value) => ({ value, label: value }))} />
          </Form.Item>
          {materialForm.material_type === '表情包' && (
            <Form.Item label="表情包子类型">
              <Select value={materialForm.emoji_asset_kind} onChange={(value) => setMaterialForm((form) => ({ ...form, emoji_asset_kind: value }))} options={emojiKindOptions} />
            </Form.Item>
          )}
          <Form.Item label="来源 URL / TG 缓存引用"><Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm((form) => ({ ...form, content: event.target.value }))} rows={3} placeholder="支持外部可恢复 URL，或后续由缓存队列写入 TG 缓存引用" /></Form.Item>
          <Form.Item label="标签"><Input value={materialForm.tags} onChange={(event) => setMaterialForm((form) => ({ ...form, tags: event.target.value }))} /></Form.Item>
        </Form>
      </Modal>

      <Modal title="确认发送" open={confirmOpen} onCancel={() => { setConfirmOpen(false); clearConfirmedPreflightPayload(); }} onOk={submit} confirmLoading={isActionPending('message-send:create')} okText="确认提交">
        <Space direction="vertical" style={{ width: '100%' }}>
          {preflight && (
            <Alert
              type={preflight.decision === 'allow' ? 'success' : 'warning'}
              showIcon
              message={preflight.decision === 'allow' ? '风控预检通过' : '风控预检提示'}
              description={(
                <Space direction="vertical" size={2}>
                  <Typography.Text>可用账号 {preflight.available_accounts.length} 个，受限账号 {preflight.limited_accounts.length} 个，阻塞账号 {preflight.blocked_accounts.length} 个</Typography.Text>
                  {[...preflight.proxy_warnings, ...preflight.target_warnings, ...preflight.content_warnings, ...preflight.suggested_actions].filter(Boolean).slice(0, 6).map((item) => (
                    <Typography.Text key={item} type="secondary">{item}</Typography.Text>
                  ))}
                </Space>
              )}
            />
          )}
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
