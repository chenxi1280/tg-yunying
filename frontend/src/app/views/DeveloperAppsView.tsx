import React from 'react';
import { Button, Card, Descriptions, Empty, Space, Typography } from 'antd';
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
  showTenants?: boolean;
  isActionPending: (key: string) => boolean;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
}

export default function DeveloperAppsView({ developerApps, tenants, onCreateClick, onEdit, onCheck, onToggle, onEditTenant, showTenants = true, isActionPending, onOpenConfirm }: Props) {
  return (
    <>
      <Card
        className="panel"
        title="开发者应用池"
        extra={<Button type="primary" onClick={onCreateClick}>新增应用</Button>}
      >
        <Typography.Text type="secondary">平台级 api_id/api_hash 凭证池，按纯轮询绑定 TG 账号</Typography.Text>
        <div className="cards-grid developer-grid">
          {!developerApps.length && (
            <Empty description="还没有开发者应用">
              <Typography.Paragraph type="secondary">请新增真实 Telegram api_id/api_hash。配置完成前，账号新增和登录入口会保持禁用。</Typography.Paragraph>
              <Button type="primary" onClick={onCreateClick}>新增应用</Button>
            </Empty>
          )}
          {developerApps.map((app) => (
            <Card className={`developer-card ${statusAccent(app.is_active ? app.health_status : '禁用')}`} key={app.id} size="small" title={app.app_name} extra={<Badge tone="neutral">v{app.credentials_version}</Badge>}>
              <Space>
                <StatusBadge status={app.is_active ? app.health_status : '禁用'} />
                <Typography.Text type="secondary">API ID {app.api_id}</Typography.Text>
              </Space>
              <Descriptions size="small" column={2} items={[
                { key: 'assigned', label: '绑定账号', children: app.assigned_accounts },
                { key: 'limit', label: '账号上限', children: app.max_accounts || '不限' },
              ]} />
              {app.last_error && <Typography.Paragraph type="danger">{app.last_error}</Typography.Paragraph>}
              <Space wrap>
                <Button size="small" onClick={() => onEdit(app)}>编辑</Button>
                <Button size="small" loading={isActionPending(`developer-app:${app.id}:check`)} onClick={() => onCheck(app)}>检查</Button>
                <Button size="small" danger={app.is_active} loading={isActionPending(`developer-app:${app.id}:toggle`)} onClick={() => onOpenConfirm({
                  title: app.is_active ? '禁用开发者应用' : '启用开发者应用',
                  message: `确认${app.is_active ? '禁用' : '启用'}「${app.app_name}」？已绑定账号会继续保留绑定关系。`,
                  confirmLabel: app.is_active ? '确认禁用' : '确认启用',
                  tone: app.is_active ? 'danger' : 'normal',
                  onConfirm: () => onToggle(app),
                })}>{app.is_active ? '禁用' : '启用'}</Button>
              </Space>
            </Card>
          ))}
        </div>
      </Card>

      {showTenants && <Card className="panel" title="租户与配额" extra={<Typography.Text type="secondary">后台统一维护套餐名称、账号配额和任务配额</Typography.Text>}>
        <div className="cards-grid developer-grid">
          {tenants.map((tenant) => (
            <Card className="developer-card status-accent neutral" key={tenant.id} size="small" title={tenant.name}>
              <Space>
                <Badge tone="neutral">租户 #{tenant.id}</Badge>
                <Badge tone="positive">{tenant.plan_name}</Badge>
              </Space>
              <Descriptions size="small" column={2} items={[
                { key: 'account_quota', label: '账号配额', children: tenant.account_quota },
                { key: 'task_quota', label: '任务配额', children: tenant.task_quota },
              ]} />
              <Button size="small" onClick={() => onEditTenant(tenant)}>编辑配额</Button>
            </Card>
          ))}
        </div>
      </Card>}
    </>
  );
}
