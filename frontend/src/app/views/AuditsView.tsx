import React from 'react';
import { Database } from 'lucide-react';
import { Button, Card, Input, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { AuditFilters, AuditLog } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

interface Props {
  audits: AuditLog[];
  filters: AuditFilters;
  setFilters: React.Dispatch<React.SetStateAction<AuditFilters>>;
  onRefresh: () => Promise<void>;
}

const TARGET_TYPE_OPTIONS = [
  { value: 'tg_account', label: 'TG 账号' },
  { value: 'operation_target', label: '运营目标' },
  { value: 'task', label: '任务中心' },
  { value: 'rule_set', label: '规则集' },
  { value: 'message_task', label: '消息发送' },
  { value: 'group_archive', label: '归档' },
  { value: 'audit', label: '审计' },
];

export default function AuditsView({ audits, filters, setFilters, onRefresh }: Props) {
  const auditTable = useAntdTableControls<AuditLog>({
    rows: audits,
    placeholder: '搜索动作 / 操作人 / 对象 / 详情',
    search: [
      (log) => [
        log.id,
        log.action,
        log.actor,
        log.target_type,
        log.detail,
        log.created_at,
      ],
    ],
  });

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
      render: (_, log) => <StatusBadge status={log.action.includes('失败') ? '失败' : log.action.includes('禁用') ? '禁用' : log.action.includes('查看') ? '已查看' : '已完成'} />,
    },
    {
      title: '对象',
      key: 'target',
      width: 220,
      render: (_, log) => `${log.target_type} / ${log.target_id}`,
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
    <Card className="panel" title="审计记录" extra={<Typography.Text type="secondary">登录、验证码、自动生成、规则命中、发送、跳过、失败和归档都留痕</Typography.Text>}>
      <Space className="toolbar-row" wrap>
        {auditTable.searchInput}
        <Input placeholder="操作人" value={filters.actor} onChange={(event) => setFilters((current) => ({ ...current, actor: event.target.value }))} style={{ width: 130 }} />
        <Input placeholder="动作" value={filters.action} onChange={(event) => setFilters((current) => ({ ...current, action: event.target.value }))} style={{ width: 150 }} />
        <Select allowClear placeholder="对象类型" value={filters.target_type || undefined} onChange={(value) => setFilters((current) => ({ ...current, target_type: value ?? '' }))} options={TARGET_TYPE_OPTIONS} style={{ width: 150 }} />
        <Input placeholder="对象 ID" value={filters.target_id} onChange={(event) => setFilters((current) => ({ ...current, target_id: event.target.value }))} style={{ width: 120 }} />
        <Input placeholder="账号 ID" value={filters.account_id} onChange={(event) => setFilters((current) => ({ ...current, account_id: event.target.value }))} style={{ width: 120 }} />
        <Input placeholder="目标 ID" value={filters.operation_target_id} onChange={(event) => setFilters((current) => ({ ...current, operation_target_id: event.target.value }))} style={{ width: 120 }} />
        <Input placeholder="任务 ID" value={filters.task_id} onChange={(event) => setFilters((current) => ({ ...current, task_id: event.target.value }))} style={{ width: 160 }} />
        <Select allowClear placeholder="状态" value={filters.status || undefined} onChange={(value) => setFilters((current) => ({ ...current, status: value ?? '' }))} options={[{ value: 'success', label: '成功/完成' }, { value: 'failed', label: '失败/异常' }, { value: 'skipped', label: '跳过/停止' }]} style={{ width: 140 }} />
        <Input placeholder="关键词" value={filters.keyword} onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))} style={{ width: 160 }} />
        <Input type="datetime-local" value={filters.start_at} onChange={(event) => setFilters((current) => ({ ...current, start_at: event.target.value }))} style={{ width: 190 }} />
        <Input type="datetime-local" value={filters.end_at} onChange={(event) => setFilters((current) => ({ ...current, end_at: event.target.value }))} style={{ width: 190 }} />
        <Button type="primary" onClick={onRefresh}>应用筛选</Button>
        <Button onClick={() => setFilters({ actor: '', action: '', target_type: '', target_id: '', keyword: '', account_id: '', operation_target_id: '', task_id: '', status: '', start_at: '', end_at: '' })}>清空</Button>
      </Space>
      <Table<AuditLog>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={auditTable.filteredRows}
        pagination={auditTable.pagination}
        scroll={{ x: 900 }}
        locale={{ emptyText: '暂无审计记录。配置、登录、任务、规则、发送和账号池操作会写入这里。' }}
      />
    </Card>
  );
}
