import React from 'react';
import { Alert, Button, Descriptions, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { HardHourlyRecentBucket, TaskCenterAction, TaskCenterDetail, TaskCenterTask, TenantBotSettings } from '../types';
import { DetailModal, StatusBadge } from '../components/shared';
import { parseBeijingDate } from '../time';
import { API_ORIGIN } from '../../shared/api/client';
import { TYPE_LABEL, accountCoverageLabel, formatDateTime, formatHardHourlyBlockers, hardHourlyStats, hardHourlyStatusColor, hardHourlyStatusLabel, runtimeStage, statusLabel } from './taskCenterViewModel';
import { TaskMembershipPanel } from './TaskMembershipPanel';

type DetailProfile = {
  hour: number;
  intensity: number;
  mode: string;
} | null;

type DetailSectionKind = 'aiCycles' | 'messageGroups' | 'relayBatches' | 'admissionItems';
type DetailPagination = { current: number; pageSize: number; total: number; loading?: boolean };

const rescueStatusLabel = (status: string) => {
  if (status === 'pending') return '已触发';
  if (status === 'invite_success') return '邀请成功';
  if (status === 'invite_failed') return '邀请失败';
  if (status === 'unconfigured') return '救援配置缺失';
  if (status === 'unknown_after_send') return '结果未知';
  return '未触发';
};

const deleteStatusLabel = (status?: string | null) => {
  const labels: Record<string, string> = {
    delete_pending: '待删除',
    deleting: '删除中',
    deleted: '已删除',
    delete_failed: '删除失败',
    not_requested: '不删除',
    unknown_after_send: '结果未知',
  };
  return status ? labels[status] || status : '-';
};

const topicDirectionTags = (value: unknown) => {
  const items = Array.isArray(value) ? value : [];
  if (!items.length) return '-';
  return (
    <Space wrap>
      {items.map((item: any, index) => (
        <Tag key={`${item?.title || 'topic'}:${index}`}>{item?.title || '-'}</Tag>
      ))}
    </Space>
  );
};

const teacherTargetTags = (value: unknown) => {
  const items = Array.isArray(value) ? value : [];
  if (!items.length) return '-';
  return (
    <Space wrap>
      {items.map((item: any, index) => (
        <Tag key={`${item?.name || 'teacher'}:${index}`}>{item?.name || '-'}</Tag>
      ))}
    </Space>
  );
};

interface TaskCenterDetailModalProps {
  detail: TaskCenterDetail | null;
  canManageTasks: boolean;
  supportLoading: boolean;
  plannedActions: TaskCenterAction[];
  executedActions: TaskCenterAction[];
  plannedActionLoading: boolean;
  executedActionLoading: boolean;
  plannedActionPagination: { current: number; pageSize: number; total: number };
  executedActionPagination: { current: number; pageSize: number; total: number };
  aiCyclePagination: DetailPagination;
  messageGroupPagination: DetailPagination;
  relayBatchPagination: DetailPagination;
  admissionItemPagination: DetailPagination;
  detailProfile: DetailProfile;
  detailPlannedTotal: number;
  membershipLoading: boolean;
  membershipPagination: { current: number; pageSize: number; total: number };
  membershipFilters: { phase: string; manualRequired: string };
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
  onPlannedActionPageChange: (page: number, pageSize: number) => void;
  onExecutedActionPageChange: (page: number, pageSize: number) => void;
  onDetailSectionPageChange: (kind: DetailSectionKind, page: number, pageSize: number) => void;
  onEditTask: (task: TaskCenterTask) => void;
  onRefreshTask: (task: TaskCenterTask) => void;
  telegramBotSettings?: TenantBotSettings | null;
  onMembershipPageChange: (page: number, pageSize: number) => void;
  onMembershipFiltersChange: (filters: { phase: string; manualRequired: string }) => void;
  onOpenAccountDetail?: (accountId: number, tab?: string) => void | Promise<void>;
  onResumeTask: (task: TaskCenterTask) => void;
  admissionBusyId: string;
  onRetryAdmissionItem: (item: TaskCenterDetail['membership_admission_items'][number]) => void | Promise<void>;
  onRetryAdmissionRescue: (item: TaskCenterDetail['membership_admission_items'][number]) => void | Promise<void>;
  onRetryFailedAdmissionItems: (task: TaskCenterTask) => void | Promise<void>;
  onMarkAdmissionManualHandled: (item: TaskCenterDetail['membership_admission_items'][number]) => void | Promise<void>;
  onExportAdmissionFailures: (task: TaskCenterTask) => void | Promise<void>;
  onClose: () => void;
}

function DetailStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={statusLabel(status)} />;
}

