import { useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Space, Table, Tabs, Tag, Typography } from 'antd';
import type {
  AdminUser,
  Account,
  AiProvider,
  ConfirmPayload,
  ContentKeywordRule,
  DeveloperApp,
  Material,
  MaterialCacheConfig,
  MaterialCacheHealth,
  PromptTemplate,
  Tenant,
  TenantAiSetting,
  TenantBotSettings,
  TenantFixedTwoFaSettings,
  CurrentUser,
  RuntimeConfig,
  PlannerPressure,
} from '../types';
import { api } from '../../shared/api/client';
import AISettingsView from './AISettingsView';
import DeveloperAppsView from './DeveloperAppsView';
import GroupRescueSettingsView from './GroupRescueSettingsView';
import ProxyAirportSubscriptionView from './ProxyAirportSubscriptionView';
import TelegramBotSettingsView from './TelegramBotSettingsView';
import TenantFixedTwoFaSettingsView from './TenantFixedTwoFaSettingsView';
import { hasPermission } from '../utils';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  aiProviders: AiProvider[];
  accounts: Account[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  tenantBotSettings: Record<number, TenantBotSettings>;
  tenantFixedTwoFaSettings: Record<number, TenantFixedTwoFaSettings>;
  materials: Material[];
  materialCacheHealth: MaterialCacheHealth | null;
  materialCacheConfig: MaterialCacheConfig | null;
  contentKeywordRules: ContentKeywordRule[];
  adminUsers: AdminUser[];
  currentUser: CurrentUser | null;
  currentUserRole: string | undefined;
  runtime: RuntimeConfig | null;
  activeTab?: string;
  onTabChange?: (key: string) => void;
  onCreateDeveloperApp: () => void;
  onEditDeveloperApp: (app: DeveloperApp) => void;
  onCheckDeveloperApp: (app: DeveloperApp) => void;
  onToggleDeveloperApp: (app: DeveloperApp) => void;
  onSaveDeveloperAppAssignments: (payload: {
    app_a_id: number;
    app_b_id: number;
    app_c_id: number;
    expected_assignment_version: number;
  }) => Promise<void>;
  onEditTenant: (tenant: Tenant) => void;
  onSaveGroupRescueSettings: (tenantId: number, payload: {
    group_rescue_enabled: boolean;
    group_rescue_admin_account_id: number | null;
  }) => Promise<void>;
  onSaveTenantBotSettings: (tenantId: number, payload: {
    admin_chat_id: string;
    telegram_bot_token?: string;
    ai_group_bot_enabled: boolean;
    notify_ai_failures_enabled: boolean;
  }) => Promise<void>;
  onTestTenantBotMessage: (tenantId: number) => Promise<void>;
  onRefreshTenantBotWebhook: (tenantId: number) => Promise<void>;
  onDeleteTenantBotWebhook: (tenantId: number) => Promise<void>;
  onSaveTenantFixedTwoFaSettings: (tenantId: number, payload: {
    password: string;
    reason: string;
  }) => Promise<boolean>;
  onCreateAdminUser: () => void;
  onEditAdminUser: (user: AdminUser) => void;
  onCreateAiProvider: () => void;
  onEditAiProvider: (provider: AiProvider) => void;
  onToggleAiProvider: (provider: AiProvider) => void;
  onCheckAiProvider: (provider: AiProvider) => void;
  onEditTenantAi: () => void;
  onCreatePromptTemplate: () => void;
  onCreateSlangTemplate: () => void;
  onEditPromptTemplate: (template: PromptTemplate) => void;
  onCreateMaterial: () => void;
  onEditMaterial: (material: Material) => void;
  onCreateKeywordRule: () => void;
  onEditKeywordRule: (rule: ContentKeywordRule) => void;
  onSavedMaterialCacheConfig: () => Promise<void>;
  onOpenConfirm: (payload: ConfirmPayload) => void;
  isActionPending: (key: string) => boolean;
}

