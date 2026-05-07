import React, { useState } from 'react';
import { CheckCircle2, RefreshCcw, ShieldAlert } from 'lucide-react';
import { Button, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import type { ActivationCode, ActivationCodeCreateForm, ActivationCodeFilters, ActivationCodePage, ConfirmPayload } from '../types';
import { FormActions, Modal } from '../components/shared';

interface Props {
  activationCodes: ActivationCode[];
  activationCodePage: ActivationCodePage;
  activationCodeFilters: ActivationCodeFilters;
  setActivationCodeFilters: React.Dispatch<React.SetStateAction<ActivationCodeFilters>>;
  activationBatch: ActivationCodeCreateForm;
  setActivationBatch: React.Dispatch<React.SetStateAction<ActivationCodeCreateForm>>;
  onLoadCodes: (filters?: ActivationCodeFilters, page?: number, pageSize?: number) => Promise<void>;
  onCreateCodes: () => Promise<void>;
  onDisableCode: (code: ActivationCode) => Promise<void>;
  onOpenConfirm: (payload: ConfirmPayload) => void;
}

const EMPTY_FILTERS: ActivationCodeFilters = { search: '', status: '', plan_type: '', batch_no: '', start_at: '', end_at: '' };

function statusLabel(status: string) {
  if (status === 'unused') return '未激活';
  if (status === 'redeemed') return '已激活';
  if (status === 'disabled') return '已停用';
  return status;
}

function planLabel(planType: string) {
  if (planType === 'monthly') return '月卡';
  if (planType === 'yearly') return '年卡';
  return planType;
}

function formatDate(value: string | null) {
  if (!value) return '-';
  return value.replace('T', ' ').slice(0, 16);
}

function statusColor(status: string) {
  if (status === 'unused') return 'gold';
  if (status === 'redeemed') return 'green';
  if (status === 'disabled') return 'default';
  return 'blue';
}

export default function ActivationCodesView({
  activationCodes,
  activationCodePage,
  activationCodeFilters,
  setActivationCodeFilters,
  activationBatch,
  setActivationBatch,
  onLoadCodes,
  onCreateCodes,
  onDisableCode,
  onOpenConfirm,
}: Props) {
  const [createOpen, setCreateOpen] = useState(false);
  const columns: ColumnsType<ActivationCode> = [
    {
      title: '卡密',
      dataIndex: 'code',
      key: 'code',
      width: 250,
      fixed: 'left',
      render: (code: string, item) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text strong copyable>{code}</Typography.Text>
          <Typography.Text type="secondary">{item.note || '无备注'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 96,
      render: (status: string) => <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>,
    },
    {
      title: '套餐 / 有效期',
      key: 'plan',
      width: 130,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text>{planLabel(item.plan_type)}</Typography.Text>
          <Typography.Text type="secondary">{item.duration_days} 天</Typography.Text>
        </Space>
      ),
    },
    {
      title: '批次 / 前缀',
      key: 'batch',
      width: 170,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text>{item.batch_no || '-'}</Typography.Text>
          <Typography.Text type="secondary">{item.serial_prefix || '-'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '生成信息',
      key: 'created',
      width: 180,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text>{item.created_by || '-'}</Typography.Text>
          <Typography.Text type="secondary">{formatDate(item.created_at)}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '激活账号',
      key: 'redeemed_user',
      width: 190,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Typography.Text>{item.redeemed_user_name || '未激活'}</Typography.Text>
          <Typography.Text type="secondary">{item.redeemed_user_email || '-'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '激活时间',
      dataIndex: 'redeemed_at',
      key: 'redeemed_at',
      width: 150,
      render: (value: string | null) => formatDate(value),
    },
    {
      title: '生效区间',
      key: 'subscription',
      width: 250,
      render: (_, item) => `${formatDate(item.subscription_start_at)} ~ ${formatDate(item.subscription_end_at)}`,
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      fixed: 'right',
      render: (_, item) => {
        const canDisable = item.status === 'unused' && !item.redeemed_by_user_id;
        if (!canDisable) return <Typography.Text type="secondary">-</Typography.Text>;
        return (
          <Button danger size="small" icon={<ShieldAlert size={14} />} onClick={() => confirmDisable(item)}>
            停用/废弃
          </Button>
        );
      },
    },
  ];

  const pagination: TablePaginationConfig = {
    current: activationCodePage.page,
    pageSize: activationCodePage.page_size,
    total: activationCodePage.total,
    showSizeChanger: true,
    pageSizeOptions: [20, 50, 100],
    showTotal: (total, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${total} 条`,
  };

  async function applyFilters() {
    await onLoadCodes(activationCodeFilters, 1, activationCodePage.page_size);
  }

  async function clearFilters() {
    setActivationCodeFilters(EMPTY_FILTERS);
    await onLoadCodes(EMPTY_FILTERS, 1, activationCodePage.page_size);
  }

  async function submitCreate() {
    try {
      await onCreateCodes();
      setCreateOpen(false);
    } catch {
      // Context already surfaces the error as a notice.
    }
  }

  function confirmDisable(item: ActivationCode) {
    onOpenConfirm({
      title: '停用卡密',
      message: `确认停用/废弃卡密 ${item.code}？停用后不能再兑换。`,
      confirmLabel: '停用/废弃',
      tone: 'danger',
      onConfirm: () => onDisableCode(item),
    });
  }

  return (
    <section className="panel">
      <div className="section-title">
        <div>
          <h2>卡密管理</h2>
          <span>查询卡密批次、激活账号与有效期，并停用未激活卡密</span>
        </div>
        <div className="row-actions">
          <button className="primary" onClick={() => setCreateOpen(true)}><CheckCircle2 size={16} />生成卡密</button>
        </div>
      </div>

      <div className="policy-grid">
        <label>关键词<input value={activationCodeFilters.search} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, search: event.target.value }))} placeholder="卡密 / 生成者 / 激活账号" /></label>
        <label>状态
          <select value={activationCodeFilters.status} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, status: event.target.value }))}>
            <option value="">全部状态</option>
            <option value="unused">未激活</option>
            <option value="redeemed">已激活</option>
            <option value="disabled">已停用</option>
          </select>
        </label>
        <label>套餐
          <select value={activationCodeFilters.plan_type} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, plan_type: event.target.value }))}>
            <option value="">全部套餐</option>
            <option value="monthly">月卡</option>
            <option value="yearly">年卡</option>
          </select>
        </label>
        <label>批次<input maxLength={24} value={activationCodeFilters.batch_no} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, batch_no: event.target.value.toUpperCase() }))} placeholder="BATCH202605" /></label>
        <label>生成开始<input type="datetime-local" value={activationCodeFilters.start_at} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, start_at: event.target.value }))} /></label>
        <label>生成结束<input type="datetime-local" value={activationCodeFilters.end_at} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, end_at: event.target.value }))} /></label>
        <div className="row-actions wide-field">
          <button className="primary" onClick={applyFilters}><RefreshCcw size={16} />查询</button>
          <button onClick={clearFilters}>清空</button>
        </div>
      </div>

      <div className="table">
        <Table<ActivationCode>
          className="activation-code-table"
          rowKey="id"
          columns={columns}
          dataSource={activationCodes}
          pagination={pagination}
          scroll={{ x: 1550 }}
          locale={{ emptyText: '暂无卡密。点击“生成卡密”创建新的批次。' }}
          onChange={(nextPagination) => {
            void onLoadCodes(
              activationCodeFilters,
              nextPagination.current ?? 1,
              nextPagination.pageSize ?? activationCodePage.page_size,
            );
          }}
        />
      </div>

      {createOpen && (
        <Modal title="生成卡密" size="medium" onClose={() => setCreateOpen(false)}>
          <div className="policy-grid">
            <label>套餐类型
              <select value={activationBatch.plan_type} onChange={(event) => setActivationBatch((current) => ({ ...current, plan_type: event.target.value }))}>
                <option value="monthly">月卡</option>
                <option value="yearly">年卡</option>
              </select>
            </label>
            <label>生成数量<input type="number" min={1} max={200} value={activationBatch.quantity} onChange={(event) => setActivationBatch((current) => ({ ...current, quantity: Number(event.target.value) }))} /></label>
            <label>批次号<input maxLength={24} value={activationBatch.batch_no} onChange={(event) => setActivationBatch((current) => ({ ...current, batch_no: event.target.value.toUpperCase() }))} placeholder="BATCH202605" /></label>
            <label>序列号前缀<input maxLength={24} value={activationBatch.serial_prefix} onChange={(event) => setActivationBatch((current) => ({ ...current, serial_prefix: event.target.value.toUpperCase() }))} placeholder="VIP" /></label>
            <label className="wide-field">备注<input value={activationBatch.note} onChange={(event) => setActivationBatch((current) => ({ ...current, note: event.target.value }))} placeholder="批次用途或来源" /></label>
          </div>
          <FormActions submitLabel="批量生成" onCancel={() => setCreateOpen(false)} onSubmit={submitCreate} disabled={!activationBatch.batch_no.trim() || !activationBatch.serial_prefix.trim() || activationBatch.quantity < 1 || activationBatch.quantity > 200} />
        </Modal>
      )}
    </section>
  );
}
