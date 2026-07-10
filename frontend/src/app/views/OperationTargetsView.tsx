import React from 'react';
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Form, Input, InputNumber, List, Modal, Select, Space, Switch, Tag, Typography } from 'antd';
import { MessageSquareText, RefreshCcw } from 'lucide-react';
import { api, ApiError } from '../../shared/api/client';
import type { ChannelMessage, ChannelMessageComment, ChannelMessageCommentSync, OperationTarget, OperationTargetDetail, OperationTargetMessageSync, OperationTargetsSync, TaskCenterTaskType } from '../types';
import { DetailModal, StatusBadge } from '../components/shared';
import OperationTargetManagementTable, { OperationTargetCapabilityTags } from '../components/OperationTargetManagementTable';
import { useOperationTargetManagementPage } from '../hooks/useOperationTargetManagementPage';
import { formatBeijingDateTime } from '../time';

type Props = {
  onSendToTarget: (target: OperationTarget) => void;
  onCreateTaskFromTarget: (taskType: Extract<TaskCenterTaskType, 'group_ai_chat' | 'group_relay' | 'channel_view' | 'channel_like' | 'channel_comment'>, target: OperationTarget, message?: ChannelMessage, comment?: ChannelMessageComment) => void;
  focusTarget?: { targetId: number; nonce: number } | null;
  onFocusTargetConsumed?: () => void;
  canManageMessageSending: boolean;
  canManageTargets: boolean;
  canManageTasks: boolean;
  canManageArchives: boolean;
  onOpenTargetProfile: () => void;
};

type OperationTargetFormValues = {
  target_type?: OperationTarget['target_type'];
  tg_peer_id?: string;
  title?: string;
  username?: string;
  member_count?: number;
  can_send?: boolean;
  auth_status?: string;
};


function formatDateTime(value?: string | null) {
  return formatBeijingDateTime(value);
}

function taskLabel(taskType: TaskCenterTaskType) {
  if (taskType === 'channel_view') return '浏览';
  if (taskType === 'channel_like') return '点赞';
  if (taskType === 'channel_comment') return '评论';
  if (taskType === 'group_relay') return '转发监听';
  return 'AI 活跃';
}

function commentsForMessage(comments: ChannelMessageComment[], messageId: number) {
  return comments.filter((comment) => comment.channel_message_id === messageId);
}

