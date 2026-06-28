import React from 'react';
import { Descriptions, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TaskCenterDetail } from '../types';

type OnlineSample = NonNullable<TaskCenterDetail['account_online_summary']['samples']>[number];

const BUCKET_LABELS: Record<string, string> = {
  online: '在线',
  warming: '预热中',
  recovering: '恢复中',
  relogin_required: '需重登',
  blocked: '阻断',
  stale: 'Stale',
  offline: '离线',
  missing_state: '缺状态',
};

function bucketLabel(bucket: string) {
  return BUCKET_LABELS[bucket] || bucket;
}

export function TaskAccountOnlineSummaryPanel({ summary }: { summary: TaskCenterDetail['account_online_summary'] }) {
  const samples = summary.samples || [];
  const sampleColumns: ColumnsType<OnlineSample> = [
    { title: '账号', dataIndex: 'account_id', width: 90 },
    { title: '状态', dataIndex: 'bucket', width: 100, render: (bucket) => <Tag>{bucketLabel(String(bucket))}</Tag> },
    { title: '失败类型', dataIndex: 'failure_type', width: 140 },
    { title: '失败详情', dataIndex: 'failure_detail', ellipsis: true },
    { title: 'Stale 时间', dataIndex: 'stale_after_at', width: 160, render: (value) => value ? String(value).replace('T', ' ').slice(0, 16) : '-' },
  ];
  if (!summary.desired_count) return null;
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Descriptions
        bordered
        size="small"
        column={4}
        items={[
          { key: 'desired', label: '应在线', children: summary.desired_count || 0 },
          { key: 'online', label: '当前在线', children: summary.online_count || 0 },
          { key: 'warming', label: '预热中', children: summary.warming_count || 0 },
          { key: 'stale', label: 'Stale/掉线', children: (summary.stale_count || 0) + (summary.offline_count || 0) },
          { key: 'recovering', label: '恢复中', children: summary.recovering_count || 0 },
          { key: 'relogin', label: '需重登', children: summary.relogin_required_count || 0 },
          { key: 'blocked', label: '阻断', children: summary.blocked_count || 0 },
          { key: 'missing', label: '缺状态', children: summary.missing_state_count || 0 },
        ]}
      />
      {samples.length > 0 && (
        <>
          <Typography.Text type="secondary">在线异常样例</Typography.Text>
          <Table rowKey={(row) => `${row.account_id}:${row.bucket}`} columns={sampleColumns} dataSource={samples} pagination={false} size="small" scroll={{ x: 860 }} />
        </>
      )}
    </Space>
  );
}