export default function SystemConfigView({
  developerApps,
  tenants,
  aiProviders,
  accounts,
  promptTemplates,
  tenantAiSetting,
  tenantBotSettings,
  tenantFixedTwoFaSettings,
  materials,
  materialCacheHealth,
  materialCacheConfig,
  contentKeywordRules,
  adminUsers,
  currentUser,
  currentUserRole,
  runtime,
  activeTab,
  onTabChange,
  onCreateDeveloperApp,
  onEditDeveloperApp,
  onCheckDeveloperApp,
  onToggleDeveloperApp,
  onSaveDeveloperAppAssignments,
  onEditTenant,
  onSaveGroupRescueSettings,
  onSaveTenantBotSettings,
  onTestTenantBotMessage,
  onRefreshTenantBotWebhook,
  onDeleteTenantBotWebhook,
  onSaveTenantFixedTwoFaSettings,
  onCreateAdminUser,
  onEditAdminUser,
  onCreateAiProvider,
  onEditAiProvider,
  onToggleAiProvider,
  onCheckAiProvider,
  onEditTenantAi,
  onCreatePromptTemplate,
  onCreateSlangTemplate,
  onEditPromptTemplate,
  onCreateMaterial,
  onEditMaterial,
  onCreateKeywordRule,
  onEditKeywordRule,
  onSavedMaterialCacheConfig,
  onOpenConfirm,
  isActionPending,
}: Props) {
  const [plannerPressure, setPlannerPressure] = useState<PlannerPressure | null>(null);
  const [plannerPressureError, setPlannerPressureError] = useState('');

  useEffect(() => {
    if (activeTab !== 'runtime' || !hasPermission(currentUser, 'system.view')) return;
    let active = true;
    const load = async () => {
      try {
        const payload = await api<PlannerPressure>('/system/runtime/planner-pressure');
        if (active) {
          setPlannerPressure(payload);
          setPlannerPressureError('');
        }
      } catch (error) {
        if (active) setPlannerPressureError(error instanceof Error ? error.message : String(error));
      }
    };
    load();
    const timer = window.setInterval(load, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeTab, currentUser]);

  return (
    <Tabs
      className="config-tabs"
      activeKey={activeTab}
      defaultActiveKey="developer-apps"
      onChange={onTabChange}
      items={[
        {
          key: 'developer-apps',
          label: 'TG 开发者应用',
          children: (
            <DeveloperAppsView
              developerApps={developerApps}
              tenants={tenants}
              showTenants={false}
              canManageDeveloperApps={hasPermission(currentUser, 'developer_apps.manage')}
              onCreateClick={onCreateDeveloperApp}
              onEdit={onEditDeveloperApp}
              onCheck={onCheckDeveloperApp}
              onToggle={onToggleDeveloperApp}
              onSaveAssignments={onSaveDeveloperAppAssignments}
              onEditTenant={onEditTenant}
              onOpenConfirm={onOpenConfirm}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'telegram-bot',
          label: 'TG Bot 配置',
          children: (
            <TelegramBotSettingsView
              tenants={tenants}
              botSettings={tenantBotSettings}
              onSaveTenantBotSettings={onSaveTenantBotSettings}
              onTestTenantBotMessage={onTestTenantBotMessage}
              onRefreshTenantBotWebhook={onRefreshTenantBotWebhook}
              onDeleteTenantBotWebhook={onDeleteTenantBotWebhook}
              canManageBotSettings={hasPermission(currentUser, 'system.manage')}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'account-security',
          label: '账号安全配置',
          children: (
            <TenantFixedTwoFaSettingsView
              tenants={tenants}
              settings={tenantFixedTwoFaSettings}
              canManage={hasPermission(currentUser, 'system.manage')}
              onSave={onSaveTenantFixedTwoFaSettings}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'group-rescue',
          label: '群聊救援配置',
          children: (
            <GroupRescueSettingsView
              tenants={tenants}
              initialAccounts={accounts}
              onSaveGroupRescueSettings={onSaveGroupRescueSettings}
              canManageGroupRescue={hasPermission(currentUser, 'system.manage')}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'ai-providers',
          label: 'AI 供应商',
          children: (
            <AISettingsView
              section="providers"
              aiProviders={aiProviders}
              accounts={accounts}
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              materialCacheConfig={materialCacheConfig}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
              canManageAi={hasPermission(currentUser, 'ai.manage')}
              canManagePrompts={hasPermission(currentUser, 'prompt_templates.manage')}
              canManageSystem={hasPermission(currentUser, 'system.manage')}
              onCreateProvider={onCreateAiProvider}
              onEditProvider={onEditAiProvider}
              onToggleProvider={onToggleAiProvider}
              onCheckProvider={onCheckAiProvider}
              onEditTenantAi={onEditTenantAi}
              onCreatePromptTemplate={onCreatePromptTemplate}
              onCreateSlangTemplate={onCreateSlangTemplate}
              onEditPromptTemplate={onEditPromptTemplate}
              onCreateMaterial={onCreateMaterial}
              onEditMaterial={onEditMaterial}
              onCreateKeywordRule={onCreateKeywordRule}
              onEditKeywordRule={onEditKeywordRule}
              onSavedMaterialCacheConfig={onSavedMaterialCacheConfig}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'ai-slang',
          label: 'AI黑话配置',
          children: (
            <AISettingsView
              section="slang"
              aiProviders={aiProviders}
              accounts={accounts}
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              materialCacheConfig={materialCacheConfig}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
              canManageAi={hasPermission(currentUser, 'ai.manage')}
              canManagePrompts={hasPermission(currentUser, 'prompt_templates.manage')}
              canManageSystem={hasPermission(currentUser, 'system.manage')}
              onCreateProvider={onCreateAiProvider}
              onEditProvider={onEditAiProvider}
              onToggleProvider={onToggleAiProvider}
              onCheckProvider={onCheckAiProvider}
              onEditTenantAi={onEditTenantAi}
              onCreatePromptTemplate={onCreatePromptTemplate}
              onCreateSlangTemplate={onCreateSlangTemplate}
              onEditPromptTemplate={onEditPromptTemplate}
              onCreateMaterial={onCreateMaterial}
              onEditMaterial={onEditMaterial}
              onCreateKeywordRule={onCreateKeywordRule}
              onEditKeywordRule={onEditKeywordRule}
              onSavedMaterialCacheConfig={onSavedMaterialCacheConfig}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'resources',
          label: '提示词与素材运行配置',
          children: (
            <AISettingsView
              section="resources"
              showMaterialAssets={false}
              aiProviders={aiProviders}
              accounts={accounts}
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              materialCacheConfig={materialCacheConfig}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
              canManageAi={hasPermission(currentUser, 'ai.manage')}
              canManagePrompts={hasPermission(currentUser, 'prompt_templates.manage')}
              canManageSystem={hasPermission(currentUser, 'system.manage')}
              onCreateProvider={onCreateAiProvider}
              onEditProvider={onEditAiProvider}
              onToggleProvider={onToggleAiProvider}
              onCheckProvider={onCheckAiProvider}
              onEditTenantAi={onEditTenantAi}
              onCreatePromptTemplate={onCreatePromptTemplate}
              onCreateSlangTemplate={onCreateSlangTemplate}
              onEditPromptTemplate={onEditPromptTemplate}
              onCreateMaterial={onCreateMaterial}
              onEditMaterial={onEditMaterial}
              onCreateKeywordRule={onCreateKeywordRule}
              onEditKeywordRule={onEditKeywordRule}
              onSavedMaterialCacheConfig={onSavedMaterialCacheConfig}
              isActionPending={isActionPending}
            />
          ),
        },
        {
          key: 'clash',
          label: 'Clash 配置',
          children: <ProxyAirportSubscriptionView canManageSystem={hasPermission(currentUser, 'system.manage')} />,
        },
        {
          key: 'admin-users',
          label: '后台账号权限',
          children: (
            <Table
              rowKey="id"
              size="small"
              dataSource={adminUsers}
              pagination={false}
              title={() => <Space><Button type="primary" onClick={onCreateAdminUser}>新增后台账号</Button></Space>}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '账号类型', dataIndex: 'role' },
                { title: '角色模板', dataIndex: 'role_template' },
                { title: '状态', dataIndex: 'is_active', render: (value: boolean) => <Tag color={value ? 'green' : 'default'}>{value ? '允许登录' : '已停用'}</Tag> },
                { title: '权限数', dataIndex: 'permissions', render: (value: string[]) => value?.includes('*') ? '全部' : value?.length ?? 0 },
                { title: '版本', dataIndex: 'permission_version' },
                { title: '最近登录', dataIndex: 'last_login_at', render: (value: string | null) => value ? value.replace('T', ' ').slice(0, 16) : '未登录' },
                { title: '操作', render: (_, user: AdminUser) => <Button size="small" onClick={() => onEditAdminUser(user)}>编辑</Button> },
              ]}
            />
          ),
        },
        {
          key: 'runtime',
          label: '运行配置',
          children: (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Card className="panel" title="运行配置" extra={<Typography.Text type="secondary">只读底座状态</Typography.Text>}>
              {!runtime && <Alert type="warning" showIcon message="运行配置暂未加载" />}
              {runtime && (
                <Descriptions
                  bordered
                  size="small"
                  column={3}
                  items={[
                    { key: 'env', label: '环境', children: runtime.app_env },
                    { key: 'queue', label: '任务队列', children: runtime.queue_backend },
                    { key: 'gateway', label: 'TG 网关', children: runtime.tg_gateway_mode },
                    { key: 'telethon', label: 'Telethon', children: runtime.telethon_configured ? '已配置' : '待配置' },
                    { key: 'fallback', label: '同步调度回退', children: runtime.sync_dispatch_fallback ? '开启' : '关闭' },
                    { key: 'code_ttl', label: '验证码 TTL', children: `${runtime.code_ttl_seconds} 秒` },
                    { key: 'developer_apps', label: '开发者应用', children: `${runtime.developer_app_healthy_count}/${runtime.developer_app_count} 正常` },
                    { key: 'ai', label: 'AI 服务', children: `${runtime.healthy_ai_provider_count}/${runtime.ai_provider_count} 正常` },
                    { key: 'mock_ai', label: 'AI 回退', children: runtime.mock_ai_fallback_enabled ? '开启' : '关闭' },
                    { key: 'avatar_size', label: '头像上限', children: `${runtime.avatar_max_bytes} bytes` },
                    { key: 'avatar_types', label: '头像类型', span: 2, children: runtime.avatar_allowed_types.join('、') || '-' },
                  ]}
                />
              )}
            </Card>
            <Card className="panel" title="Planner 资源压力" extra={<Typography.Text type="secondary">每 30 秒刷新</Typography.Text>}>
              {plannerPressureError && <Alert type="error" showIcon message="Planner 压力读取失败" description={plannerPressureError} />}
              {!plannerPressureError && (!plannerPressure || plannerPressure.state === 'unavailable') && (
                <Alert type="warning" showIcon message="Planner 采样暂不可用" />
              )}
              {plannerPressure?.memory_kib && (
                <Descriptions
                  bordered
                  size="small"
                  column={3}
                  items={[
                    { key: 'state', label: '采样状态', children: <Tag color={plannerPressure.state === 'fresh' ? 'green' : 'orange'}>{plannerPressure.state}</Tag> },
                    { key: 'captured', label: '采样时间', children: plannerPressure.captured_at?.replace('T', ' ').slice(0, 19) || '-' },
                    { key: 'sha', label: '版本', children: plannerPressure.release_sha?.slice(0, 12) || '-' },
                    { key: 'pss', label: 'PSS', children: formatMiB(plannerPressure.memory_kib.pss) },
                    { key: 'private', label: 'Private Dirty', children: formatMiB(plannerPressure.memory_kib.private_dirty) },
                    { key: 'anonymous', label: 'Anonymous', children: formatMiB(plannerPressure.memory_kib.anonymous) },
                    { key: 'cpu', label: 'CPU', children: `${plannerPressure.cpu_percent ?? 0}%` },
                    { key: 'drain', label: 'Drain P50 / P95', children: `${plannerPressure.drain?.p50_ms ?? 0} / ${plannerPressure.drain?.p95_ms ?? 0} ms` },
                    { key: 'processed', label: '最近处理量', children: plannerPressure.drain?.latest_processed_count ?? 0 },
                    { key: 'cgroup', label: 'cgroup', children: `v${plannerPressure.cgroup?.version ?? 0}` },
                    { key: 'events', label: '内存事件', children: plannerPressure.cgroup?.event_count ?? 0 },
                    { key: 'telethon', label: 'Planner Telethon 客户端', children: plannerPressure.telethon_client_count ?? 0 },
                  ]}
                />
              )}
            </Card>
            </Space>
          ),
        },
      ]}
    />
  );
}

function formatMiB(kib: number): string {
  return `${(Number(kib || 0) / 1024).toFixed(1)} MiB`;
}
