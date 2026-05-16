import React from 'react';
import { Button, Descriptions, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TaskCenterAction, TaskCenterDetail, TaskCenterTask } from '../types';
import { DetailModal, StatusBadge } from '../components/shared';
import { TYPE_LABEL, formatDateTime, statusLabel } from './taskCenterViewModel';

type DetailProfile = {
  hour: number;
  intensity: number;
  mode: string;
} | null;

interface TaskCenterDetailModalProps {
  detail: TaskCenterDetail | null;
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

export function TaskCenterDetailModal({
  detail,
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
  messageColumns,
  planColumns,
  recordColumns,
  onEditTask,
  onRefreshTask,
  onClose,
}: TaskCenterDetailModalProps) {
  return (
    <DetailModal
      title={detail?.task.name ?? '任务详情'}
      open={Boolean(detail)}
      size="wide"
      extra={detail && (
        <Space>
          <Button loading={supportLoading} onClick={() => onEditTask(detail.task)}>编辑任务</Button>
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
              { key: 'type', label: '类型', children: TYPE_LABEL[detail.task.type] },
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
                  expandedRowRender: (item) => <Table rowKey="action_id" columns={relayItemColumns} dataSource={item.items} pagination={false} size="small" scroll={{ x: 2200 }} />,
                }}
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
                scroll={{ x: 1180 }}
                expandable={{
                  expandedRowRender: (item) => <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={item.actions} pagination={false} size="small" scroll={{ x: 1180 }} />,
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
            <Table<TaskCenterAction> rowKey="id" columns={recordColumns} dataSource={executedActions} pagination={{ pageSize: 8 }} scroll={{ x: 1180 }} locale={{ emptyText: '暂无已执行记录' }} />
          </Space>
        </Space>
      )}
    </DetailModal>
  );
}
