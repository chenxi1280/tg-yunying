import React from 'react';
import { Card, Descriptions, Table, Typography } from 'antd';
import type { TaskRecentSuccess } from '../types/taskCenter';
import { formatDateTime } from './taskCenterViewModel';

export function TaskRecentSuccessPanel({ stats }: { stats?: TaskRecentSuccess }) {
  if (!stats) return null;
  return (
    <Card size="small" title={`最近 ${stats.window_hours} 小时确认成功`}>
      <Descriptions size="small" column={2} items={[
        { key: 'total', label: stats.metric_label, children: `${stats.success_count} 次` },
        { key: 'accounts', label: '成功账号数', children: stats.account_counts.length },
        { key: 'window', label: '统计区间', span: 2,
          children: `${formatDateTime(stats.window_start)} 至 ${formatDateTime(stats.window_end)}` },
        ...(stats.unassigned_count ? [{ key: 'unassigned', label: '未归属账号', children: `${stats.unassigned_count} 次` }] : []),
      ]} />
      <Typography.Paragraph type="secondary">
        按原调用确认时间统计，同一动作只计一次；不包含待执行、失败和结果未知。
        自动化点赞、浏览操作不代表真人热度或平台计数器增量。
      </Typography.Paragraph>
      <Table size="small" rowKey="account_id" dataSource={stats.account_counts}
        pagination={stats.account_counts.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
        locale={{ emptyText: '该时间段没有已确认成功的账号' }}
        columns={[
          { title: '账号', dataIndex: 'account_id', render: (id: number) => `#${id}` },
          { title: stats.metric_label, dataIndex: 'success_count', render: (count: number) => `${count} 次` },
        ]} />
    </Card>
  );
}
