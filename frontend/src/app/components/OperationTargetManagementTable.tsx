import React from 'react';
import { Alert, Button, Card, Input, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import { RefreshCcw } from 'lucide-react';
import type { OperationTarget } from '../types';
import type { TargetListQuery } from '../hooks/useOperationTargetManagementPage';
import { StatusBadge } from './shared';
import { formatBeijingDateTime } from '../time';
import { OperationTargetLifecycleTag } from './OperationTargetLifecycleActions';

type ColumnOptions = Readonly<{
  canManageTargets: boolean;
  onOpenDetail: (target: OperationTarget) => void;
  onEdit: (target: OperationTarget) => void;
}>;

type Props = Readonly<{
  targets: OperationTarget[];
  query: TargetListQuery;
  total: number;
  search: string;
  loading: boolean;
  error: string;
  syncingAllTargets: boolean;
  canManageTargets: boolean;
  onSearchChange: (value: string) => void;
  onSearch: (value: string) => void;
  onPageChange: (pagination: TablePaginationConfig) => void;
  onRefresh: () => void;
  onSyncAll: () => void;
  onCreate: () => void;
  onOpenDetail: (target: OperationTarget) => void;
  onEdit: (target: OperationTarget) => void;
}>;

export function OperationTargetCapabilityTags({ target }: { target: OperationTarget }) {
  if (!target.task_capabilities.length) {
    return <Typography.Text type="secondary">暂无可创建任务</Typography.Text>;
  }
  return (
    <Space size={[4, 4]} wrap>
      {target.task_capabilities.map((item) => <Tag color="blue" key={item}>{item}</Tag>)}
    </Space>
  );
}

function operationTargetColumns(options: ColumnOptions): ColumnsType<OperationTarget> {
  return [
    {
      title: '目标', key: 'target', render: (_, target) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{target.title}</Typography.Text>
          <Typography.Text type="secondary">{target.target_type === 'channel' ? '频道' : '群聊'} / {target.tg_peer_id}{target.username ? ` / @${target.username}` : ''}</Typography.Text>
        </Space>
      ),
    },
    { title: '人数', dataIndex: 'member_count', key: 'member_count', width: 110 },
    { title: '生命周期', key: 'lifecycle_status', width: 150, render: (_, target) => <OperationTargetLifecycleTag target={target} /> },
    { title: '使用范围', key: 'auth_status', width: 140, render: (_, target) => <StatusBadge status={target.auth_status} /> },
    { title: '发送能力', key: 'can_send', width: 140, render: (_, target) => <StatusBadge status={target.can_send ? '可发送' : '只读'} /> },
    { title: '任务能力', key: 'task_capabilities', width: 240, render: (_, target) => <OperationTargetCapabilityTags target={target} /> },
    { title: '最近同步', key: 'last_sync_at', width: 200, render: (_, target) => target.last_sync_at ? formatBeijingDateTime(target.last_sync_at) : '手动创建' },
    {
      title: '操作', key: 'actions', width: 170, fixed: 'right', render: (_, target) => (
        <Space wrap>
          <Button size="small" onClick={() => options.onOpenDetail(target)}>查看详情</Button>
          {options.canManageTargets && <Button size="small" onClick={() => options.onEdit(target)}>编辑</Button>}
        </Space>
      ),
    },
  ];
}

function ManagementToolbar({ props }: { props: Props }) {
  return (
    <Space className="toolbar-row" wrap>
      <Input.Search
        value={props.search}
        allowClear
        placeholder="搜索群/频道 / peer / username"
        onChange={(event) => props.onSearchChange(event.target.value)}
        onSearch={props.onSearch}
      />
      <Button loading={props.loading} onClick={props.onRefresh}>刷新</Button>
    </Space>
  );
}

export default function OperationTargetManagementTable(props: Props) {
  const columns = React.useMemo(
    () => operationTargetColumns({
      canManageTargets: props.canManageTargets,
      onOpenDetail: props.onOpenDetail,
      onEdit: props.onEdit,
    }),
    [props.canManageTargets, props.onEdit, props.onOpenDetail],
  );
  const extra = (
    <Space wrap>
      {props.canManageTargets && <Button icon={<RefreshCcw size={16} />} loading={props.syncingAllTargets} onClick={props.onSyncAll}>同步全部账号目标</Button>}
      {props.canManageTargets && <Button type="primary" onClick={props.onCreate}>新增目标</Button>}
    </Space>
  );
  return (
    <Card className="panel" title="群/频道目标" extra={extra}>
      <Typography.Text type="secondary">统一维护账号运营目标。群聊用于普通发言，频道用于发帖、查看、点赞和回复任务。</Typography.Text>
      {props.error && <Alert className="form-alert" type="error" showIcon message={props.error} />}
      <ManagementToolbar props={props} />
      <Table<OperationTarget>
        className="tg-table" rowKey="id" columns={columns} dataSource={props.targets}
        pagination={{ current: props.query.page, pageSize: props.query.pageSize, total: props.total, showSizeChanger: true }}
        onChange={props.onPageChange} scroll={{ x: 960 }} loading={props.loading}
      />
    </Card>
  );
}
