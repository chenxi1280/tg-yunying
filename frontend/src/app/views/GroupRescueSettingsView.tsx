import React from 'react';
import { Alert, Button, Card, Checkbox, Select, Space, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type { Account, Tenant } from '../types';
import { Badge } from '../components/shared';

const RESCUE_FAILURE_THRESHOLD = 3;
const ACCOUNT_SEARCH_PAGE_SIZE = 20;

type GroupRescueSettingsPayload = {
  group_rescue_enabled: boolean;
  group_rescue_admin_account_id: number | null;
};

interface Props {
  tenants: Tenant[];
  initialAccounts: Account[];
  onSaveGroupRescueSettings: (tenantId: number, payload: GroupRescueSettingsPayload) => Promise<void>;
  canManageGroupRescue?: boolean;
  isActionPending: (key: string) => boolean;
}

function accountLabel(account: Account) {
  const username = account.username ? ` @${account.username}` : '';
  return `${account.display_name || `账号 #${account.id}`}${username} ${account.phone_masked}`;
}

function accountOptions(accounts: Account[]) {
  return accounts
    .filter((account) => account.status === '在线' && !account.deleted_at)
    .map((account) => ({ value: account.id, label: accountLabel(account) }));
}

function rescueAccountLabel(tenant: Tenant, accounts: Account[]) {
  if (!tenant.group_rescue_admin_account_id) return '未配置';
  const account = accounts.find((item) => item.id === tenant.group_rescue_admin_account_id);
  return account ? accountLabel(account) : `账号 #${tenant.group_rescue_admin_account_id}`;
}

function TenantGroupRescueCard({ tenant, initialAccounts, canManage, isActionPending, onSave }: {
  tenant: Tenant;
  initialAccounts: Account[];
  canManage: boolean;
  isActionPending: Props['isActionPending'];
  onSave: Props['onSaveGroupRescueSettings'];
}) {
  const [enabled, setEnabled] = React.useState(tenant.group_rescue_enabled);
  const [adminAccountId, setAdminAccountId] = React.useState<number | null>(tenant.group_rescue_admin_account_id);
  const [accounts, setAccounts] = React.useState<Account[]>(initialAccounts);
  const [searching, setSearching] = React.useState(false);
  const [searchError, setSearchError] = React.useState('');
  const searchRequestSeq = React.useRef(0);
  const saveDisabled = !canManage || (enabled && !adminAccountId);

  React.useEffect(() => {
    setEnabled(tenant.group_rescue_enabled);
    setAdminAccountId(tenant.group_rescue_admin_account_id);
  }, [tenant.id, tenant.group_rescue_enabled, tenant.group_rescue_admin_account_id]);

  async function searchOnlineAccounts(search: string) {
    const requestSeq = searchRequestSeq.current + 1;
    searchRequestSeq.current = requestSeq;
    const params = new URLSearchParams({
      page: '1',
      page_size: String(ACCOUNT_SEARCH_PAGE_SIZE),
      status: '在线',
    });
    if (search.trim()) params.set('search', search.trim());
    setSearching(true);
    setSearchError('');
    try {
      const nextAccounts = await api<Account[]>(`/tg-accounts?${params.toString()}`);
      if (searchRequestSeq.current !== requestSeq) return;
      setAccounts(nextAccounts);
    } catch (error) {
      if (searchRequestSeq.current !== requestSeq) return;
      setSearchError(error instanceof Error ? error.message : '搜索救援管理员账号失败');
    } finally {
      if (searchRequestSeq.current === requestSeq) setSearching(false);
    }
  }

  return (
    <Card className="developer-card status-accent neutral" size="small" title={tenant.name} extra={<Badge tone={enabled ? 'positive' : 'neutral'}>{enabled ? '已启用' : '关闭'}</Badge>}>
      <div className="policy-grid">
        <Checkbox disabled={!canManage} checked={enabled} onChange={(event) => setEnabled(event.target.checked)}>启用群聊救援</Checkbox>
        <label className="wide-field">救援管理员账号<Select<number> showSearch allowClear disabled={!canManage} loading={searching} value={adminAccountId ?? undefined} onSearch={searchOnlineAccounts} onDropdownVisibleChange={(open) => { if (open) void searchOnlineAccounts(''); }} onChange={(value) => setAdminAccountId(value ?? null)} filterOption={false} options={accountOptions(accounts)} placeholder="搜索在线 TG 账号" /></label>
      </div>
      {searchError && <Alert type="error" showIcon message={searchError} />}
      <Typography.Paragraph type="secondary">当前救援账号：{rescueAccountLabel(tenant, accounts)}</Typography.Paragraph>
      <Typography.Paragraph type="secondary">连续失败阈值固定为 {RESCUE_FAILURE_THRESHOLD} 次；该账号只做救援处置，直接邀请异常账号入群，不参与发送、点赞、评论等普通任务。</Typography.Paragraph>
      <Button size="small" type="primary" disabled={saveDisabled} loading={isActionPending(`tenant:${tenant.id}:group-rescue:save`)} onClick={() => onSave(tenant.id, {
        group_rescue_enabled: enabled,
        group_rescue_admin_account_id: adminAccountId,
      })}>保存群聊救援配置</Button>
    </Card>
  );
}

export default function GroupRescueSettingsView({ tenants, initialAccounts, onSaveGroupRescueSettings, canManageGroupRescue = false, isActionPending }: Props) {
  return (
    <Card className="panel" title="群聊救援配置" extra={<Typography.Text type="secondary">运营空间全局生效</Typography.Text>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Paragraph type="secondary">专职处置账号必须先在 TG 账号管理里完成添加和登录；启用后只执行群聊救援邀请，不参与正式运营动作。</Typography.Paragraph>
        <div className="cards-grid developer-grid">
          {tenants.map((tenant) => (
            <TenantGroupRescueCard key={tenant.id} tenant={tenant} initialAccounts={initialAccounts} canManage={Boolean(canManageGroupRescue)} isActionPending={isActionPending} onSave={onSaveGroupRescueSettings} />
          ))}
        </div>
      </Space>
    </Card>
  );
}
