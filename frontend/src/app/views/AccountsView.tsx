import React from 'react';
import type { Account, AccountPool } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent, healthTone } from '../utils';

interface Props {
  accounts: Account[];
  accountPools: AccountPool[];
  selectedPoolId: number | '';
  setSelectedPoolId: (id: number | '') => void;
  selectedPool: AccountPool | undefined;
  avatarUrl: (value: string) => string;
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
          <button onClick={() => onCreateAccount(false)}>新增账号</button>
          <button className="primary" onClick={() => onCreateAccount(true)}>新增登录账号</button>
        </div>
      </div>
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
              <button className="primary small" onClick={() => onOpenAccountDetail(account)}>详情</button>
              <button onClick={() => onRunLogin(account, 'code')}>验证码</button>
              <button onClick={() => onRunLogin(account, 'qr')}>扫码</button>
              <button onClick={() => onVerifyAccount(account)}>验证</button>
              <button onClick={() => onHealthCheck(account)}>检查</button>
              <button onClick={() => onSyncGroups(account)}>同步群</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
