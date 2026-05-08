import React from 'react';
import { Activity, Bot, CheckCircle2, Database } from 'lucide-react';
import { Card, List, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { CurrentUser, UsageLedger, UsageSummary } from '../types';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';

interface Props {
  usageLedgers: UsageLedger[];
  usageSummary: UsageSummary | null;
  currentUser?: CurrentUser | null;
}

export default function UsageReportsView({ usageLedgers, usageSummary, currentUser }: Props) {
  const usageTable = useAntdTableControls<UsageLedger>({
    rows: usageLedgers,
    placeholder: '搜索模型 / 任务 / 状态 / 费用',
    search: [
      (item) => [
        item.id,
        item.provider_name,
        item.model_name,
        item.campaign_id,
        item.request_status,
        item.total_tokens,
        item.total_cost,
        item.currency,
        item.created_at,
      ],
    ],
  });

  const columns: ColumnsType<UsageLedger> = [
    {
      title: '模型',
      key: 'model',
      width: 220,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.provider_name || 'Mock'}</Typography.Text>
          <Typography.Text type="secondary">{item.model_name} / campaign #{item.campaign_id ?? '-'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, item) => <StatusBadge status={item.request_status === 'success' ? '已完成' : '失败'} />,
    },
    {
      title: 'Token',
      key: 'tokens',
      width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.total_tokens}</Typography.Text>
          <Typography.Text type="secondary">输入 {item.prompt_tokens} / 输出 {item.completion_tokens}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '费用',
      key: 'cost',
      width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.total_cost} {item.currency}</Typography.Text>
          <Typography.Text type="secondary">{item.created_at}</Typography.Text>
        </Space>
      ),
    },
  ];

  return (
    <section className="view-grid">
      <Card className="panel" title="用户用量汇总" extra={<Typography.Text type="secondary">按用户汇总 token 和费用</Typography.Text>}>
        <div className="stats-grid">
          <StatCard label="总请求" value={usageSummary?.total_requests ?? 0} detail="AI 调用次数" icon={<Bot size={22} />} />
          <StatCard label="总 Token" value={usageSummary?.total_tokens ?? 0} detail="输入输出累计" icon={<Activity size={22} />} />
          <StatCard label="总费用" value={`${usageSummary?.total_cost ?? 0} ${usageSummary?.currency ?? 'CNY'}`} detail="按模型单价结算" icon={<Database size={22} />} />
          <StatCard label="计费请求" value={usageSummary?.billable_requests ?? 0} detail="返回 usage 的真实请求" icon={<CheckCircle2 size={22} />} />
          {currentUser?.role !== '系统管理员' && <StatCard label="我的余额" value={currentUser?.token_balance ?? 0} detail={`累计额度 ${currentUser?.token_quota_total ?? 0}`} icon={<Activity size={22} />} />}
        </div>
        <List
          className="mini-list"
          dataSource={usageSummary?.by_user ?? []}
          locale={{ emptyText: '暂无用户用量汇总。' }}
          renderItem={(item) => (
            <List.Item>
              <List.Item.Meta title={item.user_name} description={`请求 ${item.requests} / Token ${item.total_tokens} / 费用 ${item.total_cost} ${item.currency}`} />
            </List.Item>
          )}
        />
      </Card>
      <Card className="panel" title="调用明细" extra={<Typography.Text type="secondary">记录用户、任务、模型、token 和费用</Typography.Text>}>
        <Space className="toolbar-row" wrap>
          {usageTable.searchInput}
        </Space>
        <Table<UsageLedger>
          className="tg-table"
          rowKey="id"
          columns={columns}
          dataSource={usageTable.filteredRows}
          pagination={usageTable.pagination}
          scroll={{ x: 760 }}
          locale={{ emptyText: '暂无调用明细。' }}
        />
      </Card>
    </section>
  );
}