function mediaUrl(value?: string | null) {
  if (!value) return '';
  return value.startsWith('http') ? value : `${API_ORIGIN}${value}`;
}

function HardHourlyExecutionPanel({ detail }: { detail: TaskCenterDetail }) {
  const stats = hardHourlyStats(detail.task);
  if (!stats) return null;
  const goal = Number(stats.hard_hourly_goal ?? detail.task.type_config?.hourly_min_messages ?? 0);
  const success = Number(stats.hard_hourly_success_count ?? 0);
  const futureOpen = Number(stats.hard_hourly_open_count ?? 0);
  const overdueOpen = Number(stats.hard_hourly_overdue_open_count ?? 0);
  const deficit = Number(stats.hard_hourly_deficit ?? Math.max(0, goal - success - futureOpen));
  const recentBuckets = stats.hard_hourly_recent_buckets ?? [];
  const recentColumns: ColumnsType<HardHourlyRecentBucket> = [
    { title: '小时桶', dataIndex: 'bucket', width: 180, render: (value) => formatDateTime(value) },
    { title: '成功/目标', key: 'goal', width: 110, render: (_, item) => `${item.success_count ?? 0}/${item.goal ?? 0}` },
    { title: '未来待执行', key: 'future', width: 110, render: (_, item) => item.future_open_count ?? item.open_count ?? 0 },
    { title: '执行滞后', dataIndex: 'overdue_open_count', width: 100 },
    { title: '缺口', dataIndex: 'deficit', width: 80 },
    { title: '状态', dataIndex: 'status', width: 100, render: (value) => <Tag color={hardHourlyStatusColor(value)}>{hardHourlyStatusLabel(value)}</Tag> },
    { title: '阻塞原因', key: 'blockers', ellipsis: true, render: (_, item) => formatHardHourlyBlockers(item.blockers) },
  ];
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {overdueOpen > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`当前小时有 ${overdueOpen} 条执行滞后`}
          description="这些过期待执行项不抵扣硬目标缺口，请按 dispatcher_lag / worker 执行延迟排查。"
        />
      )}
      <Descriptions
        bordered
        size="small"
        column={4}
        title="硬目标执行"
        items={[
          { key: 'bucket', label: '小时桶', children: formatDateTime(stats.hard_hourly_bucket) },
          { key: 'status', label: '状态', children: <Tag color={hardHourlyStatusColor(stats.hard_hourly_status)}>{hardHourlyStatusLabel(stats.hard_hourly_status)}</Tag> },
          { key: 'success', label: '成功 / 目标', children: `${success} / ${goal || '-'}` },
          { key: 'deficit', label: '缺口', children: deficit },
          { key: 'future-open', label: '未来待执行覆盖', children: futureOpen },
          { key: 'overdue-open', label: '执行滞后（不抵扣缺口）', children: overdueOpen },
          { key: 'last-plan', label: '最近强推', children: stats.hard_hourly_last_check_at ? `${formatDateTime(stats.hard_hourly_last_check_at)} / 创建 ${stats.hard_hourly_last_planned_count ?? 0} 条` : '-' },
          { key: 'blockers', label: '阻塞原因', children: formatHardHourlyBlockers(stats.hard_hourly_last_blockers) },
        ]}
      />
      {recentBuckets.length > 0 && (
        <Table
          rowKey={(item) => item.bucket}
          columns={recentColumns}
          dataSource={recentBuckets.slice(-6).reverse()}
          pagination={false}
          size="small"
          scroll={{ x: 900 }}
        />
      )}
    </Space>
  );
}

