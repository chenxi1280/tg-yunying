import React from 'react';
import { Alert, Button, Descriptions, Drawer, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TaskCenterDetail, TaskMembershipItem } from '../types';
import { StatusBadge } from '../components/shared';
import { formatDateTime, statusLabel } from './taskCenterViewModel';

type MembershipFilters = { phase: string; manualRequired: string };

interface TaskMembershipPanelProps {
  membershipPhase: TaskCenterDetail['membership_phase'];
  membershipAccounts: TaskMembershipItem[];
  membershipLoading: boolean;
  membershipPagination: { current: number; pageSize: number; total: number };
  membershipFilters: MembershipFilters;
  onMembershipPageChange: (page: number, pageSize: number) => void;
  onMembershipFiltersChange: (filters: MembershipFilters) => void;
  onOpenAccountDetail?: (accountId: number, tab?: string) => void | Promise<void>;
}

function DetailStatusBadge({ status }: { status?: string | null }) {
  return <StatusBadge status={status} label={statusLabel(status)} />;
}

function membershipFailureSummary(item: TaskMembershipItem) {
  return item.failure_detail || item.challenge_question || item.failure_type || '-';
}

function membershipPhaseLabel(phase?: string) {
  const labels: Record<string, string> = {
    not_joined: '待入群',
    joining: '加入中',
    channel_follow_required: '待关注频道',
    following_channel: '关注中',
    challenge_required: '待验证',
    challenge_solving: '验证中',
    manual_required: '人工处理',
    ready: '可发言',
    failed: '失败',
  };
  return labels[phase || ''] || phase || '-';
}

function needsOperatorAction(item: TaskMembershipItem) {
  return item.manual_required || item.phase === 'manual_required' || Boolean(item.verification_task_id);
}

