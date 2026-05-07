import React from 'react';
import { Button, Card, Descriptions, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type {
  ActivationCode,
  ActivationCodeCreateForm,
  ActivationCodeFilters,
  ActivationCodePage,
  AdminUser,
  AiProvider,
  ConfirmPayload,
  ContentKeywordRule,
  DeveloperApp,
  Material,
  PromptTemplate,
  SchedulingSetting,
  SubscriptionPlan,
  Tenant,
  TenantAiSetting,
  UsageLedger,
  UsageSummary,
} from '../types';
import { Badge, StatusBadge } from '../components/shared';
import ActivationCodesView from './ActivationCodesView';
import AISettingsView from './AISettingsView';
import DeveloperAppsView from './DeveloperAppsView';
import UsageReportsView from './UsageReportsView';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  subscriptionPlans: SubscriptionPlan[];
  adminUsers: AdminUser[];
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  schedulingSetting: SchedulingSetting | null;
  materials: Material[];
  contentKeywordRules: ContentKeywordRule[];
  activationCodes: ActivationCode[];
  activationCodePage: ActivationCodePage;
  activationCodeFilters: ActivationCodeFilters;
  setActivationCodeFilters: React.Dispatch<React.SetStateAction<ActivationCodeFilters>>;
  activationBatch: ActivationCodeCreateForm;
  setActivationBatch: React.Dispatch<React.SetStateAction<ActivationCodeCreateForm>>;
  usageLedgers: UsageLedger[];
  usageSummary: UsageSummary | null;
  currentUserRole: string | undefined;
  onCreateDeveloperApp: () => void;
  onEditDeveloperApp: (app: DeveloperApp) => void;
  onCheckDeveloperApp: (app: DeveloperApp) => void;
  onToggleDeveloperApp: (app: DeveloperApp) => void;
  onEditTenant: (tenant: Tenant) => void;
  onCreateSubscriptionPlan: () => void;
  onEditSubscriptionPlan: (plan: SubscriptionPlan) => void;
  onEditAdminUser: (user: AdminUser) => void;
  onCreateAiProvider: () => void;
  onEditAiProvider: (provider: AiProvider) => void;
  onToggleAiProvider: (provider: AiProvider) => void;
  onCheckAiProvider: (provider: AiProvider) => void;
  onEditTenantAi: () => void;
  onEditScheduling: () => void;
  onCreatePromptTemplate: () => void;
  onCreateMaterial: () => void;
  onCreateKeywordRule: () => void;
  onEditKeywordRule: (rule: ContentKeywordRule) => void;
  onLoadCodes: (filters?: ActivationCodeFilters, page?: number, pageSize?: number) => Promise<void>;
  onCreateCodes: () => Promise<void>;
  onDisableCode: (code: ActivationCode) => Promise<void>;
  onOpenConfirm: (payload: ConfirmPayload) => void;
}

const MENU_LABELS: Record<string, string> = {
  overview: '运营概览',
  accounts: '账号管理',
  taskManagement: '任务管理',
  groupManagement: '群聊管理',
  usageReports: '用户用量',
  audits: '审计安全',
};