export function TaskCenterDetailModal({
  detail,
  canManageTasks,
  supportLoading,
  plannedActions,
  executedActions,
  plannedActionLoading,
  executedActionLoading,
  plannedActionPagination,
  executedActionPagination,
  aiCyclePagination,
  messageGroupPagination,
  relayBatchPagination,
  admissionItemPagination,
  detailProfile,
  detailPlannedTotal,
  membershipLoading,
  membershipPagination,
  membershipFilters,
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
  onPlannedActionPageChange,
  onExecutedActionPageChange,
  onDetailSectionPageChange,
  onEditTask,
  onRefreshTask,
  telegramBotSettings,
  onMembershipPageChange,
  onMembershipFiltersChange,
  onOpenAccountDetail,
  onResumeTask,
  admissionBusyId,
  onRetryAdmissionItem,
  onRetryAdmissionRescue,
  onRetryFailedAdmissionItems,
  onMarkAdmissionManualHandled,
  onExportAdmissionFailures,
  onClose,
}: TaskCenterDetailModalProps) {
  const summaryUpdatedAt = detail?.task_runtime_summary?.updated_at ?? null;
  const currentStage = detail ? runtimeStage(detail.task, detail.task_runtime_summary, detail.membership_phase) : null;
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
  const accountSecurityBatch = detail?.account_security_batch ?? (detail?.profile_batch ? {
    ...detail.profile_batch,
    system_task_type: 'account_profile_init',
  } : null);
  const profileBatchItems = accountSecurityBatch?.items ?? [];
  const archivedSkippedCount = Number(detail?.stats?.archived_skipped_count ?? 0);
  const effectiveSkippedCount = Number(detail?.stats?.skipped_count ?? 0);
  const effectiveTotalActions = Number(detail?.stats?.total_actions ?? 0);
  const accountSecurityBatchColumns: ColumnsType<typeof profileBatchItems[number]> = [
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
    { title: '设备清理', dataIndex: 'device_cleanup_status', width: 120, render: (value) => value ? <DetailStatusBadge status={value} /> : '-' },
    { title: '2FA', dataIndex: 'two_fa_status', width: 100, render: (value) => value ? <DetailStatusBadge status={value} /> : '-' },
    { title: '备用 session', dataIndex: 'standby_session_status', width: 130, render: (value) => value ? <DetailStatusBadge status={value} /> : '-' },
    { title: '目标槽位', dataIndex: 'target_slot', width: 110, render: (value) => value || '-' },
    { title: '开发者应用', dataIndex: 'developer_app_label', width: 130, render: (value) => value || '-' },
    { title: '代理', dataIndex: 'proxy_label', width: 110, render: (value) => value || '-' },
    { title: '验证码读取', dataIndex: 'verification_code_status', width: 120, render: (value) => value || '-' },
    { title: '2FA 使用', dataIndex: 'two_fa_usage_status', width: 130, render: (value) => value || '-' },
    { title: '保留设备', dataIndex: 'preserved_devices_summary', width: 220, render: (value) => value || 'primary / standby_1 / standby_2 / 官方锚点设备' },
    {
      title: '头像回显',
      dataIndex: 'avatar_preview_url',
      width: 110,
      render: (value) => value ? <img alt="账号头像" src={mediaUrl(value)} style={{ width: 36, height: 36, borderRadius: 4, objectFit: 'cover' }} /> : '-',
    },
    { title: '失败原因', key: 'failure', ellipsis: true, render: (_, item) => item.failure_detail || item.failure_type || '-' },
  ];
  const admissionColumns: ColumnsType<TaskCenterDetail['membership_admission_items'][number]> = [
    { title: '账号', key: 'account', width: 180, render: (_, item) => item.display_name || `账号 #${item.account_id}` },
    { title: '阶段', dataIndex: 'phase', width: 130, render: (value, item) => <Tag color={item.manual_required ? 'orange' : value === 'completed' ? 'green' : value === 'failed' ? 'red' : 'blue'}>{value}</Tag> },
    { title: '测试消息', dataIndex: 'test_message_text', ellipsis: true, render: (value) => value || '-' },
    { title: '消息 ID', dataIndex: 'test_message_id', width: 120, render: (value) => value || '-' },
    { title: '删除', dataIndex: 'delete_status', width: 110, render: (value) => deleteStatusLabel(value) },
    { title: '权限失败', dataIndex: 'permission_failure_count', width: 100, render: (value) => value || 0 },
    {
      title: '救援状态',
      key: 'rescue',
      width: 150,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Tag color={item.rescue_status === 'invite_success' ? 'green' : item.rescue_status === 'invite_failed' || item.rescue_status === 'unconfigured' ? 'red' : item.rescue_status ? 'blue' : 'default'}>{rescueStatusLabel(item.rescue_status)}</Tag>
          {item.rescue_action_id && <Typography.Text type="secondary">{item.rescue_action_id.slice(0, 8)}</Typography.Text>}
        </Space>
      ),
    },
    { title: '失败原因', key: 'failure', ellipsis: true, render: (_, item) => item.failure_detail || item.failure_type || '-' },
    { title: '救援错误', dataIndex: 'rescue_failure_detail', ellipsis: true, render: (value) => value || '-' },
    { title: '完成时间', dataIndex: 'completed_at', width: 170, render: (value) => formatDateTime(value) },
    {
      title: '操作',
      key: 'action',
      width: 190,
      render: (_, item) => (
        <Space>
          {canManageTasks && item.phase === 'failed' && (
            <Button size="small" loading={admissionBusyId === `retry:${item.id}`} onClick={() => onRetryAdmissionItem(item)}>重试</Button>
          )}
          {canManageTasks && item.rescue_status && item.rescue_status !== 'invite_success' && (
            <Button size="small" loading={admissionBusyId === `rescue:${item.id}`} onClick={() => onRetryAdmissionRescue(item)}>重试救援</Button>
          )}
          {canManageTasks && item.manual_required && (
            <Button size="small" loading={admissionBusyId === `manual:${item.id}`} onClick={() => onMarkAdmissionManualHandled(item)}>已处理，重查</Button>
          )}
        </Space>
      ),
    },
  ];
  const admissionTotal = Number(detail?.membership_admission_phase?.snapshot_total ?? admissionItemPagination.total ?? 0);
  const showAiTab = detail?.task.type === 'group_ai_chat';
  const botMissingReasons = [
    !telegramBotSettings?.telegram_bot_configured ? 'TG bot 未配置' : '',
    !telegramBotSettings?.admin_chat_id ? '管理员 Chat ID 未配置' : '',
    telegramBotSettings && !telegramBotSettings.ai_group_bot_enabled ? 'AI 活群 Bot 设置未启用' : '',
  ].filter(Boolean);
  const showTargetTab = detail ? ['group_relay', 'channel_view', 'channel_like', 'channel_comment'].includes(detail.task.type) : false;
  const accountCoverage = detail?.task.stats?.account_coverage;
  const detailTabs = detail ? [
    showAiTab ? {
      key: 'ai-settings',
      label: 'AI 设置',
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {botMissingReasons.length > 0 && <Alert type="warning" showIcon message={botMissingReasons.join('；')} />}
          <Descriptions
            bordered
            size="small"
            column={2}
            items={[
              { key: 'topic_directions', label: '话题方向', children: topicDirectionTags(detail.task.type_config?.topic_directions) },
              { key: 'teacher_targets', label: '讨论老师', children: teacherTargetTags(detail.task.type_config?.teacher_targets) },
              { key: 'burst_enabled', label: '同账号连发', children: detail.task.type_config?.consecutive_message_enabled ? '开启' : '关闭' },
              { key: 'burst_window', label: '连发窗口', children: `${detail.task.type_config?.consecutive_message_min ?? 2}-${detail.task.type_config?.consecutive_message_max ?? 4}` },
              { key: 'burst_probability', label: '连发概率', children: detail.task.type_config?.consecutive_message_probability ?? 0.3 },
              { key: 'coverage_mode', label: '全账号日覆盖', children: detail.task.type_config?.account_coverage_mode === 'all_accounts_daily' ? '开启' : '关闭' },
              { key: 'coverage_window', label: '覆盖窗口', children: `${detail.task.type_config?.coverage_window_hours ?? 24} 小时` },
              { key: 'coverage_range', label: '每账号消息数', children: `${detail.task.type_config?.per_account_daily_min_messages ?? 1}-${detail.task.type_config?.per_account_daily_max_messages ?? 2}` },
              { key: 'coverage_progress', label: '今日覆盖', children: accountCoverage ? accountCoverageLabel(detail.task) : '-' },
              { key: 'coverage_remaining', label: '剩余覆盖账号', children: accountCoverage ? Number(accountCoverage.remaining_count ?? 0) : '-' },
              { key: 'coverage_target_accounts', label: '覆盖目标账号', children: accountCoverage ? Number(accountCoverage.target_account_count ?? accountCoverage.eligible_count ?? 0) : '-' },
              { key: 'coverage_remaining_messages', label: '剩余覆盖消息', children: accountCoverage ? Number(accountCoverage.remaining_message_count ?? 0) : '-' },
              { key: 'coverage_not_ready', label: '未准入/受限', children: accountCoverage ? `${Number(accountCoverage.pending_admission_count ?? 0)} / ${Number(accountCoverage.restricted_count ?? 0)}` : '-' },
              { key: 'coverage_estimated_window', label: '预计补齐窗口', children: accountCoverage?.estimated_completion_window?.label || '-' },
              {
                key: 'coverage_blocked_reasons',
                label: '阻塞原因',
                children: accountCoverage?.blocked_reasons?.length ? (
                  <Space wrap>{accountCoverage.blocked_reasons.map((item) => <Tag key={`${item.reason}:${item.message || item.count}`}>{item.message || item.reason}{item.count ? ` x${item.count}` : ''}</Tag>)}</Space>
                ) : '-',
              },
              {
                key: 'coverage_pending_accounts',
                label: '近期待补账号',
                children: accountCoverage?.pending_accounts?.length ? (
                  <Space direction="vertical" size={2}>{accountCoverage.pending_accounts.slice(0, 6).map((item) => <Typography.Text key={item.account_id}>{item.display_name || item.account_id}：{item.completed_count}/{item.target_count}，{item.reason}</Typography.Text>)}</Space>
                ) : '-',
              },
            ]}
          />
          {canManageTasks && <Button onClick={() => onEditTask(detail.task)}>编辑设置</Button>}
        </Space>
      ),
    } : null,
    accountSecurityBatch ? {
      key: 'account-security-batch',
      label: TYPE_LABEL[accountSecurityBatch.system_task_type] || '账号安全批次',
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {canManageTasks && (Number(detail.membership_admission_phase.failed_count ?? 0) > 0 || Number(detail.membership_admission_phase.manual_required_count ?? 0) > 0) && (
            <Space>
              {Number(detail.membership_admission_phase.failed_count ?? 0) > 0 && (
                <Button loading={admissionBusyId === 'retry-failed'} onClick={() => onRetryFailedAdmissionItems(detail.task)}>重试失败项</Button>
              )}
              <Button loading={admissionBusyId === `export:${detail.task.id}`} onClick={() => onExportAdmissionFailures(detail.task)}>导出失败清单</Button>
            </Space>
          )}
          <Descriptions
            bordered
            size="small"
            column={5}
            items={[
              { key: 'batch', label: '批次', children: `#${accountSecurityBatch.batch_id}` },
              { key: 'batch_status', label: '批次状态', children: <DetailStatusBadge status={accountSecurityBatch.batch_status} /> },
              { key: 'type', label: '系统任务', children: TYPE_LABEL[accountSecurityBatch.system_task_type] || accountSecurityBatch.system_task_type },
              { key: 'preserve', label: '设备保护', children: accountSecurityBatch.system_task_type === 'account_device_cleanup' ? 'primary / standby_1 / standby_2 / 官方锚点设备' : '-' },
              { key: 'two-fa', label: '二步密码', children: accountSecurityBatch.system_task_type === 'account_2fa_setup' ? '平台托管 2FA 设置 / 替换 / 旧密码未知跳过' : '-' },
              { key: 'standby', label: '备用补齐', children: accountSecurityBatch.system_task_type === 'account_standby_session_provision' ? '目标槽位 / 开发者应用 / 代理 / 验证码读取 / 2FA 使用 / 健康检查' : '-' },
            ]}
          />
          <Table
            rowKey={(item) => `${item.account_id}:${item.avatar_source || 'profile'}`}
            columns={accountSecurityBatchColumns}
            dataSource={profileBatchItems}
            pagination={{ pageSize: 8 }}
            size="small"
            scroll={{ x: 1700 }}
          />
        </Space>
      ),
    } : null,
    admissionTotal > 0 ? {
      key: 'membership-admission',
      label: `群聊准入 (${admissionItemPagination.total || admissionTotal})`,
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Descriptions
            bordered
            size="small"
            column={4}
            items={[
              { key: 'total', label: '快照账号', children: detail.membership_admission_phase.snapshot_total ?? 0 },
              { key: 'completed', label: '已达标', children: detail.membership_admission_phase.completed_count ?? 0 },
              { key: 'manual', label: '需人工处理', children: detail.membership_admission_phase.manual_required_count ?? 0 },
              { key: 'failed', label: '失败', children: detail.membership_admission_phase.failed_count ?? 0 },
            ]}
          />
          <Table
            rowKey="id"
            columns={admissionColumns}
            dataSource={detail.membership_admission_items}
            loading={admissionItemPagination.loading}
            pagination={{ ...admissionItemPagination, showSizeChanger: true, onChange: (page, pageSize) => onDetailSectionPageChange('admissionItems', page, pageSize) }}
            size="small"
            scroll={{ x: 1000 }}
          />
        </Space>
      ),
    } : null,
    (detail.membership_phase?.stage || detail.membership_accounts.length > 0 || membershipPagination.total > 0) ? {
      key: 'membership',
      label: `准入前置 ${membershipPagination.total ? `(${membershipPagination.total})` : detail.membership_accounts.length ? `(${detail.membership_accounts.length})` : ''}`,
      children: (
        <TaskMembershipPanel
          membershipPhase={detail.membership_phase}
          membershipAccounts={detail.membership_accounts}
          membershipLoading={membershipLoading}
          membershipPagination={membershipPagination}
          membershipFilters={membershipFilters}
          onMembershipPageChange={onMembershipPageChange}
          onMembershipFiltersChange={onMembershipFiltersChange}
          onOpenAccountDetail={onOpenAccountDetail}
        />
      ),
    } : null,
    showAiTab ? {
      key: 'ai-cycles',
      label: `AI 活跃 ${aiCyclePagination.total ? `(${aiCyclePagination.total})` : ''}`,
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {detail.learning_profile_preview?.profile_scene && (
            <Descriptions
              bordered
              size="small"
              column={4}
              items={[
                { key: 'scene', label: '目标画像', children: detail.learning_profile_preview.profile_scene },
                { key: 'version', label: '版本', children: detail.learning_profile_preview.profile_version || 0 },
                { key: 'samples', label: '样本', children: detail.learning_profile_preview.source_sample_count || 0 },
                { key: 'status', label: '状态', children: detail.learning_profile_preview.profile_unavailable_reason || detail.learning_profile_preview.profile_hit_summary || '-' },
              ]}
            />
          )}
          {detail.ai_generation_records.length > 0 && <Table rowKey="generation_id" columns={aiGenerationColumns} dataSource={detail.ai_generation_records} pagination={false} size="small" scroll={{ x: 950 }} />}
          {detail.ai_account_profiles.length > 0 && <Table rowKey="account_id" columns={aiAccountProfileColumns} dataSource={detail.ai_account_profiles} pagination={false} size="small" scroll={{ x: 900 }} />}
          <Table<TaskCenterDetail['ai_cycles'][number]>
            rowKey="cycle_id"
            columns={aiCycleColumns}
            dataSource={detail.ai_cycles}
            loading={aiCyclePagination.loading}
            pagination={{ ...aiCyclePagination, showSizeChanger: true, onChange: (page, pageSize) => onDetailSectionPageChange('aiCycles', page, pageSize) }}
            scroll={{ x: 820 }}
            expandable={{
              expandedRowRender: (item) => <Table rowKey="action_id" columns={aiTurnColumns} dataSource={item.turns} pagination={false} size="small" scroll={{ x: 1540 }} />,
            }}
          />
        </Space>
      ),
    } : null,
    showTargetTab ? {
      key: 'targets',
      label: '目标明细',
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {detail.task.type === 'group_relay' && (
            <Table<TaskCenterDetail['relay_batches'][number]>
              rowKey="relay_batch_id"
              columns={relayBatchColumns}
              dataSource={detail.relay_batches}
              loading={relayBatchPagination.loading}
              pagination={{ ...relayBatchPagination, showSizeChanger: true, onChange: (page, pageSize) => onDetailSectionPageChange('relayBatches', page, pageSize) }}
              scroll={{ x: 820 }}
              expandable={{
                expandedRowRender: (item) => <Table rowKey="action_id" columns={relayItemColumns} dataSource={item.items} pagination={false} size="small" scroll={{ x: 2600 }} />,
              }}
            />
          )}
          {detail.task.type === 'group_relay' && detail.recent_relay_sources.length > 0 && (
            <Table<TaskCenterDetail['recent_relay_sources'][number]>
              rowKey={(item) => `${item.source_group_id ?? 'source'}:${item.remote_message_id || item.sender_peer_id}`}
              columns={relaySourceColumns}
              dataSource={detail.recent_relay_sources}
              pagination={{ pageSize: 6 }}
              size="small"
              scroll={{ x: 1020 }}
            />
          )}
          {['channel_view', 'channel_like', 'channel_comment'].includes(detail.task.type) && (
            <Table<TaskCenterDetail['message_groups'][number]>
              rowKey={(item) => `${item.channel_target_id ?? 'channel'}:${item.message_id ?? 'message'}`}
              columns={messageColumns}
              dataSource={detail.message_groups}
              loading={messageGroupPagination.loading}
              pagination={{ ...messageGroupPagination, showSizeChanger: true, onChange: (page, pageSize) => onDetailSectionPageChange('messageGroups', page, pageSize) }}
              scroll={{ x: 1680 }}
              expandable={{
                expandedRowRender: (item) => <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={item.actions} pagination={false} size="small" scroll={{ x: 1680 }} />,
              }}
            />
          )}
        </Space>
      ),
    } : null,
    {
      key: 'plan',
      label: `执行计划 (${plannedActionPagination.total})`,
      children: <Table<TaskCenterAction> rowKey="id" columns={planColumns} dataSource={plannedActions} loading={plannedActionLoading} pagination={{ ...plannedActionPagination, showSizeChanger: true, onChange: onPlannedActionPageChange }} scroll={{ x: 980 }} locale={{ emptyText: detail.task.last_error ? `暂未生成执行计划：${detail.task.last_error}` : `暂未生成执行计划，下次运行：${formatDateTime(detail.task.next_run_at)}` }} />,
    },
    {
      key: 'records',
      label: `执行记录 (${executedActionPagination.total})`,
      children: <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={executedActions} loading={executedActionLoading} pagination={{ ...executedActionPagination, showSizeChanger: true, onChange: onExecutedActionPageChange }} scroll={{ x: 1680 }} locale={{ emptyText: '暂无已执行记录' }} />,
    },
  ].filter(Boolean) as Array<{ key: string; label: React.ReactNode; children: React.ReactNode }> : [];
  return (
    <>
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
            {currentStage?.stage_code === 'paused' && (
              <Alert
                type="error"
                showIcon
                message="任务已暂停，不会继续规划或执行新动作"
                description={currentStage.reason}
                action={canManageTasks ? <Button size="small" danger onClick={() => onResumeTask(detail.task)}>继续任务</Button> : undefined}
              />
            )}
            <Descriptions
              bordered
              column={3}
              size="small"
              items={[
                { key: 'type', label: '类型', children: TYPE_LABEL[detail.task.type] ?? detail.task.type },
                { key: 'status', label: '状态', children: <StatusBadge status={currentStage?.stage_label || detail.task.status} label={currentStage?.stage_label || statusLabel(detail.task.status)} /> },
                { key: 'runtime-stage', label: '运行阶段', children: currentStage?.reason || '-' },
                { key: 'target', label: '目标', children: detail.task.target_summary || '-' },
                { key: 'planned', label: '计划中', children: plannedActionPagination.total },
                { key: 'executing', label: '执行中', children: plannedActions.filter((action) => action.status === 'executing').length },
                { key: 'success', label: '已成功', children: detail.stats.success_count ?? 0 },
                { key: 'failure', label: '失败', children: detail.stats.failure_count ?? 0 },
                { key: 'skipped', label: '跳过', children: effectiveSkippedCount },
                { key: 'total', label: '总动作', children: effectiveTotalActions },
                ...(archivedSkippedCount > 0 ? [{ key: 'archived-skipped', label: '历史归档跳过', children: archivedSkippedCount }] : []),
                { key: 'curve-now', label: '当前曲线', children: detailProfile ? `${String(detailProfile.hour).padStart(2, '0')}:00 ${detailProfile.intensity}${detail.task.type === 'group_ai_chat' ? ' 轮/小时' : ''}，${detailProfile.mode}运行` : '-' },
                { key: 'curve-gap', label: '原因分解', children: `计划 ${detailPlannedTotal}，成功 ${detail.stats.success_count ?? 0}，失败 ${detail.stats.failure_count ?? 0}，跳过 ${effectiveSkippedCount}，待执行 ${plannedActionPagination.total}` },
                { key: 'account-coverage', label: '今日账号参与覆盖', children: accountCoverageLabel(detail.stats) },
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
            <HardHourlyExecutionPanel detail={detail} />
            <Tabs className="tabs-row" items={detailTabs} />
          </Space>
        )}
      </DetailModal>
    </>
  );
}
