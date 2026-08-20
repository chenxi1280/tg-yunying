import React from 'react';
import { Button, Card, Descriptions, Empty, Modal, Select, Space, Tag, Typography } from 'antd';
import type { DeveloperApp, Tenant } from '../types';
import { StatusBadge, Badge } from '../components/shared';
import { statusAccent } from '../utils';
import { formatBeijingDateTime } from '../time';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  onCreateClick: () => void;
  onEdit: (app: DeveloperApp) => void;
  onCheck: (app: DeveloperApp) => void;
  onToggle: (app: DeveloperApp) => void;
  onSaveAssignments: (payload: {
    app_a_id: number;
    app_b_id: number;
    app_c_id: number;
    expected_assignment_version: number;
  }) => Promise<void>;
  onEditTenant: (tenant: Tenant) => void;
  showTenants?: boolean;
  canManageDeveloperApps?: boolean;
  isActionPending: (key: string) => boolean;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
}

const ROLE_LABELS: Record<string, string> = {
  primary_sv: '硅谷主授权',
  standby_1_sv: '硅谷备用 1',
  standby_2_my: '马来西亚备用 2',
};

export default function DeveloperAppsView({ developerApps, tenants, onCreateClick, onEdit, onCheck, onToggle, onSaveAssignments, onEditTenant, showTenants = true, canManageDeveloperApps = false, isActionPending, onOpenConfirm }: Props) {
  const [detailApp, setDetailApp] = React.useState<DeveloperApp | null>(null);
  const [detailTenant, setDetailTenant] = React.useState<Tenant | null>(null);
  const [assignmentOpen, setAssignmentOpen] = React.useState(false);
  const [assignmentSaving, setAssignmentSaving] = React.useState(false);
  const [assignment, setAssignment] = React.useState({ app_a_id: 0, app_b_id: 0, app_c_id: 0 });

  function openAssignments() {
    const byRole = Object.fromEntries(developerApps.map((app) => [app.slot_purpose, app.id]));
    setAssignment({
      app_a_id: byRole.primary_sv ?? 0,
      app_b_id: byRole.standby_1_sv ?? 0,
      app_c_id: byRole.standby_2_my ?? 0,
    });
    setAssignmentOpen(true);
  }

  async function saveAssignments() {
    const expected = Math.max(0, ...developerApps.map((app) => app.assignment_version));
    setAssignmentSaving(true);
    try {
      await onSaveAssignments({ ...assignment, expected_assignment_version: expected });
      setAssignmentOpen(false);
    } finally {
      setAssignmentSaving(false);
    }
  }

  return (
    <>
      <Card
        className="panel"
        title="开发者应用池"
        extra={canManageDeveloperApps ? <Space><Button onClick={openAssignments}>配置三地角色</Button><Button type="primary" onClick={onCreateClick}>新增应用</Button></Space> : null}
      >
        <Typography.Text type="secondary">平台级 api_id/api_hash 凭证池；配置角色后，新账号主授权固定使用硅谷主授权 App。</Typography.Text>
        <div className="cards-grid developer-grid">
          {!developerApps.length && (
            <Empty description="还没有开发者应用">
              <Typography.Paragraph type="secondary">请新增真实 Telegram api_id/api_hash。配置完成前，账号新增和登录入口会保持禁用。</Typography.Paragraph>
              {canManageDeveloperApps && <Button type="primary" onClick={onCreateClick}>新增应用</Button>}
            </Empty>
          )}
          {developerApps.map((app) => (
            <Card className={`developer-card ${statusAccent(app.is_active ? app.health_status : '禁用')}`} key={app.id} size="small" title={app.app_name} extra={<Badge tone="neutral">v{app.credentials_version}</Badge>}>
              <Space>
                <StatusBadge status={app.is_active ? app.health_status : '禁用'} />
                <Typography.Text type="secondary">API ID {app.api_id}</Typography.Text>
                {app.slot_purpose && <Tag color={app.slot_purpose === 'standby_2_my' ? 'blue' : 'green'}>{ROLE_LABELS[app.slot_purpose]}</Tag>}
              </Space>
              <Typography.Paragraph type="secondary">已用账号 {app.used_distinct_accounts} / 上限 {app.capacity_unlimited ? '不限' : app.max_accounts}</Typography.Paragraph>
              <Space wrap>
                <Button size="small" onClick={() => setDetailApp(app)}>详情</Button>
                {canManageDeveloperApps && <Button size="small" onClick={() => onEdit(app)}>编辑</Button>}
                {canManageDeveloperApps && <Button size="small" loading={isActionPending(`developer-app:${app.id}:check`)} onClick={() => onCheck(app)}>检查</Button>}
                {canManageDeveloperApps && <Button size="small" danger={app.is_active} loading={isActionPending(`developer-app:${app.id}:toggle`)} onClick={() => onOpenConfirm({
                  title: app.is_active ? '禁用开发者应用' : '启用开发者应用',
                  message: `确认${app.is_active ? '禁用' : '启用'}「${app.app_name}」？已绑定账号会继续保留绑定关系。`,
                  confirmLabel: app.is_active ? '确认禁用' : '确认启用',
                  tone: app.is_active ? 'danger' : 'normal',
                  onConfirm: () => onToggle(app),
                })}>{app.is_active ? '禁用' : '启用'}</Button>}
              </Space>
            </Card>
          ))}
        </div>
      </Card>

      {showTenants && <Card className="panel" title="运营空间与配额" extra={<Typography.Text type="secondary">后台统一维护运行口径和任务配额</Typography.Text>}>
        <div className="cards-grid developer-grid">
          {tenants.map((tenant) => (
            <Card className="developer-card status-accent neutral" key={tenant.id} size="small" title={tenant.name}>
              <Space>
                <Badge tone="neutral">运营空间 #{tenant.id}</Badge>
                <Badge tone="positive">{tenant.plan_name}</Badge>
              </Space>
              <Typography.Paragraph type="secondary">账号 不限 / 任务 {tenant.task_quota}</Typography.Paragraph>
              <Space wrap>
                <Button size="small" onClick={() => setDetailTenant(tenant)}>详情</Button>
                <Button size="small" onClick={() => onEditTenant(tenant)}>编辑配置</Button>
              </Space>
            </Card>
          ))}
        </div>
      </Card>}

      <Modal className="tg-modal medium" title={detailApp?.app_name ?? '开发者应用详情'} open={Boolean(detailApp)} width={720} footer={null} destroyOnHidden centered onCancel={() => setDetailApp(null)}>
        {detailApp && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions size="small" column={2} items={[
              { key: 'status', label: '状态', children: <StatusBadge status={detailApp.is_active ? detailApp.health_status : '禁用'} /> },
              { key: 'api_id', label: 'API ID', children: detailApp.api_id },
              { key: 'assigned', label: '绑定账号', children: detailApp.assigned_accounts },
              { key: 'role', label: '固定角色', children: ROLE_LABELS[detailApp.slot_purpose] || '未分配' },
              { key: 'used', label: '占用账号', children: detailApp.used_distinct_accounts },
              { key: 'pending', label: '迁移占用', children: detailApp.pending_distinct_accounts },
              { key: 'limit', label: '账号上限', children: detailApp.max_accounts || '不限' },
              { key: 'version', label: '凭证版本', children: `v${detailApp.credentials_version}` },
              { key: 'last_check', label: '最近检查', children: detailApp.last_check_at ? formatBeijingDateTime(detailApp.last_check_at) : '未检查' },
            ]} />
            {detailApp.last_error && <Typography.Paragraph type="danger">{detailApp.last_error}</Typography.Paragraph>}
          </Space>
        )}
      </Modal>

      <Modal
        title="配置三套 Developer App 角色"
        open={assignmentOpen}
        okText="保存角色"
        cancelText="取消"
        confirmLoading={assignmentSaving}
        okButtonProps={{ disabled: new Set(Object.values(assignment)).size !== 3 || Object.values(assignment).some((value) => !value) }}
        onOk={() => void saveAssignments()}
        onCancel={() => setAssignmentOpen(false)}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {([
            ['app_a_id', '硅谷主授权'],
            ['app_b_id', '硅谷备用 1'],
            ['app_c_id', '马来西亚备用 2'],
          ] as const).map(([key, label]) => (
            <label key={key}>{label}<Select style={{ width: '100%' }} value={assignment[key] || undefined} options={developerApps.filter((app) => app.is_active && app.health_status === '健康').map((app) => ({ value: app.id, label: `${app.app_name} / API ID ${app.api_id}` }))} onChange={(value) => setAssignment((current) => ({ ...current, [key]: value }))} /></label>
          ))}
        </Space>
      </Modal>

      <Modal className="tg-modal medium" title={detailTenant?.name ?? '运营空间详情'} open={Boolean(detailTenant)} width={720} footer={null} destroyOnHidden centered onCancel={() => setDetailTenant(null)}>
        {detailTenant && (
          <Descriptions size="small" column={2} items={[
            { key: 'id', label: '运营空间 ID', children: detailTenant.id },
            { key: 'plan', label: '运行口径', children: detailTenant.plan_name },
            { key: 'account_quota', label: '账号上限', children: '不限' },
            { key: 'task_quota', label: '任务配额', children: detailTenant.task_quota },
            { key: 'bot', label: 'Bot 配置', children: <StatusBadge status={detailTenant.telegram_bot_configured ? '已配置' : '未配置'} /> },
            { key: 'notify', label: 'AI 失败通知', children: detailTenant.notify_ai_failures_enabled ? '启用' : '关闭' },
            { key: 'group_rescue_enabled', label: '群聊救援', children: detailTenant.group_rescue_enabled ? '启用' : '关闭' },
            { key: 'group_rescue_account', label: '救援账号', children: detailTenant.group_rescue_admin_account_id ? `#${detailTenant.group_rescue_admin_account_id}` : '未配置' },
          ]} />
        )}
      </Modal>
    </>
  );
}
