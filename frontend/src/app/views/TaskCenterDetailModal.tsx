import React from 'react';
import { Button, Descriptions, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TaskCenterAction, TaskCenterDetail, TaskCenterTask } from '../types';
import { DetailModal, StatusBadge } from '../components/shared';
import { parseBeijingDate } from '../time';
import { API_ORIGIN } from '../../shared/api/client';
import { TYPE_LABEL, formatDateTime, statusLabel } from './taskCenterViewModel';

type DetailProfile = {
  hour: number;
  intensity: number;
  mode: string;
} | null;

interface TaskCenterDetailModalProps {
  detail: TaskCenterDetail | null;
  canManageTasks: boolean;
  supportLoading: boolean;
  plannedActions: TaskCenterAction[];
  executedActions: TaskCenterAction[];
  detailProfile: DetailProfile;
  detailPlannedTotal: number;
  aiGenerationColumns: ColumnsType<TaskCenterDetail['ai_generation_records'][number]>;
  aiAccountProfileColumns: ColumnsType<TaskCenterDetail['ai_account_profiles'][number]>;
  aiCycleColumns: ColumnsType<TaskCenterDetail['ai_cycles'][number]>;
  aiTurnColumns: ColumnsType<TaskCenterDetail['ai_cycles'][number]['turns'][number]>;
  relayBatchColumns: ColumnsType<TaskCenterDetail['relay_batches'][number]>;
  relayItemColumns: ColumnsType<TaskCenterDetail['relay_batches'][number]['items'][number]>;
  onBlockRelaySource: (source: TaskCenterDetail['recent_relay_sources'][number]) => void;
  messageColumns: ColumnsType<TaskCenterDetail['message_groups'][number]>;
  planColumns: ColumnsType<TaskCenterAction>;
  recordColumns: ColumnsType<TaskCenterAction>;
  onEditTask: (task: TaskCenterTask) => void;
  onRefreshTask: (task: TaskCenterTask) => void;
  onClose: () => void;
}

function DetailStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={statusLabel(status)} />;
}

function mediaUrl(value?: string | null) {
  if (!value) return '';
  return value.startsWith('http') ? value : `${API_ORIGIN}${value}`;
}

