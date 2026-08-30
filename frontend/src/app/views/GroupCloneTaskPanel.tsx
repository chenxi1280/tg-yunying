import React from 'react';
import { Alert, Button, Descriptions, Input, Modal, Space, Table, Tabs, Tag, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type { TaskCenterTask } from '../types';
import { formatDateTime } from './taskCenterViewModel';

type Page<T> = { items: T[]; total?: number };
type SourceEvent = {
  id: string; stream_order_no: number; event_type: string; source_message_id?: number;
  sender_peer_id?: string; content?: string; observed_at?: string;
};
type Obligation = {
  id: string; stream_order_no: number; obligation_kind: string; state: string;
  planned_at?: string; sequencer_head_case_id?: string;
};
type ManualReview = {
  review_id: string; obligation_kind: string; state: string; error_code?: string;
  sequencer_id: number; revision: number;
};
type SequencerCase = {
  id: string; sequencer_id: number; case_kind: string; state: string; revision: number;
  remote_mutation_started: boolean; policy_snapshot: string; created_at?: string;
};
type Binding = {
  id: string; source_sender_peer_id: string; source_sender_name?: string;
  assigned_account_id?: number; status: string; is_vip: boolean; last_spoken_at?: string;
};
type MessageMapping = {
  id: string; source_message_id: number; target_message_id?: number;
  target_top_message_id?: number; account_id?: number; remote_confirmed_at?: string;
};
type IngressStatus = {
  state: string; stream_state?: string; owner_lease_healthy: boolean;
  subscription_state?: string; last_ingress_order_no?: number;
  last_consumed_ingress_order_no?: number; pending_delivery_count: number;
};
type CloneEvidence = {
  sources: SourceEvent[]; obligations: Obligation[]; reviews: ManualReview[];
  cases: SequencerCase[]; bindings: Binding[]; mappings: MessageMapping[];
  ingress: IngressStatus | null;
};

const EMPTY_EVIDENCE: CloneEvidence = {
  sources: [], obligations: [], reviews: [], cases: [], bindings: [], mappings: [], ingress: null,
};

interface GroupCloneTaskPanelProps {
  task: TaskCenterTask;
  canManageTasks: boolean;
  onChanged: () => void;
}

export function GroupCloneTaskPanel({ task, canManageTasks, onChanged }: GroupCloneTaskPanelProps) {
  const [evidence, setEvidence] = React.useState<CloneEvidence>(EMPTY_EVIDENCE);
  const [loading, setLoading] = React.useState(false);
  const [busyKey, setBusyKey] = React.useState('');
  const [reason, setReason] = React.useState('');
  const [notice, setNotice] = React.useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const requestIds = React.useRef(new Map<string, string>());

  const load = React.useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const prefix = `/tasks/${task.id}`;
      const [sources, obligations, reviews, cases, bindings, mappings, ingress] = await Promise.all([
        api<Page<SourceEvent>>(`${prefix}/clone-source-events?limit=50`),
        api<Page<Obligation>>(`${prefix}/clone-obligations?limit=50`),
        api<Page<ManualReview>>(`${prefix}/clone-manual-reviews?limit=50`),
        api<Page<SequencerCase>>(`${prefix}/clone-sequencer-head-cases?limit=50`),
        api<Page<Binding>>(`${prefix}/clone-bindings?limit=50`),
        api<Page<MessageMapping>>(`${prefix}/clone-message-mappings?limit=50`),
        api<IngressStatus>(`${prefix}/clone-update-ingress-status`),
      ]);
      setEvidence({
        sources: sources.items, obligations: obligations.items, reviews: reviews.items,
        cases: cases.items, bindings: bindings.items, mappings: mappings.items, ingress,
      });
    } catch (error) {
      setNotice({ type: 'error', text: error instanceof Error ? error.message : String(error) });
    } finally {
      setLoading(false);
    }
  }, [task.id]);

  React.useEffect(() => { void load(); }, [load]);

  const clientRequestId = (key: string) => {
    const existing = requestIds.current.get(key);
    if (existing) return existing;
    const value = `clone-ui-${crypto.randomUUID()}`;
    requestIds.current.set(key, value);
    return value;
  };

  const mutate = async (key: string, path: string, body: object, successText: string) => {
    setBusyKey(key);
    setNotice(null);
    try {
      await api(path, { method: 'POST', body: JSON.stringify(body) });
      requestIds.current.delete(key);
      setNotice({ type: 'success', text: successText });
      await load();
      onChanged();
    } catch (error) {
      setNotice({ type: 'error', text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusyKey('');
    }
  };

  const decideReview = (item: ManualReview, decision: 'release' | 'drop' | 'keep_blocked') => {
    const normalizedReason = reason.trim();
    if (!normalizedReason) {
      setNotice({ type: 'error', text: '人工处置必须填写原因。' });
      return;
    }
    const key = `review:${item.review_id}:${item.revision}:${decision}:${normalizedReason}`;
    const execute = () => mutate(
      key,
      `/tasks/${task.id}/clone-manual-reviews/${item.review_id}/decision`,
      {
        expected_review_revision: item.revision,
        decision,
        reason: normalizedReason,
        client_request_id: clientRequestId(key),
      },
      '人工审核决定已提交。',
    );
    if (decision !== 'drop') void execute();
    else Modal.confirm({ title: '确认丢弃此克隆义务？', content: normalizedReason, okType: 'danger', onOk: execute });
  };

  const decideCase = (item: SequencerCase, decision: 'accept_visible_gap' | 'retry_same_mutation' | 'keep_blocked') => {
    const normalizedReason = reason.trim();
    if (!normalizedReason) {
      setNotice({ type: 'error', text: 'Sequencer 决策必须填写原因。' });
      return;
    }
    const key = `case:${item.id}:${item.revision}:${decision}:${normalizedReason}`;
    void mutate(
      key,
      `/tasks/${task.id}/clone-sequencer-head-cases/${item.id}/decision`,
      {
        expected_case_revision: item.revision,
        decision,
        reason: normalizedReason,
        client_request_id: clientRequestId(key),
      },
      'Sequencer 队头决定已提交。',
    );
  };

  const rollback = async () => {
    const normalizedReason = reason.trim();
    if (!normalizedReason) {
      setNotice({ type: 'error', text: '回滚必须填写原因。' });
      return;
    }
    setBusyKey('rollback-preview');
    try {
      const preview = await api<Record<string, string | number>>(
        `/tasks/${task.id}/group-clone/rollback/preview`, { method: 'POST' },
      );
      const key = `rollback:${task.id}:${preview.preview_token}:${normalizedReason}`;
      Modal.confirm({
        title: '确认回滚到旧监听转发任务？',
        content: `旧任务 ${preview.legacy_task_id}；仅在 Clone 尚无 Gateway-started mutation 时允许。`,
        okType: 'danger',
        onOk: () => mutate(
          key,
          `/tasks/${task.id}/group-clone/rollback/apply`,
          {
            preview_token: preview.preview_token,
            clone_task_id: task.id,
            expected_authority_version: preview.expected_authority_version,
            open_action_fingerprint: preview.open_action_fingerprint,
            client_request_id: clientRequestId(key),
            reason: normalizedReason,
          },
          '已回滚到旧监听转发任务。',
        ),
      });
    } catch (error) {
      setNotice({ type: 'error', text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusyKey('');
    }
  };

  const ingress = evidence.ingress;
  const manualColumns = [
    { title: '序号', dataIndex: 'sequencer_id', width: 90 },
    { title: '类型', dataIndex: 'obligation_kind', width: 150 },
    { title: '原因码', dataIndex: 'error_code', ellipsis: true, render: (value: string) => value || '-' },
    {
      title: '操作', key: 'actions', width: 250,
      render: (_: unknown, item: ManualReview) => canManageTasks ? (
        <Space wrap>
          <Button size="small" loading={busyKey.startsWith(`review:${item.review_id}`)} onClick={() => decideReview(item, 'release')}>释放</Button>
          <Button size="small" danger onClick={() => decideReview(item, 'drop')}>丢弃</Button>
          <Button size="small" onClick={() => decideReview(item, 'keep_blocked')}>保持阻塞</Button>
        </Space>
      ) : '-',
    },
  ];
  const caseColumns = [
    { title: '序号', dataIndex: 'sequencer_id', width: 90 },
    { title: '类型', dataIndex: 'case_kind', width: 180 },
    { title: '状态', dataIndex: 'state', width: 160, render: (value: string) => <Tag>{value}</Tag> },
    { title: '远端已开始', dataIndex: 'remote_mutation_started', width: 110, render: (value: boolean) => value ? '是' : '否' },
    {
      title: '操作', key: 'actions', width: 320,
      render: (_: unknown, item: SequencerCase) => canManageTasks && item.state === 'waiting_decision' ? (
        <Space wrap>
          <Button size="small" onClick={() => decideCase(item, 'accept_visible_gap')}>接受可见缺口</Button>
          <Button size="small" onClick={() => decideCase(item, 'retry_same_mutation')}>重试原 mutation</Button>
          <Button size="small" onClick={() => decideCase(item, 'keep_blocked')}>保持阻塞</Button>
        </Space>
      ) : '-',
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {notice && <Alert type={notice.type} showIcon message={notice.text} />}
      <Alert
        type={ingress?.owner_lease_healthy ? 'success' : 'warning'}
        showIcon
        message={ingress?.owner_lease_healthy ? 'Update ingress owner lease 正常' : 'Update ingress owner lease 未证实'}
        description={`ingress=${ingress?.state || 'missing'}；subscription=${ingress?.subscription_state || 'missing'}；待投递=${ingress?.pending_delivery_count ?? 0}`}
        action={<Button size="small" loading={loading} onClick={() => void load()}>刷新证据</Button>}
      />
      <Descriptions bordered size="small" column={4} items={[
        { key: 'ingress', label: '最新 ingress', children: ingress?.last_ingress_order_no ?? '-' },
        { key: 'consumed', label: '最新消费', children: ingress?.last_consumed_ingress_order_no ?? '-' },
        { key: 'source', label: '源事件', children: evidence.sources.length },
        { key: 'obligations', label: '送达义务', children: evidence.obligations.length },
      ]} />
      {canManageTasks && (
        <Space wrap>
          <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="人工处置原因（必填）" style={{ width: 360 }} />
          {task.stats?.cutover_legacy_task_id && (
            <Button danger loading={busyKey === 'rollback-preview'} onClick={() => void rollback()}>预览并回滚到旧任务</Button>
          )}
        </Space>
      )}
      <Tabs items={[
        {
          key: 'stream', label: `源流水 / 义务 (${evidence.sources.length}/${evidence.obligations.length})`,
          children: <Space direction="vertical" style={{ width: '100%' }}>
            <Table<SourceEvent> rowKey="id" loading={loading} size="small" pagination={false} dataSource={evidence.sources} columns={[
              { title: '顺序', dataIndex: 'stream_order_no', width: 80 },
              { title: '事件', dataIndex: 'event_type', width: 140 },
              { title: '源消息', dataIndex: 'source_message_id', width: 100, render: (value) => value || '-' },
              { title: '发言人', dataIndex: 'sender_peer_id', width: 160, render: (value) => value || '-' },
              { title: '内容', dataIndex: 'content', ellipsis: true },
              { title: '观察时间', dataIndex: 'observed_at', width: 170, render: formatDateTime },
            ]} />
            <Table<Obligation> rowKey="id" size="small" pagination={false} dataSource={evidence.obligations} columns={[
              { title: '顺序', dataIndex: 'stream_order_no', width: 80 },
              { title: '义务', dataIndex: 'obligation_kind', width: 180 },
              { title: '状态', dataIndex: 'state', width: 180, render: (value) => <Tag>{value}</Tag> },
              { title: '计划时间', dataIndex: 'planned_at', width: 170, render: formatDateTime },
              { title: '队头 Case', dataIndex: 'sequencer_head_case_id', ellipsis: true, render: (value) => value || '-' },
            ]} />
          </Space>,
        },
        { key: 'manual', label: `人工审核 (${evidence.reviews.length})`, children: <Table<ManualReview> rowKey="review_id" size="small" pagination={false} dataSource={evidence.reviews} columns={manualColumns} /> },
        { key: 'cases', label: `Sequencer 队头 (${evidence.cases.length})`, children: <Table<SequencerCase> rowKey="id" size="small" pagination={false} dataSource={evidence.cases} columns={caseColumns} /> },
        {
          key: 'identity', label: '绑定 / 消息映射',
          children: <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text strong>发言人绑定</Typography.Text>
            <Table<Binding> rowKey="id" size="small" pagination={false} dataSource={evidence.bindings} columns={[
              { title: '源发言人', key: 'source', render: (_, item) => item.source_sender_name || item.source_sender_peer_id },
              { title: '受控账号', dataIndex: 'assigned_account_id', width: 120, render: (value) => value || '-' },
              { title: '状态', dataIndex: 'status', width: 120 },
              { title: 'VIP', dataIndex: 'is_vip', width: 70, render: (value) => value ? '是' : '否' },
              { title: '最近发言', dataIndex: 'last_spoken_at', width: 170, render: formatDateTime },
            ]} />
            <Typography.Text strong>消息映射</Typography.Text>
            <Table<MessageMapping> rowKey="id" size="small" pagination={false} dataSource={evidence.mappings} columns={[
              { title: '源消息', dataIndex: 'source_message_id', width: 110 },
              { title: '目标消息', dataIndex: 'target_message_id', width: 110, render: (value) => value || '-' },
              { title: 'Topic', dataIndex: 'target_top_message_id', width: 100, render: (value) => value || '-' },
              { title: '账号', dataIndex: 'account_id', width: 100, render: (value) => value || '-' },
              { title: '远端确认', dataIndex: 'remote_confirmed_at', width: 170, render: formatDateTime },
            ]} />
          </Space>,
        },
      ]} />
    </Space>
  );
}
