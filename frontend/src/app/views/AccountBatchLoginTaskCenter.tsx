import React from 'react';
import { Alert, Button, Drawer, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { AccountBatchLogin, AccountPool } from '../types';
import { formatBeijingDateTime } from '../time';
import { isActiveLoginBatch, loginStatusColor, loginStatusLabel } from './accountBatchLoginPresentation';

const TASK_LIST_POLL_MS = 5_000;
const TASK_LIST_LIMIT = 200;

interface Props {
  open: boolean;
  pools: AccountPool[];
  refreshToken: number;
  onClose: () => void;
  onOpenBatch: (batchId: number) => void;
  onActiveCountChange: (count: number) => void;
}

export function AccountBatchLoginTaskCenter(props: Props) {
  const { batches, error, loading, reload } = useBatchLoginTasks(props.refreshToken, props.onActiveCountChange);
  const rows = sortBatches(batches);
  return (
    <Drawer title="登录任务" open={props.open} width={980} onClose={props.onClose}>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Space style={{ marginBottom: 12 }}>
        <Button loading={loading} onClick={() => void reload()}>刷新</Button>
        <Typography.Text type="secondary">运行中的批次会持续显示，关闭详情或刷新页面后仍可从这里恢复。</Typography.Text>
      </Space>
      <Table rowKey="id" columns={taskColumns(props.pools, props.onOpenBatch)} dataSource={rows} loading={loading && !rows.length} pagination={{ pageSize: 20 }} scroll={{ x: 900 }} />
    </Drawer>
  );
}

function useBatchLoginTasks(refreshToken: number, onActiveCountChange: (count: number) => void) {
  const [batches, setBatches] = React.useState<AccountBatchLogin[]>([]);
  const [error, setError] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const requestSequence = React.useRef(0);
  const reload = React.useCallback(async () => {
    const requestId = ++requestSequence.current;
    setLoading(true);
    try {
      const rows = await api<AccountBatchLogin[]>(`/tg-accounts/login-batches?limit=${TASK_LIST_LIMIT}&offset=0`);
      if (requestId !== requestSequence.current) return;
      setBatches(rows);
      onActiveCountChange(rows.filter((batch) => isActiveLoginBatch(batch.status)).length);
      setError('');
    } catch (requestError) {
      if (requestId !== requestSequence.current) return;
      setError(requestError instanceof Error ? requestError.message : '读取登录任务失败');
    } finally {
      if (requestId === requestSequence.current) setLoading(false);
    }
  }, [onActiveCountChange]);
  React.useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), TASK_LIST_POLL_MS);
    return () => {
      requestSequence.current += 1;
      window.clearInterval(timer);
    };
  }, [refreshToken, reload]);
  return { batches, error, loading, reload };
}

function sortBatches(batches: AccountBatchLogin[]) {
  return [...batches].sort((left, right) => {
    const activeOrder = Number(isActiveLoginBatch(right.status)) - Number(isActiveLoginBatch(left.status));
    return activeOrder || right.id - left.id;
  });
}

function taskColumns(pools: AccountPool[], onOpenBatch: (batchId: number) => void): ColumnsType<AccountBatchLogin> {
  return [
    { title: '批次', width: 90, render: (_, batch) => `#${batch.id}` },
    { title: '状态', width: 120, render: (_, batch) => <Tag color={loginStatusColor(batch.status)}>{loginStatusLabel(batch.status)}</Tag> },
    { title: '目标分组', width: 150, render: (_, batch) => pools.find((pool) => pool.id === batch.pool_id)?.name || `#${batch.pool_id}` },
    { title: '创建人', dataIndex: 'created_by', width: 120 },
    { title: '进度', width: 250, render: (_, batch) => batchProgress(batch) },
    { title: '创建时间', width: 180, render: (_, batch) => formatBeijingDateTime(batch.created_at) },
    { title: '操作', fixed: 'right', width: 110, render: (_, batch) => <Button size="small" onClick={() => onOpenBatch(batch.id)}>查看详情</Button> },
  ];
}

function batchProgress(batch: AccountBatchLogin) {
  const settled = batch.success_count + batch.failed_count + batch.unresolved_count + batch.skipped_count;
  return `${settled}/${batch.total_count}；已授权 ${batch.authorized_count} / 完整初始化 ${batch.fully_initialized_count} / 等待 ${batch.post_init_waiting_count} / 失败 ${batch.failed_count} / 未解 ${batch.unresolved_count}`;
}
