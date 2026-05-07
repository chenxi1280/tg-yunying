import React from 'react';
import { Database } from 'lucide-react';
import { Card, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { AuditLog } from '../types';
import { StatusBadge } from '../components/shared';

interface Props {
  audits: AuditLog[];
}

export default function AuditsView({ audits }: Props) {
  const columns: ColumnsType<AuditLog> = [
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 220,
      render: (action: string) => (
        <Space>
          <Database size={16} />
          <Typography.Text strong>{action}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 120,
      render: (_, log) => <StatusBadge status={log.action.includes('失败') ? '失败' : log.action.includes('禁用') ? '禁用' : log.action.includes('查看') ? '待审核' : '已完成'} />,
    },
    {
      title: '对象',
      key: 'target',
      width: 220,
      render: (_, log) => `${log.actor} / ${log.target_type}`,
    },
    {
      title: '详情',
      dataIndex: 'detail',
      key: 'detail',
      render: (detail: string) => detail || '已记录操作',
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (value: string) => new Date(value).toLocaleString(),
    },
  ];

  return (
    <Card className="panel" title="审计安全" extra={<Typography.Text type="secondary">登录、验证码、发送、归档、权限变更都留痕</Typography.Text>}>
      <Table<AuditLog>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={audits}
        pagination={false}
        scroll={{ x: 900 }}
        locale={{ emptyText: '暂无审计记录。配置、登录、卡密和账号池操作会写入这里。' }}
      />
    </Card>
  );
}
