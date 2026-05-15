import React from 'react';
import { Alert, Avatar, Button, Card, Progress, Segmented, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { Account, AccountPool } from '../types';
import type { RuntimeConfig } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';

const LOGIN_REQUIRED_STATUSES = new Set(['待登录', '等待验证码', '等待扫码', '等待2FA', '需重新登录', '异常']);
const ACCOUNT_RESTRICTED_STATUSES = new Set(['受限', '疑似封禁', '已封禁', 'Session失效']);
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
  canCreateAccount?: boolean;
  canLoginAccount?: boolean;
  canSyncAccount?: boolean;
  canViewCodes?: boolean;
  canMovePool?: boolean;
  canDeleteAccount?: boolean;
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
  canCreateAccount = true,
  canLoginAccount = true,
  canSyncAccount = true,
  canViewCodes = true,
  canMovePool = true,
  canDeleteAccount = true,
}: Props) {
  const accountTable = useAntdTableControls<Account>({
    rows: accounts,
    placeholder: '搜索账号 / username / 手机号 / 分组 / 状态 / 代理',
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
        account.proxy_name,
        account.proxy_local_address,
        account.proxy_status,
        account.proxy_alert_status,
        account.tg_first_name,
        account.tg_last_name,
        ACCOUNT_RESTRICTED_STATUSES.has(account.status) ? '账号级受限 受限 系统探测恢复' : '',
        LOGIN_REQUIRED_STATUSES.has(account.status) ? '待完成登录 等待 登录' : '',
        account.health_score < 60 ? '健康分偏低 健康' : '',
        account.proxy_status && account.proxy_status !== 'healthy' && account.proxy_status !== '健康' ? '代理异常 代理' : '',
      ],
    ],
  });
  const restrictedAccounts = accounts.filter((account) => ACCOUNT_RESTRICTED_STATUSES.has(account.status));
  const loginRequiredAccounts = accounts.filter((account) => LOGIN_REQUIRED_STATUSES.has(account.status));
  const lowHealthAccounts = accounts.filter((account) => account.health_score < 60 && !ACCOUNT_RESTRICTED_STATUSES.has(account.status));
  const proxyBlockedAccounts = accounts.filter((account) => account.proxy_status && account.proxy_status !== 'healthy' && account.proxy_status !== '健康');

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
          <Typography.Text type="secondary">{account.proxy_name ? `代理：${account.proxy_name}` : '代理：未绑定'}</Typography.Text>
          <Typography.Text type="secondary">{account.proxy_local_address || '高频发送建议绑定本地代理'}</Typography.Text>
          {account.proxy_status && <StatusBadge status={account.proxy_status} label={account.proxy_alert_status ? `${account.proxy_status} / ${account.proxy_alert_status}` : account.proxy_status} />}
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
              {canLoginAccount && <Button type="primary" size="small" onClick={() => onVerifyAccount(account)}>{account.status === '待登录' ? '完成登录' : '继续登录'}</Button>}
              {canDeleteAccount && <Button danger size="small" loading={isActionPending(`account:${account.id}:delete`)} onClick={() => onDeleteAccount(account)}>移除</Button>}
            </>
          ) : account.status === '在线' ? (
            <>
              <Button type="primary" size="small" loading={isActionPending(`account:${account.id}:detail`)} onClick={() => onOpenAccountDetail(account)}>详情</Button>
              {canViewCodes && <Button size="small" loading={isActionPending(`account:${account.id}:codes`)} onClick={() => onExtractCodes(account)}>提取验证码</Button>}
              {canMovePool && <Button size="small" loading={isActionPending(`account:${account.id}:move-pool`)} onClick={() => onMovePool(account)}>移动分组</Button>}
              {canSyncAccount && <Button size="small" loading={isActionPending(`account:${account.id}:health`)} onClick={() => onHealthCheck(account)}>检查</Button>}
              {canSyncAccount && <Button size="small" loading={isActionPending(`account:${account.id}:sync`)} onClick={() => onSyncGroups(account)}>同步</Button>}
            </>
          ) : (
            <>
              <Button type="primary" size="small" loading={isActionPending(`account:${account.id}:detail`)} onClick={() => onOpenAccountDetail(account)}>详情</Button>
              {canMovePool && <Button size="small" loading={isActionPending(`account:${account.id}:move-pool`)} onClick={() => onMovePool(account)}>移动分组</Button>}
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
          {canMovePool && <Button onClick={onCreatePoolClick}>新增账号分组</Button>}
          {canCreateAccount && <Button disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(false)}>新增账号</Button>}
        </Space>
      )}
    >
      <Typography.Text type="secondary">按账号分组管理；账号级受限由系统探测恢复，群管理机器拦截在账号详情内解除并重查。</Typography.Text>
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
      <div className="summary-grid">
        <Card className="summary-card" size="small">
          <span>账号级受限</span>
          <strong>{restrictedAccounts.length}</strong>
          <p>系统每小时探测恢复；仅在 session 失效或凭证不可用时重新登录。</p>
          <Button size="small" disabled={!restrictedAccounts.length} onClick={() => accountTable.setQuery('账号级受限')}>查看账号</Button>
        </Card>
        <Card className="summary-card" size="small">
          <span>待完成登录</span>
          <strong>{loginRequiredAccounts.length}</strong>
          <p>只展示完成登录入口，验证码、扫码、2FA 按登录流程继续。</p>
          <Button size="small" disabled={!loginRequiredAccounts.length} onClick={() => accountTable.setQuery('待完成登录')}>查看登录</Button>
        </Card>
        <Card className="summary-card" size="small">
          <span>健康分偏低</span>
          <strong>{lowHealthAccounts.length}</strong>
          <p>先做健康检查和同步，不直接要求重新登录。</p>
          <Button size="small" disabled={!lowHealthAccounts.length} onClick={() => accountTable.setQuery('健康分偏低')}>查看健康</Button>
        </Card>
        <Card className="summary-card" size="small">
          <span>代理异常</span>
          <strong>{proxyBlockedAccounts.length}</strong>
          <p>本地代理异常会影响高频发送，处理入口在风控中心。</p>
          <Button size="small" disabled={!proxyBlockedAccounts.length} onClick={() => accountTable.setQuery('代理异常')}>查看代理</Button>
        </Card>
      </div>
      <Table<Account>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={accountTable.filteredRows}
        pagination={accountTable.pagination}
        scroll={{ x: 1280 }}
        locale={{ emptyText: '暂无 TG 账号。配置开发者应用后，可以通过手机号新增账号并启动真实 TG 登录。' }}
      />
    </Card>
  );
}
