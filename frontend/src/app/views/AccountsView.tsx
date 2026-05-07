import React from 'react';
import type { Account, AccountPool } from '../types';
import type { RuntimeConfig } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent, healthTone } from '../utils';

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
  return (
    <section className="panel">
      <div className="section-title">
        <div>
          <h2>TG 账号池</h2>
          <span>按客户账号池分组管理，登录、资料、克隆和验证辅助都在账号详情中处理</span>
        </div>
        <div className="row-actions">
          <button onClick={onCreatePoolClick}>新增账号池</button>
          <button disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(false)}>新增账号</button>
          <button className="primary" disabled={!runtime?.can_create_tg_account} onClick={() => onCreateAccount(true)}>新增登录账号</button>
        </div>
      </div>
      {!runtime?.can_create_tg_account && (
        <div className="sub-panel compact-panel">
          <strong>请先配置开发者应用</strong>
          <p className="muted-line">新增 TG 账号需要可用的 Telegram api_id/api_hash。配置完成并保持健康后，账号新增和登录入口会自动启用。</p>
          <button className="primary small" onClick={onConfigureDeveloperApps}>去配置开发者应用</button>
        </div>
      )}
      <div className="pool-filter-strip">
        <button className={selectedPoolId === '' ? 'active' : ''} onClick={() => setSelectedPoolId('')}>全部账号池</button>
        {accountPools.map((pool) => (
          <button key={pool.id} className={selectedPoolId === pool.id ? 'active' : ''} onClick={() => setSelectedPoolId(pool.id)}>
            {pool.name}<small>{pool.account_count}</small>
          </button>
        ))}
        {selectedPool && <button className="primary" onClick={() => onOpenPoolDetail(selectedPool)}>进入账号池</button>}
      </div>
      <div className="table">
        {!accounts.length && <p className="muted-line">暂无 TG 账号。配置开发者应用后，可以通过手机号新增账号并启动真实 TG 登录。</p>}
        {accounts.map((account) => (
          <div className={`table-row account-row ${statusAccent(account.status)}`} key={account.id}>
            <div className="account-identity">
              <div className="avatar-preview small-avatar">
                {account.avatar_preview_url ? <img src={avatarUrl(account.avatar_preview_url)} alt="" /> : <span>{account.display_name.slice(0, 1)}</span>}
              </div>
              <div>
                <strong>{account.display_name}</strong>
                <span>@{account.username ?? '未设置'} / {account.phone_masked}</span>
                <span>账号池：{account.pool_name}</span>
                <span>昵称：{[account.tg_first_name, account.tg_last_name].filter(Boolean).join(' ') || '未设置'}</span>
                <span className="inline-status">资料 <StatusBadge status={account.profile_sync_status} /></span>
              </div>
            </div>
            <StatusBadge status={account.status} />
            <div>
              <strong>底层连接：{account.developer_app_name ? '正常' : '未绑定'}</strong>
              <span>{account.developer_app_name ? '登录能力已分配' : '登录时自动准备'}</span>
              <span><StatusBadge status={account.developer_app_health_status ?? '未配置'} label={account.developer_app_health_status === '健康' ? '正常' : account.developer_app_health_status ?? '未配置'} /></span>
            </div>
            <div className={`meter ${healthTone(account.health_score)}`} title={`健康分 ${account.health_score}`}><span style={{ width: `${account.health_score}%` }} /></div>
            <div className="row-actions">
              {LOGIN_REQUIRED_STATUSES.has(account.status) ? (
                <button className="primary small" onClick={() => onVerifyAccount(account)}>完成登录</button>
              ) : account.status === '在线' ? (
                <>
                  <button className="primary small" onClick={() => onOpenAccountDetail(account)}>详情</button>
                  <button onClick={() => onRunLogin(account, 'qr')}>扫码</button>
                  <button onClick={() => onHealthCheck(account)}>检查</button>
                  <button onClick={() => onSyncGroups(account)}>同步群</button>
                </>
              ) : (
                <button className="primary small" onClick={() => onOpenAccountDetail(account)}>详情</button>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
