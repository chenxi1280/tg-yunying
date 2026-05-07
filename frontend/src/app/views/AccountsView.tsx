import React from 'react';
import { Alert, Avatar, Button, Card, Progress, Segmented, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { Account, AccountPool } from '../types';
import type { RuntimeConfig } from '../types';
import { StatusBadge } from '../components/shared';

const LOGIN_REQUIRED_STATUSES = new Set(['待登录', '等待验证码', '等待2FA', '需重新登录', '异常']);

interface Props {
  accounts: Account[];
  accountPools: AccountPool[];
  selectedPoolId: number | '';
  setSelectedPoolId: (id: number | '') => void;
  selectedPool: AccountPool | undefined;
  avatarUrl: (value: string) => string;
  runtime: RuntimeConfig | null;
  onConfigureDeveloperApps: () => void;
  onCreatePoolClick: () => void;
  onCreateAccount: (login: boolean) => void;
  onOpenPoolDetail: (pool: AccountPool) => void;
  onOpenAccountDetail: (account: Account) => void;
  onRunLogin: (account: Account, method: 'code' | 'qr') => void;
  onVerifyAccount: (account: Account) => void;
  onHealthCheck: (account: Account) => void;
  onSyncGroups: (account: Account) => void;
}

export default function AccountsView({
  accounts,
  accountPools,
  selectedPoolId,
  setSelectedPoolId,
  selectedPool,
  avatarUrl,
  runtime,
  onConfigureDeveloperApps,
  onCreatePoolClick,
  onCreateAccount,
  onOpenPoolDetail,
  onOpenAccountDetail,
  onRunLogin,
  onVerifyAccount,
  onHealthCheck,
  onSyncGroups,
}: Props) {
  const columns: ColumnsType<Account> = [
    {
      title: '账号',
      dataIndex: 'display_name',
      key: 'account',
      width: 280,
      fixed: 'left',
      render: (_, account) => (
        <Space>
          <Avatar src={account.avatar_preview_url ? avatarUrl(account.avatar_preview_url) : undefined}>
            {account.display_name.slice(0, 1)}
          </Avatar>
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{account.display_name}</Typography.Text>
            <Typography.Text type="secondary">@{account.username ?? '未设置'} / {account.phone_masked}</Typography.Text>
            <Typography.Text type="secondary">账号池：{account.pool_name}</Typography.Text>
            <Typography.Text type="secondary">昵称：{[account.tg_first_name, account.tg_last_name].filter(Boolean).join(' ') || '未设置'}</Typography.Text>
          </Space>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 150,
      render: (_, account) => (
        <Space direction="vertical" size={4}>
          <StatusBadge status={account.status} />
          <StatusBadge status={account.profile_sync_status} label={`资料 ${account.profile_sync_status}`} />
        </Space>
      ),
    },
    {
      title: '底层连接',
      key: 'developer',
      width: 190,
      render: (_, account) => (
        <Space direction="vertical" size={2}>
          <Typography.Text strong>{account.developer_app_name ? '正常' : '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">{account.developer_app_name ? '登录能力已分配' : '登录时自动准备'}</Typography.Text>
          <StatusBadge status={account.developer_app_health_status ?? '未配置'} label={account.developer_app_health_status === '健康' ? '正常' : account.developer_app_health_status ?? '未配置'} />
        </Space>
      ),
    },
    {
      title: '健康分',
      dataIndex: 'health_score',
      key: 'health_score',
      width: 150,
      render: (score: number) => <Progress percent={score} size="small" status={score < 60 ? 'exception' : score < 80 ? 'normal' : 'success'} />,
    },
    {
      title: '操作',
      key: 'actions',
      width: 280,
      fixed: 'right',
      render: (_, account) => (
        <Space wrap>
          {LOGIN_REQUIRED_STATUSES.has(account.status) ? (
            <Button type="primary" size="small" onClick={() => onVerifyAccount(account)}>完成登录</Button>
          ) : account.status === '在线' ? (
            <>
              <Button type="primary" size="small" onClick={() => onOpenAccountDetail(account)}>详情</Button>
              <Button size="small" onClick={() => onRunLogin(account, 'qr')}>扫码</Button>
              <Button size="small" onClick={() => onHealthCheck(account)}>检查</Button>
              <Button size="small" onClick={() => onSyncGroups(account)}>同步群</Button>
            </>
          ) : (
            <Button type="primary" size="small" onClick={() => onOpenAccountDetail(account)}>详情</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card
      className="panel"
      title="TG 账号池"
      extra={(
        <Space wrap>
          <Button onClick={onCreatePoolClick}>新增账号池</Button>
          <Button disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(false)}>新增账号</Button>
          <Button type="primary" disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(true)}>新增登录账号</Button>
        </Space>
      )}
    >
      <Typography.Text type="secondary">按客户账号池分组管理，登录、资料、克隆和验证辅助都在账号详情中处理</Typography.Text>
      {!runtime?.can_create_tg_account && (
        <Alert
          className="sub-panel compact-panel"
          type="warning"
          showIcon
          message="请先配置开发者应用"
          description="新增 TG 账号需要可用的 Telegram api_id/api_hash。配置完成并保持健康后，账号新增和登录入口会自动启用。"
          action={<Button type="primary" size="small" onClick={onConfigureDeveloperApps}>去配置开发者应用</Button>}
        />
      )}
      <Space className="pool-filter-strip" wrap>
        <Segmented
          value={selectedPoolId === '' ? 'all' : String(selectedPoolId)}
          onChange={(value) => setSelectedPoolId(value === 'all' ? '' : Number(value))}
          options={[
            { label: '全部账号池', value: 'all' },
            ...accountPools.map((pool) => ({ label: `${pool.name} ${pool.account_count}`, value: String(pool.id) })),
          ]}
        />
        {selectedPool && <Button type="primary" onClick={() => onOpenPoolDetail(selectedPool)}>进入账号池</Button>}
      </Space>
      <Table<Account>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        pagination={false}
        scroll={{ x: 1050 }}
        locale={{ emptyText: '暂无 TG 账号。配置开发者应用后，可以通过手机号新增账号并启动真实 TG 登录。' }}
      />
    </Card>
  );
}