function TenantPlansPanel({
  tenants,
  subscriptionPlans,
  onCreateSubscriptionPlan,
  onEditSubscriptionPlan,
  onEditTenant,
}: {
  tenants: Tenant[];
  subscriptionPlans: SubscriptionPlan[];
  onCreateSubscriptionPlan: () => void;
  onEditSubscriptionPlan: (plan: SubscriptionPlan) => void;
  onEditTenant: (tenant: Tenant) => void;
}) {
  const planColumns: ColumnsType<SubscriptionPlan> = [
    { title: '套餐', dataIndex: 'name', key: 'name', render: (_, plan) => <Space><Typography.Text strong>{plan.name}</Typography.Text><Tag>{plan.plan_type}</Tag></Space> },
    { title: '有效期', dataIndex: 'duration_days', key: 'duration_days', render: (days: number) => `${days} 天` },
    { title: '默认 Token', dataIndex: 'token_quota', key: 'token_quota', render: (tokens: number) => tokens.toLocaleString() },
    { title: '状态', dataIndex: 'is_active', key: 'is_active', render: (active: boolean) => <StatusBadge status={active ? '已启用' : '禁用'} /> },
    { title: '操作', key: 'actions', render: (_, plan) => <Button size="small" onClick={() => onEditSubscriptionPlan(plan)}>编辑</Button> },
  ];

  return (
    <section className="view-grid">
      <Card className="panel" title="套餐配置" extra={<Button type="primary" onClick={onCreateSubscriptionPlan}>新增套餐</Button>}>
        <Typography.Text type="secondary">卡密生成会复制套餐快照，用户兑换后按快照延长订阅并增加 Token 余额。</Typography.Text>
        <Table<SubscriptionPlan>
          className="tg-table"
          rowKey="id"
          columns={planColumns}
          dataSource={subscriptionPlans}
          pagination={false}
          scroll={{ x: 760 }}
          locale={{ emptyText: '暂无套餐。' }}
        />
      </Card>
      <Card className="panel" title="租户配额" extra={<Typography.Text type="secondary">账号与任务额度仍按租户隔离</Typography.Text>}>
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
      </Card>
    </section>
  );
}

function AdminUsersPanel({ users, onEditUser }: { users: AdminUser[]; onEditUser: (user: AdminUser) => void }) {
  const columns: ColumnsType<AdminUser> = [
    {
      title: '用户',
      key: 'user',
      fixed: 'left',
      width: 260,
      render: (_, user) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{user.name}</Typography.Text>
          <Typography.Text type="secondary">{user.email}</Typography.Text>
        </Space>
      ),
    },
    { title: '角色', dataIndex: 'role', key: 'role', width: 120 },
    { title: '订阅', dataIndex: 'subscription_status', key: 'subscription_status', width: 140, render: (status: string) => <StatusBadge status={status} /> },
    { title: '剩余 Token', dataIndex: 'token_balance', key: 'token_balance', width: 150, render: (value: number) => value.toLocaleString() },
    { title: '菜单权限', dataIndex: 'menu_permissions', key: 'menu_permissions', render: (items: string[]) => (items.includes('*') ? '全部菜单' : items.map((item) => MENU_LABELS[item] ?? item).join(' / ')) },
    { title: '登录', dataIndex: 'is_active', key: 'is_active', width: 100, render: (active: boolean) => <StatusBadge status={active ? '已启用' : '禁用'} /> },
    { title: '操作', key: 'actions', fixed: 'right', width: 100, render: (_, user) => <Button size="small" onClick={() => onEditUser(user)}>管理</Button> },
  ];
  return (
    <Card className="panel" title="用户管理" extra={<Typography.Text type="secondary">启停用户、菜单权限、Token 调整和密码重置</Typography.Text>}>
      <Table<AdminUser>
        className="tg-table"
        rowKey="id"
        columns={columns}
        dataSource={users}
        pagination={false}
        scroll={{ x: 1050 }}
        locale={{ emptyText: '暂无用户。普通用户可自行注册，管理员在这里启停与分配权限。' }}
      />
    </Card>
  );
}

function MenuPermissionsPanel({ users }: { users: AdminUser[] }) {
  const rows = Object.entries(MENU_LABELS).map(([key, label]) => ({
    key,
    label,
    users: users.filter((user) => user.menu_permissions.includes('*') || user.menu_permissions.includes(key)).length,
  }));
  return (
    <Card className="panel" title="菜单权限" extra={<Typography.Text type="secondary">本轮控制前端入口可见与可进入</Typography.Text>}>
      <Table
        className="tg-table"
        rowKey="key"
        columns={[
          { title: '菜单', dataIndex: 'label', key: 'label' },
          { title: '权限键', dataIndex: 'key', key: 'key' },
          { title: '已授权用户数', dataIndex: 'users', key: 'users' },
        ]}
        dataSource={rows}
        pagination={false}
      />
    </Card>
  );
}