export function TaskCenterDetailModal({
  detail,
  canManageTasks,
  supportLoading,
  plannedActions,
  executedActions,
  detailProfile,
  detailPlannedTotal,
  aiGenerationColumns,
  aiAccountProfileColumns,
  aiCycleColumns,
  aiTurnColumns,
  relayBatchColumns,
  relayItemColumns,
  onBlockRelaySource,
  messageColumns,
  planColumns,
  recordColumns,
  onEditTask,
  onRefreshTask,
  onClose,
}: TaskCenterDetailModalProps) {
  const summaryUpdatedAt = detail?.task_runtime_summary?.updated_at ?? null;
  const summaryUpdatedAtDate = parseBeijingDate(summaryUpdatedAt);
  const summaryStale = Boolean(summaryUpdatedAtDate && Date.now() - summaryUpdatedAtDate.getTime() > 15 * 60 * 1000);
  const relaySourceColumns: ColumnsType<TaskCenterDetail['recent_relay_sources'][number]> = [
    { title: '源群', dataIndex: 'source_group_title', width: 180, ellipsis: true, render: (value) => value || '-' },
    {
      title: '发送人',
      key: 'sender',
      width: 220,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.sender_name || '未知来源'}</Typography.Text>
          <Typography.Text type="secondary">{item.sender_username ? `@${item.sender_username.replace(/^@+/, '')}` : item.sender_peer_id || '-'}</Typography.Text>
        </Space>
      ),
    },
    { title: '身份', key: 'role', width: 110, render: (_, item) => <Tag>{item.is_bot ? '机器人' : item.sender_role === 'owner' ? '群主' : item.sender_role === 'admin' ? '管理员' : item.sender_role === 'unknown' ? '未知身份' : '普通成员'}</Tag> },
    { title: '过滤命中', dataIndex: 'source_filter_reason', width: 180, ellipsis: true, render: (value) => value || '-' },
    { title: '最近消息', dataIndex: 'content', ellipsis: true },
    { title: '时间', dataIndex: 'sent_at', width: 170, render: (value) => formatDateTime(value) },
    { title: '操作', key: 'action', width: 150, render: (_, item) => canManageTasks ? <Button size="small" onClick={() => onBlockRelaySource(item)}>加入不转发名单</Button> : '-' },
  ];
  const membershipColumns: ColumnsType<Record<string, any>> = [
    { title: '账号', key: 'account', width: 180, render: (_, item) => <Space direction="vertical" size={0}><Typography.Text strong>{item.display_name || `账号 #${item.account_id}`}</Typography.Text><Typography.Text type="secondary">{item.username ? `@${item.username}` : '-'}</Typography.Text></Space> },
    { title: '状态', dataIndex: 'membership_status', width: 150, render: (value) => <Tag>{value || '-'}</Tag> },
    { title: '目标', dataIndex: 'target_display', width: 180, ellipsis: true },
    { title: '失败原因', dataIndex: 'failure_reason', ellipsis: true, render: (value) => value || '-' },
    { title: '计划时间', dataIndex: 'scheduled_at', width: 170, render: (value) => formatDateTime(value) },
    { title: '完成时间', dataIndex: 'completed_at', width: 170, render: (value) => formatDateTime(value) },
  ];
  const profileBatchItems = detail?.profile_batch?.items ?? [];
  const profileBatchColumns: ColumnsType<typeof profileBatchItems[number]> = [
    {
      title: '账号',
      key: 'account',
      width: 210,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.display_name || `账号 #${item.account_id}`}</Typography.Text>
          <Typography.Text type="secondary">{item.phone_number || `#${item.account_id}`}</Typography.Text>
        </Space>
      ),
    },
    { title: '资料', dataIndex: 'profile_status', width: 100, render: (value) => <DetailStatusBadge status={value} /> },
    { title: 'Username', dataIndex: 'username_status', width: 110, render: (value) => <DetailStatusBadge status={value} /> },
    { title: '头像', dataIndex: 'avatar_status', width: 120, render: (value) => <DetailStatusBadge status={value} /> },
    { title: '缓存', dataIndex: 'avatar_cache_status', width: 120, render: (value) => value ? <Tag>{value}</Tag> : '-' },
    {
      title: '头像回显',
      dataIndex: 'avatar_preview_url',
      width: 110,
      render: (value) => value ? <img alt="账号头像" src={mediaUrl(value)} style={{ width: 36, height: 36, borderRadius: 4, objectFit: 'cover' }} /> : '-',
    },
    { title: '失败原因', key: 'failure', ellipsis: true, render: (_, item) => item.failure_detail || item.failure_type || '-' },
  ];
  return (
    <DetailModal
      title={detail?.task.name ?? '任务详情'}
      open={Boolean(detail)}
      size="wide"
      extra={detail && (
        <Space>
          {canManageTasks && <Button loading={supportLoading} onClick={() => onEditTask(detail.task)}>编辑任务</Button>}
          <Button onClick={() => onRefreshTask(detail.task)}>刷新</Button>
        </Space>
      )}
      onClose={onClose}
    >
      {detail && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions
            bordered
            column={3}
            size="small"
            items={[
              { key: 'type', label: '类型', children: TYPE_LABEL[detail.task.type] ?? detail.task.type },
              { key: 'status', label: '状态', children: <DetailStatusBadge status={detail.task.status} /> },
              { key: 'target', label: '目标', children: detail.task.target_summary || '-' },
              { key: 'planned', label: '计划中', children: plannedActions.filter((action) => action.status === 'pending').length },
              { key: 'executing', label: '执行中', children: detail.actions.filter((action) => action.status === 'executing').length },
              { key: 'success', label: '已成功', children: detail.stats.success_count ?? 0 },
              { key: 'failure', label: '失败', children: detail.stats.failure_count ?? 0 },
              { key: 'skipped', label: '跳过', children: detail.stats.skipped_count ?? 0 },
              { key: 'total', label: '总动作', children: detail.stats.total_actions ?? 0 },
              { key: 'curve-now', label: '当前曲线', children: detailProfile ? `${String(detailProfile.hour).padStart(2, '0')}:00 强度 ${detailProfile.intensity}，${detailProfile.mode}运行` : '-' },
              { key: 'curve-gap', label: '原因分解', children: `计划 ${detailPlannedTotal}，成功 ${detail.stats.success_count ?? 0}，失败 ${detail.stats.failure_count ?? 0}，跳过 ${detail.stats.skipped_count ?? 0}，待执行 ${plannedActions.length}` },
              { key: 'next', label: '下次运行', children: formatDateTime(detail.task.next_run_at) },
              { key: 'summary-updated', label: '汇总更新', children: summaryUpdatedAt ? formatDateTime(summaryUpdatedAt) : '-' },
              { key: 'summary-state', label: '汇总状态', children: !summaryUpdatedAt ? <Tag>暂无汇总</Tag> : summaryStale ? <Tag color="gold">可能延迟</Tag> : <Tag color="green">正常</Tag> },
              { key: 'plan-link', label: '来源方案', children: detail.operation_plan_links?.length ? detail.operation_plan_links.map((item) => `#${item.plan_id}`).join('、') : '-' },
              ...(detail.task.type === 'group_ai_chat' ? [
                { key: 'idle-enabled', label: '无人发言续聊', children: detail.task.type_config?.idle_continuation_enabled === false ? '关闭' : '开启' },
                { key: 'idle-seconds', label: '续聊间隔', children: `${detail.task.type_config?.idle_continuation_seconds ?? 300} 秒` },
                { key: 'context-mode', label: '上下文状态', children: detail.task.stats?.context_mode || '-' },
              ] : []),
              { key: 'capacity', label: '容量提示', span: 3, children: detail.stats.capacity_warning ? `${detail.stats.capacity_warning} 该提示不会停止任务。` : '无' },
              { key: 'mode', label: '执行口径', span: 3, children: detail.task.type === 'group_ai_chat' || detail.task.type === 'group_relay' ? '自动校验通过后自动发送，无需人工确认。' : '按频道消息生成动作子任务，执行项按账号留痕。' },
              { key: 'error', label: '错误', span: 3, children: detail.task.last_error || '无' },
            ]}
          />
          {detail.profile_batch && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>资料初始化进度</Typography.Title>
              <Descriptions
                bordered
                size="small"
                column={5}
                items={[
                  { key: 'batch', label: '批次', children: `#${detail.profile_batch.batch_id}` },
                  { key: 'batch_status', label: '批次状态', children: <DetailStatusBadge status={detail.profile_batch.batch_status} /> },
                  { key: 'ready', label: '头像已缓存', children: detail.profile_batch.avatar_cache?.ready ?? 0 },
                  { key: 'waiting', label: '等待缓存', children: detail.profile_batch.avatar_cache?.waiting ?? 0 },
                  { key: 'failed', label: '缓存失败', children: detail.profile_batch.avatar_cache?.failed ?? 0 },
                ]}
              />
              <Table
                rowKey={(item) => `${item.account_id}:${item.avatar_source || 'profile'}`}
                columns={profileBatchColumns}
                dataSource={profileBatchItems}
                pagination={{ pageSize: 8 }}
                size="small"
                scroll={{ x: 1050 }}
              />
            </Space>
          )}
          {(detail.membership_phase?.stage || detail.membership_accounts.length > 0) && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>准入前置</Typography.Title>
              <Descriptions
                bordered
                size="small"
                column={4}
                items={[
                  { key: 'stage', label: '状态', children: detail.membership_phase?.stage || 'not_required' },
                  { key: 'status', label: '子任务状态', children: detail.membership_phase?.status || detail.membership_phase?.stage || 'not_required' },
                  { key: 'progress', label: '预计进度', children: `${detail.membership_phase?.progress_percent ?? 0}%` },
                  { key: 'phase', label: '当前阶段', children: detail.membership_phase?.current_phase || '-' },
                  { key: 'ready', label: '已满足', children: detail.membership_phase?.ready_account_count ?? detail.membership_phase?.joined_count ?? 0 },
                  { key: 'pending', label: '待准备', children: detail.membership_phase?.pending_account_count ?? detail.membership_phase?.need_join_count ?? 0 },
                  { key: 'failed', label: '失败', children: detail.membership_phase?.failed_account_count ?? detail.membership_phase?.failed_count ?? 0 },
                  { key: 'running', label: '执行中', children: detail.membership_phase?.running_account_count ?? detail.membership_phase?.running_count ?? detail.membership_phase?.summary?.running_account_count ?? 0 },
                  { key: 'success', label: '成功/跳过', children: detail.membership_phase?.success_account_count ?? detail.membership_phase?.success_count ?? detail.membership_phase?.summary?.success_account_count ?? 0 },
                  { key: 'blocked', label: '不可准备', children: detail.membership_phase?.blocked_account_count ?? 0 },
                  { key: 'targets', label: '目标数', children: detail.membership_phase?.summary?.target_count ?? '-' },
                  { key: 'eta', label: '预计完成', children: formatDateTime(detail.membership_phase?.estimated_finish_at || detail.membership_phase?.summary?.estimated_finish_at) || '-' },
                ]}
              />
              <Table<Record<string, any>> rowKey={(item) => `${item.account_id}:${item.scheduled_at || ''}`} columns={membershipColumns} dataSource={detail.membership_accounts} pagination={{ pageSize: 6 }} size="small" scroll={{ x: 1050 }} />
            </Space>
          )}
          {detail.ai_cycles.length > 0 && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>AI 活跃循环 Cycle / Turn</Typography.Title>
              {detail.ai_generation_records.length > 0 && <Table rowKey="generation_id" columns={aiGenerationColumns} dataSource={detail.ai_generation_records} pagination={false} size="small" scroll={{ x: 950 }} />}
              {detail.ai_account_profiles.length > 0 && <Table rowKey="account_id" columns={aiAccountProfileColumns} dataSource={detail.ai_account_profiles} pagination={false} size="small" scroll={{ x: 900 }} />}
              <Table<TaskCenterDetail['ai_cycles'][number]>
                rowKey="cycle_id"
                columns={aiCycleColumns}
                dataSource={detail.ai_cycles}
                pagination={false}
                scroll={{ x: 820 }}
                expandable={{
                  expandedRowRender: (item) => <Table rowKey="action_id" columns={aiTurnColumns} dataSource={item.turns} pagination={false} size="small" scroll={{ x: 1540 }} />,
                }}
              />
            </Space>
          )}
          {detail.relay_batches.length > 0 && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>转发监听批次 / 源事件</Typography.Title>
              <Table<TaskCenterDetail['relay_batches'][number]>
                rowKey="relay_batch_id"
                columns={relayBatchColumns}
                dataSource={detail.relay_batches}
                pagination={false}
                scroll={{ x: 820 }}
                expandable={{
                  expandedRowRender: (item) => <Table rowKey="action_id" columns={relayItemColumns} dataSource={item.items} pagination={false} size="small" scroll={{ x: 2600 }} />,
                }}
              />
            </Space>
          )}
          {detail.task.type === 'group_relay' && detail.recent_relay_sources.length > 0 && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>最近来源发言人</Typography.Title>
              <Table<TaskCenterDetail['recent_relay_sources'][number]>
                rowKey={(item) => `${item.source_group_id ?? 'source'}:${item.remote_message_id || item.sender_peer_id}`}
                columns={relaySourceColumns}
                dataSource={detail.recent_relay_sources}
                pagination={{ pageSize: 6 }}
                size="small"
                scroll={{ x: 1020 }}
              />
            </Space>
          )}
          {detail.message_groups.length > 0 && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Title level={5} style={{ margin: 0 }}>频道消息执行明细</Typography.Title>
              <Table<TaskCenterDetail['message_groups'][number]>
                rowKey={(item) => `${item.channel_target_id ?? 'channel'}:${item.message_id ?? 'message'}`}
                columns={messageColumns}
                dataSource={detail.message_groups}
                pagination={{ pageSize: 6 }}
                scroll={{ x: 1680 }}
                expandable={{
                  expandedRowRender: (item) => <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={item.actions} pagination={false} size="small" scroll={{ x: 1680 }} />,
                }}
              />
            </Space>
          )}
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>执行计划</Typography.Title>
            <Table<TaskCenterAction> rowKey="id" columns={planColumns} dataSource={plannedActions} pagination={{ pageSize: 8 }} scroll={{ x: 980 }} locale={{ emptyText: detail.task.last_error ? `暂未生成执行计划：${detail.task.last_error}` : `暂未生成执行计划，下次运行：${formatDateTime(detail.task.next_run_at)}` }} />
          </Space>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>执行记录</Typography.Title>
            <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={executedActions} pagination={{ pageSize: 8 }} scroll={{ x: 1680 }} locale={{ emptyText: '暂无已执行记录' }} />
          </Space>
        </Space>
      )}
    </DetailModal>
  );
}
