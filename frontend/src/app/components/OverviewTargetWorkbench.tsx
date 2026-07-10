import React from 'react';
import { Activity, RefreshCcw, ShieldAlert, Users } from 'lucide-react';
import { Alert, Button, Card, Space, Table, Typography } from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import type { OperationCenterSummary, OperationIssue, OperationTarget, TargetRuntimeSummary } from '../types';
import type { TargetPageQuery } from '../hooks/useOverviewOperationData';
import { Badge, StatusBadge } from './shared';
import { formatBeijingDateTime } from '../time';

type TargetWorkbenchRow = Readonly<{
  key: string;
  targetId: number;
  target?: OperationTarget;
  summary?: TargetRuntimeSummary;
  issues: OperationIssue[];
}>;

type WorkbenchActions = Readonly<{
  planBusy: string;
  onOpenTargets?: (targetId?: number) => void;
  onOpenTaskDetail?: (taskId?: string) => void;
  onOpenMessageSending?: () => void;
  onOpenIssue: (issueId: string) => void;
  onCreatePlan: (target: OperationTarget) => void;
}>;

type Props = WorkbenchActions & Readonly<{
  operationSummary: OperationCenterSummary | null;
  operationError: string;
  operationLoading: boolean;
  targets: OperationTarget[];
  targetSummaries: TargetRuntimeSummary[];
  issues: OperationIssue[];
  targetPageQuery: TargetPageQuery;
  targetTotal: number;
  onTargetPageChange: (pagination: TablePaginationConfig) => void;
  onRefresh: () => void;
}>;

export function isMessageIssue(issue?: OperationIssue | null) {
  return Boolean(issue?.source_task_id?.startsWith('message_task:') || issue?.return_to?.page === 'message-sending');
}

export function severityTone(value?: string) {
  if (['critical', 'high', '高'].includes(value ?? '')) return 'danger';
  if (['medium', '中'].includes(value ?? '')) return 'warning';
  if (['low', '低'].includes(value ?? '')) return 'positive';
  return 'neutral';
}

export function severityLabel(value?: string) {
  if (value === 'critical') return '严重';
  if (value === 'high') return '高';
  if (value === 'medium') return '中';
  if (value === 'low') return '低';
  return value || '-';
}

export function handlingModeLabel(value?: string) {
  if (value === 'modal') return '弹窗处理';
  if (value === 'drawer') return '抽屉处理';
  if (value === 'deep_link') return '深链跳转';
  return value || '-';
}

function targetRuntimeStatusLabel(status?: string) {
  if (!status) return '未汇总';
  if (status === 'healthy') return '健康';
  if (status === 'warning') return '需关注';
  if (status === 'failed') return '有失败';
  return status;
}

function buildTargetRows(
  targets: OperationTarget[],
  summaries: TargetRuntimeSummary[],
  issues: OperationIssue[],
) {
  const issueMap = new Map<number, OperationIssue[]>();
  for (const issue of issues) {
    if (issue.target_id) issueMap.set(issue.target_id, [...(issueMap.get(issue.target_id) ?? []), issue]);
  }
  const rows = new Map<number, TargetWorkbenchRow>();
  for (const target of targets) {
    rows.set(target.id, { key: String(target.id), targetId: target.id, target, issues: issueMap.get(target.id) ?? [] });
  }
  for (const summary of summaries) {
    const current = rows.get(summary.target_id);
    rows.set(summary.target_id, {
      key: String(summary.target_id), targetId: summary.target_id,
      target: current?.target, summary, issues: issueMap.get(summary.target_id) ?? current?.issues ?? [],
    });
  }
  return [...rows.values()].sort((left, right) => {
    const issueDelta = (right.summary?.open_issue_count ?? right.issues.length) - (left.summary?.open_issue_count ?? left.issues.length);
    return issueDelta || (right.summary?.failed_action_count ?? 0) - (left.summary?.failed_action_count ?? 0);
  });
}

function targetColumns(actions: WorkbenchActions): ColumnsType<TargetWorkbenchRow> {
  return [
    {
      title: '目标', key: 'target', width: 280, render: (_, row) => (
        <Space orientation="vertical" size={0}>
          <Typography.Text strong>{row.target?.title ?? `目标 #${row.targetId}`}</Typography.Text>
          <Typography.Text type="secondary">{row.target?.target_type === 'channel' ? '频道' : '群'} / {row.target?.username ? `@${row.target.username}` : row.target?.tg_peer_id ?? '-'}</Typography.Text>
        </Space>
      ),
    },
    { title: '运行状态', key: 'status', width: 130, render: (_, row) => <StatusBadge status={row.summary?.status ?? '未汇总'} label={targetRuntimeStatusLabel(row.summary?.status)} /> },
    { title: 'Open Issue', key: 'open_issue_count', width: 120, render: (_, row) => row.summary?.open_issue_count ?? row.issues.length },
    { title: '失败执行项', key: 'failed_action_count', width: 120, render: (_, row) => row.summary?.failed_action_count ?? 0 },
    { title: '关联任务', key: 'affected_task_count', width: 120, render: (_, row) => row.summary?.affected_task_count ?? 0 },
    { title: '最近失败', key: 'latest_failure_at', width: 190, render: (_, row) => formatBeijingDateTime(row.summary?.latest_failure_at) },
    {
      title: '处理入口', key: 'actions', width: 300, fixed: 'right', render: (_, row) => {
        const issue = row.issues[0];
        return <TargetRowActions row={row} issue={issue} actions={actions} />;
      },
    },
  ];
}

