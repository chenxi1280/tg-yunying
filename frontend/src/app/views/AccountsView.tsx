import React from 'react';
import { Alert, Avatar, Button, Card, Progress, Segmented, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, Database, ShieldAlert } from 'lucide-react';
import { api } from '../../shared/api/client';
import type { Account, AccountAvailabilitySummary, AccountPool } from '../types';
import type { RuntimeConfig } from '../types';
import { StatusBadge, useAntdTableControls } from '../components/shared';
import { AccountSecurityBatchDrawer } from './AccountSecurityBatchDrawer';
import { formatBeijingDateTime } from '../time';

const LOGIN_REQUIRED_STATUSES = new Set(['待登录', '等待验证码', '等待扫码', '等待2FA', '需重新登录', '异常']);
const ACCOUNT_RESTRICTED_STATUSES = new Set(['受限', '疑似封禁', '已封禁', 'Session失效']);
const accountPhone = (account: Account) => account.phone_number || account.phone_masked;
const authorizationStatusLabel = (status: string) => status === 'active' ? '主授权可用' : status === 'missing' ? '主授权缺失' : status;
function accountHealthScore(account: Account, availabilityByAccountId: Map<number, AccountAvailabilitySummary>) {
  return availabilityByAccountId.get(account.id)?.health_score ?? account.health_score;
}

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
  canSecurityRead?: boolean;
  canSecurityBatch?: boolean;
  canProfileBatchUpdate?: boolean;
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
  canSecurityRead = true,
  canSecurityBatch = true,
  canProfileBatchUpdate = true,
  canMovePool = true,
  canDeleteAccount = true,
}: Props) {
  const [selectedAccountIds, setSelectedAccountIds] = React.useState<number[]>([]);
  const [securityDrawerMode, setSecurityDrawerMode] = React.useState<'cleanup_devices' | 'set_two_fa' | 'profile' | null>(null);
  const [refreshingSecurity, setRefreshingSecurity] = React.useState(false);
  const [availabilityLoading, setAvailabilityLoading] = React.useState(false);
  const [availabilityByAccountId, setAvailabilityByAccountId] = React.useState<Map<number, AccountAvailabilitySummary>>(new Map());
  const accountTable = useAntdTableControls<Account>({
    rows: accounts,
    placeholder: '搜索账号 / username / 手机号 / 分组 / 状态 / 代理',
    pageSize: 100,
    pageSizeOptions: [50, 100, 200],
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
        account.authorization_summary.primary_status,
        account.authorization_summary.risk_hint,
        account.authorization_summary.has_standby ? '已有备用授权' : '未配置备用授权 无缝切换风险',
        canSecurityRead ? account.developer_app_health_status : '',
        account.proxy_name,
        canSecurityRead ? account.proxy_local_address : '',
        canSecurityRead ? account.proxy_status : '',
        canSecurityRead ? account.proxy_alert_status : '',
        availabilityByAccountId.get(account.id)?.unavailable_reason,
        account.tg_first_name,
        account.tg_last_name,
        !account.avatar_object_key ? '无头像 资料待初始化 资料不完整' : '',
        !account.username ? '无 username 资料待初始化 资料不完整' : '',
        !account.tg_first_name ? '昵称为空 资料待初始化 资料不完整' : '',
        ACCOUNT_RESTRICTED_STATUSES.has(account.status) ? '账号级受限 受限 系统探测恢复' : '',
        LOGIN_REQUIRED_STATUSES.has(account.status) ? '待完成登录 等待 登录' : '',
        canSecurityRead && accountHealthScore(account, availabilityByAccountId) < 60 ? '健康分偏低 健康' : '',
        canSecurityRead && account.proxy_status && account.proxy_status !== 'healthy' && account.proxy_status !== '健康' ? '代理异常 代理' : '',
      ],
    ],
  });
  const restrictedAccounts = accounts.filter((account) => ACCOUNT_RESTRICTED_STATUSES.has(account.status));
  const loginRequiredAccounts = accounts.filter((account) => LOGIN_REQUIRED_STATUSES.has(account.status));
  const lowHealthAccounts = canSecurityRead ? accounts.filter((account) => accountHealthScore(account, availabilityByAccountId) < 60 && !ACCOUNT_RESTRICTED_STATUSES.has(account.status)) : [];
  const proxyBlockedAccounts = canSecurityRead ? accounts.filter((account) => account.proxy_status && account.proxy_status !== 'healthy' && account.proxy_status !== '健康') : [];
  const selectedAccounts = accounts.filter((account) => selectedAccountIds.includes(account.id));
  const incompleteProfiles = accounts.filter((account) => !account.avatar_object_key || !account.username || !account.tg_first_name);
  const unavailableBySummary = Array.from(availabilityByAccountId.values()).filter((item) => !item.send_available);

  React.useEffect(() => {
    void loadAvailability();
  }, []);

  async function loadAvailability() {
    setAvailabilityLoading(true);
    try {
      const rows = await api<AccountAvailabilitySummary[]>('/tg-accounts/availability/summary');
      setAvailabilityByAccountId(new Map(rows.map((item) => [item.account_id, item])));
    } finally {
      setAvailabilityLoading(false);
    }
  }

  async function rebuildAvailability() {
    setAvailabilityLoading(true);
    try {
      await api('/tg-accounts/availability/rebuild', { method: 'POST' });
      await loadAvailability();
      void message.success('账号可用性汇总已重算');
    } finally {
      setAvailabilityLoading(false);
    }
  }

  async function refreshSelectedSecurity() {
    if (!selectedAccountIds.length) {
      void message.warning('请先选择账号');
      return;
    }
    setRefreshingSecurity(true);
    try {
      await Promise.all(selectedAccountIds.map((id) => api(`/tg-accounts/${id}/security/refresh`, { method: 'POST' })));
      void message.success(`已刷新 ${selectedAccountIds.length} 个账号安全状态`);
    } finally {
      setRefreshingSecurity(false);
    }
  }

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
          <Space orientation="vertical" size={0}>
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
        <Space orientation="vertical" size={4}>
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
        <Space orientation="vertical" size={2}>
          <Typography.Text strong>{account.developer_app_name ? '正常' : '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">{account.developer_app_name ? '登录能力已分配' : '登录时自动准备'}</Typography.Text>
          {canSecurityRead && <StatusBadge status={account.developer_app_health_status ?? '未配置'} label={account.developer_app_health_status === '健康' ? '正常' : account.developer_app_health_status ?? '未配置'} />}
          <Space size={4} wrap>
            <StatusBadge
              status={account.authorization_summary.primary_status === 'active' ? '可用' : '不可用'}
              label={authorizationStatusLabel(account.authorization_summary.primary_status)}
            />
            <StatusBadge
              status={account.authorization_summary.has_standby ? '可用' : '待处理'}
              label={`备用 ${account.authorization_summary.standby_count}/${account.authorization_summary.target_standby_count}`}
            />
          </Space>
          {account.authorization_summary.risk_hint && (
            <Typography.Text type={account.authorization_summary.is_blocking ? 'danger' : 'warning'}>
              {account.authorization_summary.risk_hint}
            </Typography.Text>
          )}
          <Typography.Text type="secondary">{account.proxy_name ? `代理：${account.proxy_name}` : '代理：未绑定'}</Typography.Text>
          {canSecurityRead ? (
            <>
              <Typography.Text type="secondary">{account.proxy_local_address || '高频发送建议绑定本地代理'}</Typography.Text>
              {account.proxy_status && <StatusBadge status={account.proxy_status} label={account.proxy_alert_status ? `${account.proxy_status} / ${account.proxy_alert_status}` : account.proxy_status} />}
            </>
          ) : (
            <Typography.Text type="secondary">需账号安全权限</Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '健康分',
      key: 'health_score',
      width: 150,
      render: (_, account) => {
        const score = accountHealthScore(account, availabilityByAccountId);
        return canSecurityRead ? <Progress percent={score} size="small" status={score < 60 ? 'exception' : score < 80 ? 'normal' : 'success'} /> : <Typography.Text type="secondary">需账号安全权限</Typography.Text>;
      },
    },
    {
      title: '安全 / 资料',
      key: 'security_profile',
      width: 180,
      render: (_, account) => {
        const profileComplete = Boolean(account.avatar_object_key && account.username && account.tg_first_name);
        return (
          <Space orientation="vertical" size={4}>
            {canSecurityRead && <StatusBadge status={account.status === '在线' ? '待确认' : '不可用'} label={account.status === '在线' ? '安全待刷新' : '安全不可用'} />}
            <StatusBadge status={profileComplete ? '已完成' : '待处理'} label={profileComplete ? '资料完整' : '资料待初始化'} />
            <Typography.Text type="secondary">{account.username ? `@${account.username}` : '未设置 username'}</Typography.Text>
          </Space>
        );
      },
    },
    {
      title: '可用性汇总',
      key: 'availability',
      width: 230,
      render: (_, account) => {
        const availability = availabilityByAccountId.get(account.id);
        if (!availability) {
          return <Typography.Text type="secondary">等待汇总</Typography.Text>;
        }
        return (
          <Space orientation="vertical" size={4}>
            <Space size={4} wrap>
              <StatusBadge status={availability.send_available ? '可用' : '不可用'} label="发" />
              <StatusBadge status={availability.listen_available ? '可用' : '不可用'} label="听" />
              <StatusBadge status={availability.join_available ? '可用' : '不可用'} label="加" />
              <StatusBadge status={availability.comment_available ? '可用' : '不可用'} label="评" />
            </Space>
            <Typography.Text type="secondary">容量 {availability.remaining_capacity}</Typography.Text>
            <Typography.Text type={availability.unavailable_reason ? 'danger' : 'secondary'}>{availability.unavailable_reason || '可用'}</Typography.Text>
            <Typography.Text type="secondary">更新 {formatBeijingDateTime(availability.updated_at)}</Typography.Text>
          </Space>
        );
      },
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
          title="请先配置开发者应用"
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
        {canSyncAccount && <Button loading={availabilityLoading} onClick={rebuildAvailability}>重算可用性</Button>}
      </Space>
      <Space className="pool-filter-strip" wrap>
        <Typography.Text type="secondary">已选择 {selectedAccounts.length} 个账号</Typography.Text>
        {canProfileBatchUpdate && <Button icon={<Activity size={16} />} onClick={() => setSecurityDrawerMode('profile')}>资料初始化</Button>}
        {canSecurityBatch && <Button icon={<ShieldAlert size={16} />} onClick={() => setSecurityDrawerMode('set_two_fa')}>设置二步密码</Button>}
        {canSecurityBatch && <Button icon={<Database size={16} />} onClick={() => setSecurityDrawerMode('cleanup_devices')}>清理登录设备</Button>}
        {canSecurityRead && <Button disabled={!selectedAccountIds.length} loading={refreshingSecurity} onClick={refreshSelectedSecurity}>刷新安全状态</Button>}
        <Button disabled={!selectedAccountIds.length} onClick={() => setSelectedAccountIds([])}>清空选择</Button>
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
        <Card className="summary-card" size="small">
          <span>汇总不可用</span>
          <strong>{unavailableBySummary.length}</strong>
          <p>来自账号可用性读模型；汇总可能有延迟，必要时重算。</p>
          <Button size="small" disabled={!unavailableBySummary.length} onClick={() => accountTable.setQuery('session_missing')}>查看汇总</Button>
        </Card>
        <Card className="summary-card" size="small">
          <span>资料待初始化</span>
          <strong>{incompleteProfiles.length}</strong>
          <p>头像、昵称或 username 缺失时，可批量 AI 随机生成并预览后执行。</p>
          <Button size="small" disabled={!incompleteProfiles.length} onClick={() => accountTable.setQuery('资料待初始化')}>查看资料</Button>
        </Card>
      </div>
      <Table<Account>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={accountTable.filteredRows}
        rowSelection={{
          selectedRowKeys: selectedAccountIds,
          onChange: (keys) => setSelectedAccountIds(keys.map(Number)),
        }}
        pagination={accountTable.pagination}
        scroll={{ x: 1510 }}
        locale={{ emptyText: '暂无 TG 账号。配置开发者应用后，可以通过手机号新增账号并启动真实 TG 登录。' }}
      />
      <AccountSecurityBatchDrawer
        open={securityDrawerMode !== null}
        mode={securityDrawerMode ?? 'profile'}
        accounts={accounts}
        selectedAccountIds={selectedAccountIds}
        onClose={() => setSecurityDrawerMode(null)}
      />
    </Card>
  );
}