export default function SystemConfigView(props: Props) {
  const {
    developerApps,
    tenants,
    subscriptionPlans,
    adminUsers,
    aiProviders,
    promptTemplates,
    tenantAiSetting,
    schedulingSetting,
    materials,
    contentKeywordRules,
    activationCodes,
    activationCodePage,
    activationCodeFilters,
    setActivationCodeFilters,
    activationBatch,
    setActivationBatch,
    usageLedgers,
    usageSummary,
    currentUserRole,
    onCreateDeveloperApp,
    onEditDeveloperApp,
    onCheckDeveloperApp,
    onToggleDeveloperApp,
    onEditTenant,
    onCreateSubscriptionPlan,
    onEditSubscriptionPlan,
    onEditAdminUser,
    onCreateAiProvider,
    onEditAiProvider,
    onToggleAiProvider,
    onCheckAiProvider,
    onEditTenantAi,
    onEditScheduling,
    onCreatePromptTemplate,
    onCreateMaterial,
    onCreateKeywordRule,
    onEditKeywordRule,
    onLoadCodes,
    onCreateCodes,
    onDisableCode,
    onOpenConfirm,
  } = props;

  return (
    <Tabs
      className="config-tabs"
      defaultActiveKey="developer-apps"
      items={[
        {
          key: 'developer-apps',
          label: '开发者应用',
          children: (
            <DeveloperAppsView
              developerApps={developerApps}
              tenants={tenants}
              showTenants={false}
              onCreateClick={onCreateDeveloperApp}
              onEdit={onEditDeveloperApp}
              onCheck={onCheckDeveloperApp}
              onToggle={onToggleDeveloperApp}
              onEditTenant={onEditTenant}
              onOpenConfirm={onOpenConfirm}
            />
          ),
        },
        {
          key: 'plans',
          label: '租户与套餐',
          children: (
            <TenantPlansPanel
              tenants={tenants}
              subscriptionPlans={subscriptionPlans}
              onCreateSubscriptionPlan={onCreateSubscriptionPlan}
              onEditSubscriptionPlan={onEditSubscriptionPlan}
              onEditTenant={onEditTenant}
            />
          ),
        },
        {
          key: 'ai',
          label: 'AI 配置',
          children: (
            <AISettingsView
              aiProviders={aiProviders}
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              schedulingSetting={schedulingSetting}
              materials={materials}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
              onCreateProvider={onCreateAiProvider}
              onEditProvider={onEditAiProvider}
              onToggleProvider={onToggleAiProvider}
              onCheckProvider={onCheckAiProvider}
              onEditTenantAi={onEditTenantAi}
              onEditScheduling={onEditScheduling}
              onCreatePromptTemplate={onCreatePromptTemplate}
              onCreateMaterial={onCreateMaterial}
              onCreateKeywordRule={onCreateKeywordRule}
              onEditKeywordRule={onEditKeywordRule}
            />
          ),
        },
        { key: 'users', label: '用户管理', children: <AdminUsersPanel users={adminUsers} onEditUser={onEditAdminUser} /> },
        { key: 'menus', label: '菜单权限', children: <MenuPermissionsPanel users={adminUsers} /> },
        {
          key: 'activation-codes',
          label: '卡密管理',
          children: (
            <ActivationCodesView
              activationCodes={activationCodes}
              subscriptionPlans={subscriptionPlans}
              activationCodePage={activationCodePage}
              activationCodeFilters={activationCodeFilters}
              setActivationCodeFilters={setActivationCodeFilters}
              activationBatch={activationBatch}
              setActivationBatch={setActivationBatch}
              onLoadCodes={onLoadCodes}
              onCreateCodes={onCreateCodes}
              onDisableCode={onDisableCode}
              onOpenConfirm={onOpenConfirm}
            />
          ),
        },
        { key: 'usage', label: '用户用量', children: <UsageReportsView usageLedgers={usageLedgers} usageSummary={usageSummary} /> },
      ]}
    />
  );
}
