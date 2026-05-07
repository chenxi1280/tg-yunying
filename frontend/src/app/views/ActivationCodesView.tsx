import React, { useState } from 'react';
import { CheckCircle2, RefreshCcw, ShieldAlert } from 'lucide-react';
import { Button, Card, Input, InputNumber, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import type { ActivationCode, ActivationCodeCreateForm, ActivationCodeFilters, ActivationCodePage, ConfirmPayload, SubscriptionPlan } from '../types';
import { FormActions, Modal } from '../components/shared';

interface Props {
  activationCodes: ActivationCode[];
  subscriptionPlans: SubscriptionPlan[];
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
  subscriptionPlans,
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
  const selectedPlan = subscriptionPlans.find((plan) => plan.id === activationBatch.plan_id)
    ?? subscriptionPlans.find((plan) => plan.plan_type === activationBatch.plan_type)
    ?? null;
  const columns: ColumnsType<ActivationCode> = [
    {
      title: '卡密',
      dataIndex: 'code',
      key: 'code',
      width: 250,
      fixed: 'left',
      render: (code: string, item) => (
        <Space direction="vertical" size={2}>
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
        <Space direction="vertical" size={2}>
          <Typography.Text>{planLabel(item.plan_type)}</Typography.Text>
          <Typography.Text type="secondary">{item.duration_days} 天 / {item.token_quota.toLocaleString()} Token</Typography.Text>
        </Space>
      ),
    },
    {
      title: '批次 / 前缀',
      key: 'batch',
      width: 170,
      render: (_, item) => (
        <Space direction="vertical" size={2}>
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
        <Space direction="vertical" size={2}>
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
        <Space direction="vertical" size={2}>
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
    <Card className="panel" title="卡密管理" extra={<Button type="primary" icon={<CheckCircle2 size={16} />} onClick={() => setCreateOpen(true)}>生成卡密</Button>}>
      <Typography.Text type="secondary">查询卡密批次、激活账号与有效期，并停用未激活卡密</Typography.Text>

      <div className="policy-grid">
        <label>关键词<Input value={activationCodeFilters.search} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, search: event.target.value }))} placeholder="卡密 / 生成者 / 激活账号" /></label>
        <label>状态<Select value={activationCodeFilters.status} onChange={(value) => setActivationCodeFilters((current) => ({ ...current, status: value }))} options={[{ value: '', label: '全部状态' }, { value: 'unused', label: '未激活' }, { value: 'redeemed', label: '已激活' }, { value: 'disabled', label: '已停用' }]} /></label>
        <label>套餐<Select value={activationCodeFilters.plan_type} onChange={(value) => setActivationCodeFilters((current) => ({ ...current, plan_type: value }))} options={[{ value: '', label: '全部套餐' }, ...subscriptionPlans.map((plan) => ({ value: plan.plan_type, label: plan.name }))]} /></label>
        <label>批次<Input maxLength={24} value={activationCodeFilters.batch_no} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, batch_no: event.target.value.toUpperCase() }))} placeholder="BATCH202605" /></label>
        <label>生成开始<Input type="datetime-local" value={activationCodeFilters.start_at} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, start_at: event.target.value }))} /></label>
        <label>生成结束<Input type="datetime-local" value={activationCodeFilters.end_at} onChange={(event) => setActivationCodeFilters((current) => ({ ...current, end_at: event.target.value }))} /></label>
        <Space className="row-actions wide-field">
          <Button type="primary" icon={<RefreshCcw size={16} />} onClick={applyFilters}>查询</Button>
          <Button onClick={clearFilters}>清空</Button>
        </Space>
      </div>

      <Table<ActivationCode>
        className="activation-code-table tg-table"
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

      {createOpen && (
        <Modal title="生成卡密" size="medium" onClose={() => setCreateOpen(false)}>
          <div className="policy-grid">
            <label>套餐<Select<number | ''> value={activationBatch.plan_id} onChange={(value) => {
              const plan = subscriptionPlans.find((item) => item.id === value);
              setActivationBatch((current) => ({ ...current, plan_id: value, plan_type: plan?.plan_type ?? current.plan_type }));
            }} options={[{ value: '', label: '按类型兼容创建' }, ...subscriptionPlans.filter((plan) => plan.is_active).map((plan) => ({ value: plan.id, label: `${plan.name} / ${plan.duration_days} 天 / ${plan.token_quota.toLocaleString()} Token` }))]} /></label>
            <label>兼容类型<Select value={activationBatch.plan_type} onChange={(value) => setActivationBatch((current) => ({ ...current, plan_type: value, plan_id: '' }))} options={subscriptionPlans.length ? subscriptionPlans.map((plan) => ({ value: plan.plan_type, label: plan.name })) : [{ value: 'monthly', label: '月卡' }, { value: 'yearly', label: '年卡' }]} /></label>
            <label>生成数量<InputNumber min={1} max={200} value={activationBatch.quantity} onChange={(value) => setActivationBatch((current) => ({ ...current, quantity: Number(value ?? 1) }))} /></label>
            <label>批次号<Input maxLength={24} value={activationBatch.batch_no} onChange={(event) => setActivationBatch((current) => ({ ...current, batch_no: event.target.value.toUpperCase() }))} placeholder="BATCH202605" /></label>
            <label>序列号前缀<Input maxLength={24} value={activationBatch.serial_prefix} onChange={(event) => setActivationBatch((current) => ({ ...current, serial_prefix: event.target.value.toUpperCase() }))} placeholder="VIP" /></label>
            <label className="wide-field">备注<Input value={activationBatch.note} onChange={(event) => setActivationBatch((current) => ({ ...current, note: event.target.value }))} placeholder="批次用途或来源" /></label>
            {selectedPlan && <p className="muted-line wide-field">将快照写入卡密：{selectedPlan.name} / {selectedPlan.duration_days} 天 / {selectedPlan.token_quota.toLocaleString()} Token</p>}
          </div>
          <FormActions submitLabel="批量生成" onCancel={() => setCreateOpen(false)} onSubmit={submitCreate} disabled={!activationBatch.batch_no.trim() || !activationBatch.serial_prefix.trim() || activationBatch.quantity < 1 || activationBatch.quantity > 200} />
        </Modal>
      )}
    </Card>
  );
}
