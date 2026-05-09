import React from 'react';
import { Alert, Avatar, Button, Card, Progress, Segmented, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { Account, AccountPool } from '../types';
import type { RuntimeConfig } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

const LOGIN_REQUIRED_STATUSES = new Set(['待登录', '等待验证码', '等待扫码', '等待2FA', '需重新登录', '异常']);
const accountPhone = (account: Account) => account.phone_number || account.phone_masked;

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
  onExtractCodes: (account: Account) => void;
  onMovePool: (account: Account) => void;
  onRunLogin: (account: Account, method: 'code' | 'qr') => void;
  onVerifyAccount: (account: Account) => void;
  onDeleteAccount: (account: Account) => void;
  onHealthCheck: (account: Account) => void;
  onSyncGroups: (account: Account) => void;
  isActionPending: (key: string) => boolean;
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
  onExtractCodes,
  onMovePool,
  onRunLogin,
  onVerifyAccount,
  onDeleteAccount,
  onHealthCheck,
  onSyncGroups,
  isActionPending,
}: Props) {
  const accountTable = useAntdTableControls<Account>({
    rows: accounts,
    placeholder: '搜索账号 / username / 手机号 / 分组 / 状态',
    search: [
      (account) => [
        account.id,
        account.display_name,
        account.username,
        accountPhone(account),
        account.pool_name,
        account.status,
        account.profile_sync_status,
        account.developer_app_name,
        account.developer_app_health_status,
        account.tg_first_name,
        account.tg_last_name,
      ],
    ],
  });

  const columns: ColumnsType<Account> = [
    {
      title: '账号',
      dataIndex: 'display_name',
      key: 'account',
      width: 360,
      fixed: 'left',
      render: (_, account) => (
        <Space>
          <Avatar src={account.avatar_preview_url ? avatarUrl(account.avatar_preview_url) : undefined}>
            {account.display_name.slice(0, 1)}
          </Avatar>
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{account.display_name}</Typography.Text>
            <Typography.Text type="secondary">@{account.username ?? '未设置'} / {accountPhone(account)}</Typography.Text>
            <Typography.Text type="secondary">账号分组：{account.pool_name}</Typography.Text>
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
            <>
              <Button type="primary" size="small" onClick={() => onVerifyAccount(account)}>{account.status === '待登录' ? '完成登录' : '继续登录'}</Button>
              <Button danger size="small" loading={isActionPending(`account:${account.id}:delete`)} onClick={() => onDeleteAccount(account)}>移除</Button>
            </>
          ) : account.status === '在线' ? (
            <>
              <Button type="primary" size="small" loading={isActionPending(`account:${account.id}:detail`)} onClick={() => onOpenAccountDetail(account)}>详情</Button>
              <Button size="small" loading={isActionPending(`account:${account.id}:codes`)} onClick={() => onExtractCodes(account)}>提取验证码</Button>
              <Button size="small" loading={isActionPending(`account:${account.id}:move-pool`)} onClick={() => onMovePool(account)}>移动分组</Button>
              <Button size="small" loading={isActionPending(`account:${account.id}:health`)} onClick={() => onHealthCheck(account)}>检查</Button>
              <Button size="small" loading={isActionPending(`account:${account.id}:sync`)} onClick={() => onSyncGroups(account)}>同步</Button>
            </>
          ) : (
            <>
              <Button type="primary" size="small" loading={isActionPending(`account:${account.id}:detail`)} onClick={() => onOpenAccountDetail(account)}>详情</Button>
              <Button size="small" loading={isActionPending(`account:${account.id}:move-pool`)} onClick={() => onMovePool(account)}>移动分组</Button>
            </>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card
      className="panel"
      title="TG 账号管理"
      extra={(
        <Space wrap>
          <Button onClick={onCreatePoolClick}>新增账号分组</Button>
          <Button disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(false)}>新增账号</Button>
        </Space>
      )}
    >
      <Typography.Text type="secondary">按账号分组管理，登录、资料、克隆和验证辅助都在账号详情中处理</Typography.Text>
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
            { label: '全部账号分组', value: 'all' },
            ...accountPools.map((pool) => ({ label: `${pool.name} ${pool.account_count}`, value: String(pool.id) })),
          ]}
        />
        {selectedPool && <Button type="primary" loading={isActionPending(`account-pool:${selectedPool.id}:detail`)} onClick={() => onOpenPoolDetail(selectedPool)}>进入账号分组</Button>}
        {accountTable.searchInput}
      </Space>
      <Table<Account>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={accountTable.filteredRows}
        pagination={accountTable.pagination}
        scroll={{ x: 1130 }}
        locale={{ emptyText: '暂无 TG 账号。配置开发者应用后，可以通过手机号新增账号并启动真实 TG 登录。' }}
      />
    </Card>
  );
}
