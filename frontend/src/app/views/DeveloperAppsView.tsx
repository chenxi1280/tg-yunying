import React from 'react';
import type { DeveloperApp, Tenant } from '../types';
import { StatusBadge, Badge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  onCreateClick: () => void;
  onEdit: (app: DeveloperApp) => void;
  onCheck: (app: DeveloperApp) => void;
  onToggle: (app: DeveloperApp) => void;
  onEditTenant: (tenant: Tenant) => void;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
}

export default function DeveloperAppsView({ developerApps, tenants, onCreateClick, onEdit, onCheck, onToggle, onEditTenant, onOpenConfirm }: Props) {
  return (
    <>
      <section className="panel">
        <div className="section-title">
          <div>
            <h2>开发者应用池</h2>
            <span>平台级 api_id/api_hash 凭证池，按纯轮询绑定 TG 账号</span>
          </div>
          <button className="primary" onClick={onCreateClick}>新增应用</button>
        </div>
        <div className="cards-grid developer-grid">
          {!developerApps.length && (
            <article className="developer-card status-accent warning">
              <h3>还没有开发者应用</h3>
              <p>请新增真实 Telegram api_id/api_hash。配置完成前，账号新增和登录入口会保持禁用。</p>
              <button className="primary" onClick={onCreateClick}>新增应用</button>
            </article>
          )}
          {developerApps.map((app) => (
            <article className={`developer-card ${statusAccent(app.is_active ? app.health_status : '禁用')}`} key={app.id}>
              <div>
                <StatusBadge status={app.is_active ? app.health_status : '禁用'} />
                <Badge tone="neutral">v{app.credentials_version}</Badge>
              </div>
              <h3>{app.app_name}</h3>
              <p>API ID {app.api_id}</p>
              <dl>
                <div><dt>绑定账号</dt><dd>{app.assigned_accounts}</dd></div>
                <div><dt>账号上限</dt><dd>{app.max_accounts || '不限'}</dd></div>
              </dl>
              {app.last_error && <p className="danger-text">{app.last_error}</p>}
              <div className="row-actions">
                <button onClick={() => onEdit(app)}>编辑</button>
                <button onClick={() => onCheck(app)}>检查</button>
                <button onClick={() => onOpenConfirm({
                  title: app.is_active ? '禁用开发者应用' : '启用开发者应用',
                  message: `确认${app.is_active ? '禁用' : '启用'}「${app.app_name}」？已绑定账号会继续保留绑定关系。`,
                  confirmLabel: app.is_active ? '确认禁用' : '确认启用',
                  tone: app.is_active ? 'danger' : 'normal',
                  onConfirm: () => onToggle(app),
                })}>{app.is_active ? '禁用' : '启用'}</button>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="section-title">
          <div>
            <h2>租户与配额</h2>
            <span>后台统一维护套餐名称、账号配额和任务配额</span>
          </div>
        </div>
        <div className="cards-grid developer-grid">
          {tenants.map((tenant) => (
            <article className="developer-card status-accent neutral" key={tenant.id}>
              <div>
                <Badge tone="neutral">租户 #{tenant.id}</Badge>
                <Badge tone="positive">{tenant.plan_name}</Badge>
              </div>
              <h3>{tenant.name}</h3>
              <dl>
                <div><dt>账号配额</dt><dd>{tenant.account_quota}</dd></div>
                <div><dt>任务配额</dt><dd>{tenant.task_quota}</dd></div>
              </dl>
              <div className="row-actions">
                <button onClick={() => onEditTenant(tenant)}>编辑配额</button>
              </div>
            </article>
          ))}
        </div>
      </section>
    </>
  );
}