export default function OperationTargetsView({ onSendToTarget, onCreateTaskFromTarget, focusTarget, onFocusTargetConsumed, canManageMessageSending, canManageTargets, canManageTasks, canManageArchives, onOpenTargetProfile }: Props) {
  const { message } = AntdApp.useApp();
  const [formError, setFormError] = React.useState('');
  const targetPage = useOperationTargetManagementPage({
    focusTarget,
    onFocusTargetConsumed,
    onOpenFocusedTarget: openDetail,
    onMissingFocusedTarget: (targetId) => void message.warning(`未找到目标 #${targetId}`),
    setError: setFormError,
  });
  const targets = targetPage.targets;
  const refreshTargetsListAfterAction = targetPage.refreshAfterAction;
  const [saving, setSaving] = React.useState(false);
  const [accountPolicySaving, setAccountPolicySaving] = React.useState('');
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [syncing, setSyncing] = React.useState(false);
  const [syncingAllTargets, setSyncingAllTargets] = React.useState(false);
  const [syncingCommentMessageId, setSyncingCommentMessageId] = React.useState<number | null>(null);
  const [admissionRetrySaving, setAdmissionRetrySaving] = React.useState(false);
  const [admissionRetryOpen, setAdmissionRetryOpen] = React.useState(false);
  const [admissionRetryAccountIds, setAdmissionRetryAccountIds] = React.useState<number[]>([]);
  const [admissionRetryReason, setAdmissionRetryReason] = React.useState('');
  const [creatingArchiveId, setCreatingArchiveId] = React.useState<number | null>(null);
  const [editingTarget, setEditingTarget] = React.useState<OperationTarget | null>(null);
  const [detailTarget, setDetailTarget] = React.useState<OperationTarget | null>(null);
  const [targetDetail, setTargetDetail] = React.useState<OperationTargetDetail | null>(null);
  const [targetModalOpen, setTargetModalOpen] = React.useState(false);
  const [detailOpen, setDetailOpen] = React.useState(false);
  const [form] = Form.useForm<OperationTargetFormValues>();
  const activeTargetsSyncAllRequestSeq = React.useRef(0);
  const activeDetailTargetId = React.useRef<number | null>(null);
  const activeDetailTargetRequestSeq = React.useRef(0);
  const activeDetailTargetWriteSeq = React.useRef(0);
  const activeTargetSaveRequestRef = React.useRef({ seq: 0, signature: '' });

  function errorMessage(error: unknown) {
    if (error instanceof ApiError) return error.message;
    return error instanceof Error ? error.message : String(error);
  }

  function isActiveDetailTarget(targetId: number) {
    return activeDetailTargetId.current === targetId;
  }

  function beginTargetsSyncAllRequest() {
    activeTargetsSyncAllRequestSeq.current += 1;
    return activeTargetsSyncAllRequestSeq.current;
  }

  function isActiveTargetsSyncAllRequest(requestSeq: number) {
    return activeTargetsSyncAllRequestSeq.current === requestSeq;
  }

  function beginDetailTargetRequest(targetId: number) {
    activeDetailTargetId.current = targetId;
    activeDetailTargetRequestSeq.current += 1;
    return activeDetailTargetRequestSeq.current;
  }

  function isActiveDetailTargetRequest(targetId: number, requestSeq: number) {
    return isActiveDetailTarget(targetId) && activeDetailTargetRequestSeq.current === requestSeq;
  }

  function beginDetailTargetWrite(targetId: number) {
    activeDetailTargetId.current = targetId;
    activeDetailTargetWriteSeq.current += 1;
    return activeDetailTargetWriteSeq.current;
  }

  function isActiveDetailTargetWrite(targetId: number, requestSeq: number) {
    return isActiveDetailTarget(targetId) && activeDetailTargetWriteSeq.current === requestSeq;
  }

  function operationTargetSavePayloadSignature(targetId: number | null, values: OperationTargetFormValues) {
    return JSON.stringify({
      id: targetId,
      target_type: values.target_type ?? '',
      tg_peer_id: values.tg_peer_id ?? '',
      title: values.title ?? '',
      username: values.username ?? '',
      member_count: values.member_count ?? 0,
      can_send: values.can_send ?? true,
      auth_status: values.auth_status ?? '已授权运营',
    });
  }

  function beginTargetSaveRequest(signature: string) {
    activeTargetSaveRequestRef.current = { seq: activeTargetSaveRequestRef.current.seq + 1, signature };
    return activeTargetSaveRequestRef.current;
  }

  function currentOperationTargetSavePayloadSignature() {
    return operationTargetSavePayloadSignature(editingTarget?.id ?? null, form.getFieldsValue(true));
  }

  function isActiveTargetSaveRequest(request: { seq: number; signature: string }) {
    return activeTargetSaveRequestRef.current.seq === request.seq;
  }

  function isCurrentTargetSaveRequest(request: { seq: number; signature: string }) {
    return isActiveTargetSaveRequest(request) && currentOperationTargetSavePayloadSignature() === request.signature;
  }

  async function fetchTargetDetail(target: OperationTarget, requestSeq: number): Promise<boolean> {
    const detail = await api<OperationTargetDetail>(`/operation-targets/${target.id}/detail`);
    if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;
    setTargetDetail(detail);
    return true;
  }

  async function loadTargetDetail(target: OperationTarget): Promise<boolean> {
    const requestSeq = beginDetailTargetRequest(target.id);
    setDetailLoading(true);
    setFormError('');
    try {
      return await fetchTargetDetail(target, requestSeq);
    } catch (error) {
      if (!isActiveDetailTargetRequest(target.id, requestSeq)) return false;
      setFormError(errorMessage(error));
      return false;
    } finally {
      if (isActiveDetailTargetRequest(target.id, requestSeq)) setDetailLoading(false);
    }
  }

  async function refreshTargetDetailAfterAction(actionLabel: string, target: OperationTarget) {
    const requestSeq = beginDetailTargetRequest(target.id);
    try {
      const loaded = await fetchTargetDetail(target, requestSeq);
      if (!loaded) return;
      await refreshTargetsListAfterAction(actionLabel);
    } catch (error) {
      if (!isActiveDetailTargetRequest(target.id, requestSeq)) return;
      setFormError(`运营目标数据刷新失败：${actionLabel}操作已完成，但刷新目标详情失败：${errorMessage(error)}`);
    }
  }

  async function syncTargetMessages(target: OperationTarget) {
    const requestSeq = beginDetailTargetWrite(target.id);
    setSyncing(true);
    setFormError('');
    try {
      const result = await api<OperationTargetMessageSync>(`/operation-targets/${target.id}/sync-messages`, { method: 'POST' });
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setTargetDetail(result.detail);
      await refreshTargetsListAfterAction('目标消息同步');
    } catch (error) {
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveDetailTargetWrite(target.id, requestSeq)) setSyncing(false);
    }
  }

  async function syncMessageComments(channelMessage: ChannelMessage) {
    if (!targetDetail) return;
    const target = targetDetail.target;
    const requestSeq = beginDetailTargetWrite(target.id);
    setSyncingCommentMessageId(channelMessage.id);
    setFormError('');
    try {
      const result = await api<ChannelMessageCommentSync>(`/channel-messages/${channelMessage.id}/sync-comments`, { method: 'POST' });
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      if (result.sync_error) {
        setFormError(result.sync_error);
      }
      await refreshTargetDetailAfterAction('评论同步', target);
    } catch (error) {
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveDetailTargetWrite(target.id, requestSeq)) setSyncingCommentMessageId(null);
    }
  }

  async function syncAllTargets() {
    const requestSeq = beginTargetsSyncAllRequest();
    targetPage.setLoading(false);
    setSyncingAllTargets(true);
    setFormError('');
    try {
      const result = await api<OperationTargetsSync>('/operation-targets/sync-all', { method: 'POST' });
      if (!isActiveTargetsSyncAllRequest(requestSeq)) return;
      if (result.failed_accounts.length) {
        void message.warning(`已同步 ${result.synced_accounts} 个在线账号，${result.failed_accounts.length} 个账号失败，请查看目标详情或账号同步记录。`);
      } else {
        void message.success(`已同步 ${result.synced_accounts} 个在线账号，当前 ${result.target_count} 个群/频道目标。`);
      }
      await refreshTargetsListAfterAction('目标全量同步');
    } catch (error) {
      if (!isActiveTargetsSyncAllRequest(requestSeq)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveTargetsSyncAllRequest(requestSeq)) setSyncingAllTargets(false);
    }
  }

  async function saveTarget(values: OperationTargetFormValues) {
    const actionLabel = editingTarget ? '运营目标保存' : '运营目标新增';
    const saveRequest = beginTargetSaveRequest(operationTargetSavePayloadSignature(editingTarget?.id ?? null, values));
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
      if (!isCurrentTargetSaveRequest(saveRequest)) return;
      setEditingTarget(null);
      setTargetModalOpen(false);
      form.resetFields();
      void message.success(editingTarget ? '运营目标已保存' : '运营目标已新增');
      await refreshTargetsListAfterAction(actionLabel);
    } catch (error) {
      if (!isCurrentTargetSaveRequest(saveRequest)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveTargetSaveRequest(saveRequest)) setSaving(false);
    }
  }

  async function createArchiveFromTarget(target: OperationTarget) {
    setCreatingArchiveId(target.id);
    setFormError('');
    try {
      await api('/archives', {
        method: 'POST',
        body: JSON.stringify({
          operation_target_id: target.id,
          title: `${target.title} 内容与成员归档`,
        }),
      });
      if (!isActiveDetailTarget(target.id)) return;
      void message.success('归档任务已创建');
      await refreshTargetDetailAfterAction('归档创建', target);
    } catch (error) {
      if (!isActiveDetailTarget(target.id)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveDetailTarget(target.id)) setCreatingArchiveId(null);
    }
  }

  async function saveAccountPolicy(accountId: number, patch: { can_send?: boolean; is_listener?: boolean }) {
    if (!targetDetail) return;
    const target = targetDetail.target;
    const requestSeq = beginDetailTargetWrite(target.id);
    setAccountPolicySaving(`${accountId}:${Object.keys(patch)[0] ?? 'policy'}`);
    setFormError('');
    try {
      const detail = await api<OperationTargetDetail>(`/operation-targets/${target.id}/accounts/${accountId}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      });
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setTargetDetail(detail);
      void message.success('账号风控已保存');
      await refreshTargetsListAfterAction('账号策略保存');
    } catch (error) {
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveDetailTargetWrite(target.id, requestSeq)) setAccountPolicySaving('');
    }
  }

  function openAdmissionRetry(accountIds: number[]) {
    setAdmissionRetryAccountIds(accountIds);
    setAdmissionRetryReason('');
    setFormError('');
    setAdmissionRetryOpen(true);
  }

  async function retryAdmission() {
    if (!targetDetail) return;
    const target = targetDetail.target;
    const reason = admissionRetryReason.trim();
    if (!reason) {
      setFormError('请填写重试原因');
      return;
    }
    setAdmissionRetrySaving(true);
    setFormError('');
    const requestSeq = beginDetailTargetWrite(target.id);
    try {
      const detail = await api<OperationTargetDetail>(`/operation-targets/${target.id}/admission/retry`, {
        method: 'POST',
        body: JSON.stringify({ reason, account_ids: admissionRetryAccountIds }),
      });
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setTargetDetail(detail);
      setAdmissionRetryOpen(false);
      const retry = detail.admission_retry || {};
      if (retry.mode === 'queued') {
        void message.success(`已提交后台重查 ${retry.queued_action_count ?? retry.retried_account_count ?? admissionRetryAccountIds.length} 个账号`);
      } else {
        void message.success(`已重查 ${retry.retried_account_count ?? admissionRetryAccountIds.length} 个账号，恢复 ${retry.recovered_account_count ?? 0} 个`);
      }
      await refreshTargetsListAfterAction('准入重试');
    } catch (error) {
      if (!isActiveDetailTargetWrite(target.id, requestSeq)) return;
      setFormError(errorMessage(error));
    } finally {
      if (isActiveDetailTargetWrite(target.id, requestSeq)) setAdmissionRetrySaving(false);
    }
  }

  function startEdit(target: OperationTarget) {
    setEditingTarget(target);
    setTargetModalOpen(true);
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

  function openDetail(target: OperationTarget) {
    activeDetailTargetId.current = target.id;
    setDetailTarget(target);
    setTargetDetail(null);
    setDetailOpen(true);
    setFormError('');
    setSyncing(false);
    setSyncingCommentMessageId(null);
    setAdmissionRetrySaving(false);
    setAccountPolicySaving('');
    setCreatingArchiveId(null);
    void loadTargetDetail(target).then((loaded) => {
      if (loaded && canManageTargets) void syncTargetMessages(target);
    });
  }

  function openCreate() {
    setEditingTarget(null);
    setFormError('');
    form.resetFields();
    setTargetModalOpen(true);
  }

  function closeTargetModal() {
    setEditingTarget(null);
    setTargetModalOpen(false);
    setFormError('');
    form.resetFields();
  }

  function closeDetail() {
    activeDetailTargetId.current = null;
    setDetailOpen(false);
    setDetailTarget(null);
    setTargetDetail(null);
    setAdmissionRetryOpen(false);
    setDetailLoading(false);
    setSyncing(false);
    setSyncingCommentMessageId(null);
    setAdmissionRetrySaving(false);
    setAccountPolicySaving('');
    setCreatingArchiveId(null);
  }

  const failedAdmissionAccounts = targetDetail?.accounts.filter((account) => account.admission_status === 'failed' && account.admission_retryable) ?? [];

  return (
    <>
      <OperationTargetManagementTable
        targets={targets}
        query={targetPage.query}
        total={targetPage.total}
        search={targetPage.search}
        loading={targetPage.loading}
        error={formError}
        syncingAllTargets={syncingAllTargets}
        canManageTargets={canManageTargets}
        onSearchChange={targetPage.setSearch}
        onSearch={targetPage.submitSearch}
        onPageChange={targetPage.changePage}
        onRefresh={() => void targetPage.load()}
        onSyncAll={() => void syncAllTargets()}
        onCreate={openCreate}
        onOpenDetail={openDetail}
        onEdit={startEdit}
      />

      <Modal
        className="tg-modal medium"
        title={editingTarget ? `编辑目标 #${editingTarget.id}` : '新增目标'}
        open={targetModalOpen}
        width={640}
        footer={null}
        destroyOnHidden
        centered
        onCancel={closeTargetModal}
      >
        {formError && <Alert className="form-alert" type="error" showIcon message={formError} />}
        <Form form={form} layout="vertical" onFinish={saveTarget} initialValues={{ target_type: 'group', can_send: true, auth_status: '已授权运营', member_count: 0 }}>
          <Form.Item name="target_type" label="目标类型" rules={[{ required: true }]}>
            <Select options={[{ value: 'group', label: '群聊' }, { value: 'channel', label: '频道' }]} />
          </Form.Item>
          <Form.Item name="title" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="tg_peer_id" label="Peer ID / @username / 频道链接" rules={[{ required: true }]}>
            <Input placeholder="@channel、https://t.me/channel、https://t.me/+invite 或 -100..." />
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
            <Button onClick={closeTargetModal} disabled={saving}>取消</Button>
          </Space>
        </Form>
      </Modal>

      <DetailModal
        title={detailTarget?.title ?? '目标详情'}
        open={detailOpen}
        size="large"
        extra={detailTarget && canManageTargets ? <Button icon={<RefreshCcw size={16} />} loading={syncing || detailLoading} onClick={() => syncTargetMessages(detailTarget)}>同步最新消息</Button> : null}
        onClose={closeDetail}
      >
        {formError && <Alert className="form-alert" type="error" showIcon message={formError} />}
        {targetDetail ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {targetDetail.sync_error && <Alert type="warning" showIcon message="同步未完成" description={targetDetail.sync_error} />}
            <Descriptions
              bordered
              size="small"
              column={3}
              items={[
                { key: 'type', label: '类型', children: targetDetail.target.target_type === 'channel' ? '频道' : '群聊' },
                { key: 'auth', label: '使用范围', children: <StatusBadge status={targetDetail.target.auth_status} /> },
                { key: 'send', label: '发送能力', children: <StatusBadge status={targetDetail.target.can_send ? '可发送' : '只读'} /> },
                { key: 'task', label: '任务能力', span: 3, children: <OperationTargetCapabilityTags target={targetDetail.target} /> },
                { key: 'peer', label: 'Peer', span: 2, children: targetDetail.target.tg_peer_id },
                { key: 'username', label: 'Username', children: targetDetail.target.username ? `@${targetDetail.target.username}` : '-' },
                { key: 'members', label: '人数', children: targetDetail.target.member_count },
                { key: 'sync', label: '最近同步', span: 2, children: formatDateTime(targetDetail.target.last_sync_at) },
              ]}
            />
            <Space wrap>
              {canManageMessageSending && <Button type="primary" icon={<MessageSquareText size={16} />} onClick={() => onSendToTarget(targetDetail.target)}>去发送消息</Button>}
              {canManageTasks && targetDetail.target.target_type === 'group' && <Button onClick={() => onCreateTaskFromTarget('group_ai_chat', targetDetail.target)}>创建 AI 活跃群任务</Button>}
              {canManageTasks && targetDetail.target.target_type === 'group' && <Button onClick={() => onCreateTaskFromTarget('group_relay', targetDetail.target)}>创建转发监听任务</Button>}
              {canManageArchives && targetDetail.target.target_type === 'group' && targetDetail.target.can_archive && (
                <Button loading={creatingArchiveId === targetDetail.target.id} onClick={() => createArchiveFromTarget(targetDetail.target)}>创建归档</Button>
              )}
            </Space>
            <Card className="sub-panel compact-panel" title="风险状态">
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <StatusBadge status={targetDetail.risk.level} />
                {targetDetail.risk.messages.length ? (
                  targetDetail.risk.messages.map((item) => <Alert key={item} type="warning" showIcon message={item} />)
                ) : (
                  <Typography.Text type="secondary">当前未发现目标能力风险。</Typography.Text>
                )}
              </Space>
            </Card>
            <Card
              className="sub-panel compact-panel"
              title="目标画像来源状态"
              extra={<Button size="small" onClick={onOpenTargetProfile}>打开目标画像</Button>}
            >
              <Descriptions
                size="small"
                column={3}
                items={[
                  { key: 'scope', label: '使用范围', span: 2, children: 'AI 活群、频道评论、回复共用全站目标画像' },
                  { key: 'source', label: '来源配置', children: '在目标画像统一选择' },
                  { key: 'current', label: '当前目标', span: 3, children: targetDetail.target.target_type === 'group' ? '可作为群聊学习来源候选' : '可作为频道评论学习来源候选' },
                ]}
              />
            </Card>
            {targetDetail.target.target_type === 'group' ? (
              <Card className="sub-panel compact-panel" title="最近聊天记录">
                <List
                  dataSource={targetDetail.group_messages}
                  loading={detailLoading || syncing}
                  locale={{ emptyText: <Empty description="暂无群聊上下文，确认已配置监听账号后可同步最新消息" /> }}
                  renderItem={(message) => (
                    <List.Item>
                      <List.Item.Meta
                        title={<Space><Typography.Text strong>{message.sender_name}</Typography.Text><Typography.Text type="secondary">{formatDateTime(message.sent_at)}</Typography.Text>{message.used_for_ai && <Tag>已用于 AI</Tag>}</Space>}
                        description={message.content}
                      />
                    </List.Item>
                  )}
                />
              </Card>
            ) : (
              <Card className="sub-panel compact-panel" title="频道消息">
                <List
                  dataSource={targetDetail.channel_messages}
                  loading={detailLoading || syncing}
                  locale={{ emptyText: <Empty description="暂无频道消息，同步后可从消息行创建浏览、点赞、评论任务" /> }}
                  renderItem={(message) => {
                    const messageComments = commentsForMessage(targetDetail.channel_comments, message.id);
                    return (
                      <List.Item
                        actions={[
                          canManageMessageSending ? <Button size="small" onClick={() => onSendToTarget(targetDetail.target)}>发消息</Button> : null,
                          canManageTargets ? <Button size="small" loading={syncingCommentMessageId === message.id} onClick={() => syncMessageComments(message)}>同步评论</Button> : null,
                          canManageTasks ? <Button size="small" onClick={() => onCreateTaskFromTarget('channel_view', targetDetail.target, message)}>做{taskLabel('channel_view')}任务</Button> : null,
                          canManageTasks ? <Button size="small" onClick={() => onCreateTaskFromTarget('channel_like', targetDetail.target, message)}>做{taskLabel('channel_like')}任务</Button> : null,
                          canManageTasks ? <Button size="small" onClick={() => onCreateTaskFromTarget('channel_comment', targetDetail.target, message)}>做{taskLabel('channel_comment')}任务</Button> : null,
                        ]}
                      >
                        <List.Item.Meta
                          title={<Space><Typography.Text strong>#{message.message_id}</Typography.Text><Typography.Text type="secondary">{formatDateTime(message.published_at)}</Typography.Text></Space>}
                          description={
                            <Space direction="vertical" size={4}>
                              <Typography.Text>{message.content_preview || message.message_url || '无内容预览'}</Typography.Text>
                              <Typography.Text type="secondary">已采集评论 {messageComments.length} 条</Typography.Text>
                              {messageComments.map((comment) => (
                                <Space key={comment.id} size={8} wrap>
                                  <Typography.Text type="secondary">#{comment.comment_message_id}</Typography.Text>
                                  <Typography.Text>{comment.author_name || comment.author_username || '匿名'}</Typography.Text>
                                  <Typography.Text>{comment.content_preview || '无内容预览'}</Typography.Text>
                                  {canManageTasks && <Button size="small" icon={<MessageSquareText size={14} />} onClick={() => onCreateTaskFromTarget('channel_comment', targetDetail.target, message, comment)}>上线此评论</Button>}
                                </Space>
                              ))}
                            </Space>
                          }
                        />
                      </List.Item>
                    );
                  }}
                />
              </Card>
            )}
            {targetDetail.accounts.length > 0 && (
              <Card
                className="sub-panel compact-panel"
                title="账号覆盖"
                extra={canManageTargets && failedAdmissionAccounts.length ? <Button size="small" icon={<RefreshCcw size={14} />} loading={admissionRetrySaving} onClick={() => openAdmissionRetry(failedAdmissionAccounts.map((account) => account.id))}>重试失败准入</Button> : null}
              >
                {failedAdmissionAccounts.length > 0 && (
                  <Alert
                    className="form-alert"
                    type="warning"
                    showIcon
                    message={`有 ${failedAdmissionAccounts.length} 个账号未满足准入，需要解除限制或确认已加入后重试。`}
                  />
                )}
                <List
                  dataSource={targetDetail.accounts}
                  locale={{ emptyText: <Empty description="暂无账号覆盖" /> }}
                  renderItem={(account) => {
                    const actions = [
                      canManageTargets ? <Space key="send" size={6}><Typography.Text type="secondary">发言</Typography.Text><Switch size="small" checked={account.can_send} loading={accountPolicySaving === `${account.id}:can_send`} onChange={(checked) => saveAccountPolicy(account.id, { can_send: checked })} /></Space> : null,
                      canManageTargets ? <Space key="listener" size={6}><Typography.Text type="secondary">监听</Typography.Text><Switch size="small" checked={account.is_listener} loading={accountPolicySaving === `${account.id}:is_listener`} onChange={(checked) => saveAccountPolicy(account.id, { is_listener: checked })} /></Space> : null,
                    ];
                    if (canManageTargets && account.admission_retryable) {
                      actions.push(<Button key="admission" size="small" icon={<RefreshCcw size={14} />} loading={admissionRetrySaving} onClick={() => openAdmissionRetry([account.id])}>重试准入</Button>);
                    }
                    return (
                      <List.Item actions={actions}>
                        <List.Item.Meta
                          title={<Space><Typography.Text strong>{account.display_name}</Typography.Text><StatusBadge status={account.status} />{account.is_listener && <Tag>监听号</Tag>}<StatusBadge status={account.admission_status === 'ready' ? '准入通过' : '准入失败'} /></Space>}
                          description={`@${account.username ?? '未设置'} / ${account.permission_label || '-'} / ${account.can_send ? '可发言' : '不可发言'}${account.admission_failure_reason ? ` / 失败原因：${account.admission_failure_reason}` : ''} / 最近发送 ${formatDateTime(account.last_sent_at)}`}
                        />
                      </List.Item>
                    );
                  }}
                />
              </Card>
            )}
            <Card className="sub-panel compact-panel" title="历史任务">
              <List
                dataSource={targetDetail.task_history}
                locale={{ emptyText: <Empty description="暂无关联任务" /> }}
                renderItem={(task) => (
                  <List.Item>
                    <List.Item.Meta
                      title={<Space><Typography.Text strong>{task.name}</Typography.Text><Tag>{taskLabel(task.type)}</Tag><StatusBadge status={task.status} /></Space>}
                      description={`成功 ${task.success_count} / 失败 ${task.failure_count} / 更新时间 ${formatDateTime(task.updated_at)}`}
                    />
                  </List.Item>
                )}
              />
            </Card>
            <Card className="sub-panel compact-panel" title="发送记录">
              <List
                dataSource={targetDetail.send_records}
                locale={{ emptyText: <Empty description="暂无发送记录" /> }}
                renderItem={(record) => (
                  <List.Item>
                    <List.Item.Meta
                      title={<Space><Typography.Text strong>#{record.id}</Typography.Text><StatusBadge status={record.status} /><Typography.Text type="secondary">{formatDateTime(record.sent_at ?? record.created_at)}</Typography.Text></Space>}
                      description={record.failure_detail ? `${record.content} / 失败原因：${record.failure_detail}` : record.content}
                    />
                  </List.Item>
                )}
              />
            </Card>
            {targetDetail.target.target_type === 'group' && (
              <Card className="sub-panel compact-panel" title="归档记录">
                <List
                  dataSource={targetDetail.archive_records}
                  locale={{ emptyText: <Empty description="暂无归档记录" /> }}
                  renderItem={(archive) => (
                    <List.Item>
                      <List.Item.Meta
                        title={<Space><Typography.Text strong>{archive.title}</Typography.Text><StatusBadge status={archive.status} /></Space>}
                        description={`消息 ${archive.message_count} / 成员 ${archive.member_count} / 创建 ${formatDateTime(archive.created_at)}${archive.failure_detail ? ` / ${archive.failure_detail}` : ''}`}
                      />
                    </List.Item>
                  )}
                />
              </Card>
            )}
          </Space>
        ) : (
          <Card loading={detailLoading}>正在读取目标详情</Card>
        )}
      </DetailModal>
      <Modal
        title="重试目标准入"
        open={admissionRetryOpen}
        confirmLoading={admissionRetrySaving}
        okText="开始重试"
        cancelText="取消"
        onOk={retryAdmission}
        onCancel={() => setAdmissionRetryOpen(false)}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">将重新读取所选账号在该目标里的加入/发言能力，并写入审计记录。</Typography.Text>
          <Input.TextArea rows={3} value={admissionRetryReason} onChange={(event) => setAdmissionRetryReason(event.target.value)} placeholder="例如：管理员已解除限制，重查账号准入能力" />
          <Typography.Text type="secondary">账号数：{admissionRetryAccountIds.length}</Typography.Text>
        </Space>
      </Modal>
    </>
  );
}
