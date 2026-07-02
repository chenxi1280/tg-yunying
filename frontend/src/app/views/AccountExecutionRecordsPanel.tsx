import React from 'react';
import { Alert, Button, Card, Empty, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { RefreshCcw } from 'lucide-react';
import { api } from '../../shared/api/client';
import { StatusBadge } from '../components/shared';
import { formatBeijingDateTime } from '../time';
import type { AccountExecutionRecord, AccountPendingExecutionRecheck } from '../types';

interface AccountExecutionRecordsPanelProps {
  accountId: number;
  canRecheck: boolean;
  isActionPending: (key: string) => boolean;
}

const ALL_FILTER = '__all__';

function recordTime(record: AccountExecutionRecord) {
  return record.occurred_at ? formatBeijingDateTime(record.occurred_at) : '暂无时间';
}

function optionValues(records: AccountExecutionRecord[], field: 'status_label' | 'action_label') {
  return Array.from(new Set(records.map((record) => record[field]).filter(Boolean))).sort();
}

export function AccountExecutionRecordsPanel({ accountId, canRecheck, isActionPending }: AccountExecutionRecordsPanelProps) {
  const [records, setRecords] = React.useState<AccountExecutionRecord[]>([]);
  const [statusFilter, setStatusFilter] = React.useState(ALL_FILTER);
  const [actionFilter, setActionFilter] = React.useState(ALL_FILTER);
  const [loading, setLoading] = React.useState(false);
  const [recheckResult, setRecheckResult] = React.useState<AccountPendingExecutionRecheck | null>(null);
  const [error, setError] = React.useState('');

  const loadRecords = React.useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextRecords = await api<AccountExecutionRecord[]>(`/tg-accounts/${accountId}/execution-records`);
      setRecords(nextRecords);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '读取执行记录失败');
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  React.useEffect(() => {
    setRecords([]);
    setRecheckResult(null);
    setStatusFilter(ALL_FILTER);
    setActionFilter(ALL_FILTER);
    void loadRecords();
  }, [accountId, loadRecords]);

  async function recheckPendingExecution() {
    setLoading(true);
    setError('');
    try {
      const result = await api<AccountPendingExecutionRecheck>(`/tg-accounts/${accountId}/pending-execution/recheck`, { method: 'POST' });
      setRecheckResult(result);
      await loadRecords();
    } catch (recheckError) {
      setError(recheckError instanceof Error ? recheckError.message : '复检待处理执行失败');
    } finally {
      setLoading(false);
    }
  }

  const filteredRecords = records.filter((record) => (
    (statusFilter === ALL_FILTER || record.status_label === statusFilter) &&
    (actionFilter === ALL_FILTER || record.action_label === actionFilter)
  ));

  const columns: ColumnsType<AccountExecutionRecord> = [
    {
      title: '动作',
      key: 'action',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{record.action_label}</Typography.Text>
          <Typography.Text type="secondary">{record.task_name || record.task_type || record.source}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', key: 'status', width: 130, render: (_, record) => <StatusBadge status={record.status} label={record.status_label} /> },
    { title: '远端消息', dataIndex: 'remote_message_id', width: 150, render: (value: string) => value || '-' },
    { title: '失败原因', key: 'failure', width: 220, render: (_, record) => record.failure_detail || record.failure_type || '-' },
    { title: '时间', key: 'time', width: 190, render: (_, record) => recordTime(record) },
  ];

  return (
    <Card
      className="sub-panel compact-panel"
      title="执行记录"
      extra={(
        <Space wrap>
          <Button size="small" icon={<RefreshCcw size={14} />} loading={loading} onClick={loadRecords}>刷新</Button>
          {canRecheck && <Button size="small" type="primary" icon={<RefreshCcw size={14} />} loading={loading || isActionPending(`account:${accountId}:pending-execution:recheck`)} onClick={recheckPendingExecution}>复检待处理</Button>}
        </Space>
      )}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message="执行记录读取失败" description={error} />}
        {recheckResult && (
          <Alert
            type={recheckResult.blocker_count ? 'warning' : 'success'}
            showIcon
            message="待处理执行已复检"
            description={[
              `检查 ${recheckResult.checked_count} 条，重新排队 ${recheckResult.requeued_count} 条，已在队列 ${recheckResult.existing_pending_count} 条，执行中 ${recheckResult.executing_count} 条。`,
              recheckResult.blockers.map((item) => item.reason).join('；'),
            ].filter(Boolean).join(' ')}
          />
        )}
        <Space wrap>
          <Select value={statusFilter} style={{ width: 160 }} onChange={setStatusFilter} options={[{ value: ALL_FILTER, label: '全部状态' }, ...optionValues(records, 'status_label').map((value) => ({ value, label: value }))]} />
          <Select value={actionFilter} style={{ width: 180 }} onChange={setActionFilter} options={[{ value: ALL_FILTER, label: '全部动作' }, ...optionValues(records, 'action_label').map((value) => ({ value, label: value }))]} />
        </Space>
        <Table<AccountExecutionRecord>
          className="tg-table"
          rowKey="id"
          columns={columns}
          dataSource={filteredRecords}
          pagination={{ pageSize: 8 }}
          loading={loading}
          scroll={{ x: 920 }}
          locale={{ emptyText: <Empty description={error ? '读取失败，不能按 0 条处理' : '暂无执行记录'} /> }}
        />
      </Space>
    </Card>
  );
}