export function TaskMembershipPanel({
  membershipPhase,
  membershipAccounts,
  membershipLoading,
  membershipPagination,
  membershipFilters,
  onMembershipPageChange,
  onMembershipFiltersChange,
  onOpenAccountDetail,
}: TaskMembershipPanelProps) {
  const [selectedItem, setSelectedItem] = React.useState<TaskMembershipItem | null>(null);
  const openAccountVerification = (item: TaskMembershipItem) => {
    void onOpenAccountDetail?.(item.account_id, '验证待处理');
  };
  const columns: ColumnsType<TaskMembershipItem> = [
    { title: '账号', key: 'account', width: 180, render: (_, item) => <Space direction="vertical" size={0}><Typography.Text strong>{item.display_name || `账号 #${item.account_id}`}</Typography.Text><Typography.Text type="secondary">{item.username ? `@${item.username}` : '-'}</Typography.Text></Space> },
    { title: '阶段', dataIndex: 'phase', width: 130, render: (value) => <Tag color={value === 'ready' ? 'green' : value === 'manual_required' ? 'gold' : value === 'failed' ? 'red' : undefined}>{membershipPhaseLabel(value)}</Tag> },
    { title: '可发言', dataIndex: 'can_send', width: 90, render: (value) => value ? <Tag color="green">是</Tag> : <Tag>否</Tag> },
    { title: '验证', key: 'verification', width: 190, render: (_, item) => item.verification_task_id ? <Space direction="vertical" size={0}><Typography.Text>{item.verification_action || '验证辅助'}</Typography.Text><Typography.Text type="secondary">#{item.verification_task_id} {item.verification_status || '待处理'}</Typography.Text></Space> : '-' },
    { title: '目标', dataIndex: 'target_display', width: 180, ellipsis: true },
    { title: '失败原因', key: 'failure_reason', ellipsis: true, render: (_, item) => membershipFailureSummary(item) },
    { title: '计划时间', dataIndex: 'scheduled_at', width: 170, render: (value) => formatDateTime(value) },
    { title: '完成时间', dataIndex: 'completed_at', width: 170, render: (value) => formatDateTime(value) },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 130,
      render: (_, item) => needsOperatorAction(item) ? (
        <Button
          size="small"
          type="primary"
          disabled={!onOpenAccountDetail}
          onClick={(event) => {
            event.stopPropagation();
            openAccountVerification(item);
          }}
        >
          打开账号处理
        </Button>
      ) : <Typography.Text type="secondary">无需处理</Typography.Text>,
    },
  ];

  return (
    <>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Descriptions
          bordered
          size="small"
          column={4}
          items={[
            { key: 'stage', label: '状态', children: membershipPhase?.stage || 'not_required' },
            { key: 'status', label: '子任务状态', children: membershipPhase?.status || membershipPhase?.stage || 'not_required' },
            { key: 'progress', label: '预计进度', children: `${membershipPhase?.progress_percent ?? 0}%` },
            { key: 'phase', label: '当前阶段', children: membershipPhase?.current_phase || '-' },
            { key: 'ready', label: '已满足', children: membershipPhase?.ready_account_count ?? membershipPhase?.joined_count ?? 0 },
            { key: 'pending', label: '待准备', children: membershipPhase?.pending_account_count ?? membershipPhase?.need_join_count ?? 0 },
            { key: 'failed', label: '失败', children: membershipPhase?.failed_account_count ?? membershipPhase?.failed_count ?? 0 },
            { key: 'running', label: '执行中', children: membershipPhase?.running_account_count ?? membershipPhase?.running_count ?? membershipPhase?.summary?.running_account_count ?? 0 },
            { key: 'success', label: '成功/跳过', children: membershipPhase?.success_account_count ?? membershipPhase?.success_count ?? membershipPhase?.summary?.success_account_count ?? 0 },
            { key: 'blocked', label: '不可准备', children: membershipPhase?.blocked_account_count ?? 0 },
            { key: 'targets', label: '目标数', children: membershipPhase?.summary?.target_count ?? '-' },
            { key: 'eta', label: '预计完成', children: formatDateTime(membershipPhase?.estimated_finish_at || membershipPhase?.summary?.estimated_finish_at) || '-' },
          ]}
        />
        <Space wrap>
          <Select
            aria-label="准入阶段筛选"
            value={membershipFilters.phase}
            style={{ width: 150 }}
            options={[
              { value: 'all', label: '全部阶段' },
              { value: 'not_joined', label: '待入群' },
              { value: 'joining', label: '加入中' },
              { value: 'channel_follow_required', label: '待关注频道' },
              { value: 'following_channel', label: '关注中' },
              { value: 'challenge_required', label: '待验证' },
              { value: 'challenge_solving', label: '验证中' },
              { value: 'manual_required', label: '人工处理' },
              { value: 'ready', label: '可发言' },
              { value: 'failed', label: '失败' },
            ]}
            onChange={(phase) => onMembershipFiltersChange({ ...membershipFilters, phase })}
          />
          <Select
            aria-label="人工处理筛选"
            value={membershipFilters.manualRequired}
            style={{ width: 150 }}
            options={[
              { value: 'all', label: '全部账号' },
              { value: 'true', label: '只看人工处理' },
              { value: 'false', label: '排除人工处理' },
            ]}
            onChange={(manualRequired) => onMembershipFiltersChange({ ...membershipFilters, manualRequired })}
          />
        </Space>
        <Table<TaskMembershipItem>
          rowKey={(item) => item.item_id}
          columns={columns}
          dataSource={membershipAccounts}
          loading={membershipLoading}
          onRow={(item) => ({
            onClick: () => setSelectedItem(item),
            style: { cursor: 'pointer' },
          })}
          pagination={{
            current: membershipPagination.current,
            pageSize: membershipPagination.pageSize,
            total: membershipPagination.total,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条准入账号`,
            onChange: onMembershipPageChange,
          }}
          size="small"
          scroll={{ x: 1370 }}
        />
      </Space>
      <MembershipDetailDrawer item={selectedItem} onClose={() => setSelectedItem(null)} onOpenAccountVerification={openAccountVerification} />
    </>
  );
}

function MembershipDetailDrawer({
  item,
  onClose,
  onOpenAccountVerification,
}: {
  item: TaskMembershipItem | null;
  onClose: () => void;
  onOpenAccountVerification: (item: TaskMembershipItem) => void;
}) {
  const shouldHandle = item ? needsOperatorAction(item) : false;
  return (
    <Drawer
      title={item ? `${item.display_name || `账号 #${item.account_id}`} 准入详情` : '准入详情'}
      open={Boolean(item)}
      width={560}
      onClose={onClose}
      destroyOnClose
    >
      {item && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {shouldHandle && (
            <Alert
              type="warning"
              showIcon
              message="需要到账号验证待处理完成处置"
              description="先在 Telegram 内完成人工动作，再回到账号详情的验证待处理中标记已人工处理或重查群限制。"
              action={<Button size="small" type="primary" onClick={() => onOpenAccountVerification(item)}>打开账号处理</Button>}
            />
          )}
          <Descriptions
            bordered
            size="small"
            column={1}
            items={[
              { key: 'account', label: '账号', children: `${item.display_name || `账号 #${item.account_id}`} ${item.username ? `/ @${item.username}` : ''}` },
              { key: 'target', label: '目标', children: item.target_display || item.target_id || '-' },
              { key: 'phase', label: '当前阶段', children: <Tag>{membershipPhaseLabel(item.phase)}</Tag> },
              { key: 'status', label: '动作状态', children: <DetailStatusBadge status={item.status} /> },
              { key: 'can-send', label: '可发言', children: item.can_send ? <Tag color="green">已可发言</Tag> : <Tag>未确认</Tag> },
              { key: 'manual', label: '人工处理', children: item.manual_required ? <Tag color="gold">需要人工处理</Tag> : '否' },
              { key: 'planned', label: '计划时间', children: formatDateTime(item.scheduled_at) },
              { key: 'completed', label: '完成时间', children: formatDateTime(item.completed_at) },
            ]}
          />
          <Descriptions
            bordered
            size="small"
            column={1}
            title="验证辅助"
            items={[
              { key: 'verification-id', label: '验证任务', children: item.verification_task_id ? `#${item.verification_task_id}` : '-' },
              { key: 'verification-status', label: '验证状态', children: item.verification_status || '-' },
              { key: 'verification-action', label: '处理动作', children: item.verification_action || '-' },
              { key: 'auto', label: '自动处理', children: item.can_auto_resolve ? <Tag color="blue">可自动尝试</Tag> : <Tag>不可自动</Tag> },
              { key: 'challenge', label: '题面 / 证据', children: item.challenge_question || '-' },
            ]}
          />
          <Descriptions
            bordered
            size="small"
            column={1}
            title="失败与结果"
            items={[
              { key: 'failure-type', label: '失败类型', children: item.failure_type || '-' },
              { key: 'failure-detail', label: '可读原因', children: item.failure_detail || '-' },
              { key: 'action-id', label: '准入动作', children: item.latest_action_id || item.item_id },
            ]}
          />
        </Space>
      )}
    </Drawer>
  );
}