function TargetRowActions({ row, issue, actions }: { row: TargetWorkbenchRow; issue?: OperationIssue; actions: WorkbenchActions }) {
  return (
    <Space size={6} wrap>
      <Button size="small" icon={<Users size={14} />} onClick={() => actions.onOpenTargets?.(row.targetId)}>目标详情</Button>
      <Button size="small" icon={<Activity size={14} />} onClick={() => isMessageIssue(issue) ? actions.onOpenMessageSending?.() : actions.onOpenTaskDetail?.(issue?.source_task_id)}>{isMessageIssue(issue) ? '消息发送' : '任务详情'}</Button>
      <Button size="small" icon={<ShieldAlert size={14} />} disabled={!issue} onClick={() => issue && actions.onOpenIssue(issue.id)}>查看异常</Button>
      <Button size="small" loading={actions.planBusy === 'create'} disabled={!row.target} onClick={() => row.target && actions.onCreatePlan(row.target)}>创建方案</Button>
    </Space>
  );
}

function issueColumns(actions: WorkbenchActions): ColumnsType<OperationIssue> {
  return [
    { title: '等级', dataIndex: 'severity', width: 90, render: (value) => <Badge tone={severityTone(value)}>{severityLabel(value)}</Badge> },
    { title: '目标', dataIndex: 'target_id', width: 110, render: (value) => value ? `#${value}` : '未绑定' },
    { title: '失败类型', dataIndex: 'failure_type', width: 160, render: (value) => value || '-' },
    { title: '处理方式', dataIndex: 'handling_mode', width: 120, render: (value) => handlingModeLabel(value) },
    { title: '关联任务', dataIndex: 'source_task_id', width: 180, render: (value, issue) => issue.affected_task_count || value || '-' },
    { title: '影响账号', key: 'accounts', width: 100, render: (_, issue) => issue.affected_account_count || issue.affected_account_ids.length },
    { title: '最后出现', dataIndex: 'last_seen_at', width: 190, render: (value) => formatBeijingDateTime(value) },
    {
      title: '操作', key: 'actions', width: 190, fixed: 'right', render: (_, issue) => (
        <Space size={6}>
          <Button size="small" icon={<ShieldAlert size={14} />} onClick={() => actions.onOpenIssue(issue.id)}>展开</Button>
          <Button size="small" icon={<Activity size={14} />} onClick={() => isMessageIssue(issue) ? actions.onOpenMessageSending?.() : actions.onOpenTaskDetail?.(issue.source_task_id)}>{isMessageIssue(issue) ? '消息' : '任务'}</Button>
        </Space>
      ),
    },
  ];
}

function OperationKpi({ label, value, detail }: { label: string; value: number | string; detail: string }) {
  return (
    <div className="operation-kpi-item">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function WorkbenchSummary({ summary }: { summary: OperationCenterSummary | null }) {
  return (
    <>
      <div className="operation-kpi-grid">
        <OperationKpi label="目标异常" value={summary?.open_issue_count ?? 0} detail="open issue" />
        <OperationKpi label="影响目标" value={summary?.affected_target_count ?? 0} detail="需要运营处理" />
        <OperationKpi label="失败执行项" value={summary?.failed_action_count ?? 0} detail="来自汇总读模型" />
        <OperationKpi label="影响账号" value={summary?.affected_account_count ?? 0} detail="关联失败账号" />
      </div>
      {summary?.stale
        ? <Alert className="form-alert" type="warning" showIcon message="汇总数据可能延迟" description={`最近更新时间：${formatBeijingDateTime(summary.latest_updated_at)}`} />
        : <Typography.Text className="operation-updated" type="secondary">最近更新时间：{formatBeijingDateTime(summary?.latest_updated_at)}</Typography.Text>}
    </>
  );
}

export default function OverviewTargetWorkbench(props: Props) {
  const actions: WorkbenchActions = props;
  const rows = React.useMemo(
    () => buildTargetRows(props.targets, props.targetSummaries, props.issues),
    [props.issues, props.targetSummaries, props.targets],
  );
  const targetsColumns = React.useMemo(() => targetColumns(actions), [actions]);
  const issuesColumns = React.useMemo(() => issueColumns(actions), [actions]);
  return (
    <Card className="panel operation-workbench" title="目标工作台" extra={<Button size="small" icon={<RefreshCcw size={14} />} loading={props.operationLoading} onClick={props.onRefresh}>刷新</Button>}>
      {props.operationError && <Alert className="form-alert" type="error" showIcon message="运营中心数据刷新失败" description={props.operationError} />}
      <WorkbenchSummary summary={props.operationSummary} />
      <Table<TargetWorkbenchRow>
        className="tg-table operation-target-table" rowKey="key" columns={targetsColumns} dataSource={rows}
        loading={props.operationLoading} pagination={{ current: props.targetPageQuery.page, pageSize: props.targetPageQuery.pageSize, total: props.targetTotal, showSizeChanger: true }}
        onChange={props.onTargetPageChange} scroll={{ x: 1240 }} locale={{ emptyText: '暂无目标运行摘要。' }}
      />
      <div className="operation-issue-section">
        <div className="section-title-row"><Typography.Title level={4}>目标异常</Typography.Title><Typography.Text type="secondary">按目标聚合失败，展开后看关联任务和代表执行项</Typography.Text></div>
        <Table<OperationIssue> className="tg-table" rowKey="id" columns={issuesColumns} dataSource={props.issues} loading={props.operationLoading} pagination={{ pageSize: 6 }} scroll={{ x: 1020 }} locale={{ emptyText: '暂无待处理目标异常。' }} />
      </div>
    </Card>
  );
}
