import { Alert, Button, Card, Descriptions, Space, Table, Tabs, Tag, Typography } from 'antd';
import type {
  AdminUser,
  AiProvider,
  ConfirmPayload,
  ContentKeywordRule,
  DeveloperApp,
  Material,
  MaterialCacheHealth,
  PromptTemplate,
  Tenant,
  TenantAiSetting,
  CurrentUser,
  RuntimeConfig,
} from '../types';
import AISettingsView from './AISettingsView';
import DeveloperAppsView from './DeveloperAppsView';
import { hasPermission } from '../utils';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  materials: Material[];
  materialCacheHealth: MaterialCacheHealth | null;
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
  onEditTenant: (tenant: Tenant) => void;
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
  onOpenConfirm: (payload: ConfirmPayload) => void;
  isActionPending: (key: string) => boolean;
}

export default function SystemConfigView({
  developerApps,
  tenants,
  aiProviders,
  promptTemplates,
  tenantAiSetting,
  materials,
  materialCacheHealth,
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
  onEditTenant,
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
  onOpenConfirm,
  isActionPending,
}: Props) {
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
              onEditTenant={onEditTenant}
              onOpenConfirm={onOpenConfirm}
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
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
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
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
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
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              contentKeywordRules={contentKeywordRules}
              currentUserRole={currentUserRole}
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
              isActionPending={isActionPending}
            />
          ),
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
          ),
        },
      ]}
    />
  );
}
