import React from 'react';
import { Database } from 'lucide-react';
import { App as AntdApp, Button, Card, Descriptions, Input, Modal, Select, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { AuditFilters, AuditLog } from '../types';
import { DetailModal, StatusBadge, useAntdTableControls } from '../components/shared';
import { API_BASE, apiErrorFromResponse } from '../../shared/api/client';
import { formatBeijingDateTime } from '../time';

interface Props {
  audits: AuditLog[];
  filters: AuditFilters;
  setFilters: React.Dispatch<React.SetStateAction<AuditFilters>>;
  onRefresh: () => Promise<void>;
  canExport?: boolean;
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

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export default function AuditsView({ audits, filters, setFilters, onRefresh, canExport = true }: Props) {
  const { message } = AntdApp.useApp();
  const [selectedAudit, setSelectedAudit] = React.useState<AuditLog | null>(null);
  const [exporting, setExporting] = React.useState(false);
  const [exportReasonOpen, setExportReasonOpen] = React.useState(false);
  const [exportReason, setExportReason] = React.useState('');
  const auditTable = useAntdTableControls<AuditLog>({
    rows: audits,
    placeholder: '搜索动作 / 操作人 / 对象 / 详情',
    search: [
      (log) => [
        log.id,
        log.action,
        log.actor,
        log.target_type,
        log.account_display_name,
        log.account_phone_number,
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
      render: (_, log) => log.account_phone_number ? `${log.target_type} / ${log.target_id} / ${log.account_phone_number}` : `${log.target_type} / ${log.target_id}`,
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
      render: (value: string) => formatBeijingDateTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      fixed: 'right',
      render: (_, log) => <Button size="small" onClick={() => setSelectedAudit(log)}>详情</Button>,
    },
  ];

  async function exportCsv(reason: string) {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.set(key, value);
      });
      params.set('reason', reason.trim());
      const token = localStorage.getItem('tg_ops_token');
      const response = await fetch(`${API_BASE}/audit-logs/export?${params.toString()}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      });
      if (!response.ok) throw await apiErrorFromResponse(response);
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `audit-logs-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      window.URL.revokeObjectURL(url);
      setExportReasonOpen(false);
      setExportReason('');
    } catch (error) {
      void message.error(`导出审计记录失败：${errorText(error)}`);
    } finally {
      setExporting(false);
    }
  }

  return (
    <>
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
          {canExport && <Button loading={exporting} onClick={() => setExportReasonOpen(true)}>导出 CSV</Button>}
          <Button onClick={() => setFilters({ actor: '', action: '', target_type: '', target_id: '', keyword: '', account_id: '', operation_target_id: '', task_id: '', status: '', start_at: '', end_at: '' })}>清空</Button>
        </Space>
        <Table<AuditLog>
          className="tg-table"
          rowKey="id"
          columns={columns}
          dataSource={auditTable.filteredRows}
          pagination={auditTable.pagination}
          scroll={{ x: 1000 }}
          locale={{ emptyText: '暂无审计记录。配置、登录、任务、规则、发送和账号池操作会写入这里。' }}
        />
      </Card>
      <DetailModal title={selectedAudit ? `审计详情 #${selectedAudit.id}` : '审计详情'} open={Boolean(selectedAudit)} size="medium" onClose={() => setSelectedAudit(null)}>
        {selectedAudit && (
          <Descriptions
            bordered
            size="small"
            column={1}
            items={[
              { key: 'status', label: '状态', children: <StatusBadge status={selectedAudit.action.includes('失败') ? '失败' : selectedAudit.action.includes('取消') || selectedAudit.action.includes('停止') ? '已停止' : '已完成'} /> },
              { key: 'time', label: '时间', children: formatBeijingDateTime(selectedAudit.created_at) },
              { key: 'actor', label: '操作人', children: selectedAudit.actor },
              { key: 'ip', label: '来源 IP', children: selectedAudit.ip_address || '-' },
              { key: 'action', label: '动作', children: selectedAudit.action },
              { key: 'target', label: '关联对象', children: `${selectedAudit.target_type} / ${selectedAudit.target_id}` },
              { key: 'account', label: '账号手机号', children: selectedAudit.account_phone_number ? `${selectedAudit.account_display_name || '账号'} / ${selectedAudit.account_phone_number}` : '-' },
              { key: 'detail', label: '记录详情', children: selectedAudit.detail || '无补充详情' },
              { key: 'trace', label: '追溯口径', children: auditTraceText(selectedAudit) },
            ]}
          />
        )}
      </DetailModal>
      <Modal
        title="导出审计记录"
        open={exportReasonOpen}
        okText="导出 CSV"
        cancelText="取消"
        okButtonProps={{ disabled: !exportReason.trim() }}
        confirmLoading={exporting}
        onOk={() => exportCsv(exportReason)}
        onCancel={() => setExportReasonOpen(false)}
        destroyOnHidden
        centered
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text>导出审计记录会额外写入一条导出审计。</Typography.Text>
          <Input.TextArea
            rows={3}
            value={exportReason}
            maxLength={255}
            showCount
            placeholder="填写导出原因"
            onChange={(event) => setExportReason(event.target.value)}
          />
        </Space>
      </Modal>
    </>
  );
}

function auditTraceText(log: AuditLog): string {
  if (log.target_type === 'task') return `可使用任务 ID ${log.target_id} 回到任务中心查看执行项、失败原因和重试记录。`;
  if (log.target_type === 'operation_target') return `可使用目标 ID ${log.target_id} 回到运营目标详情查看账号覆盖、历史任务和发送记录。`;
  if (log.target_type === 'rule_set') return `可使用规则集 ID ${log.target_id} 回到规则中心查看版本、过滤、转换、路由和发布记录。`;
  if (log.target_type === 'tg_account') return `可使用账号 ID ${log.target_id} 回到账号中心查看登录、同步、健康和群频道覆盖。`;
  return '该记录保留了动作、对象、操作人、时间和详情，可配合对象 ID 在对应模块继续追溯。';
}
