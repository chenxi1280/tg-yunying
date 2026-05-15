import { Button, Space, Table, Tabs, Tag } from 'antd';
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
      defaultActiveKey="developer-apps"
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
          label: '提示词与素材',
          children: (
            <AISettingsView
              section="resources"
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
          label: '账号与权限',
          children: (
            <Table
              rowKey="id"
              size="small"
              dataSource={adminUsers}
              pagination={false}
              title={() => <Space><Button type="primary" onClick={onCreateAdminUser}>新增后台账号</Button></Space>}
              columns={[
                { title: '名称', dataIndex: 'name' },
                { title: '邮箱', dataIndex: 'email' },
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
      ]}
    />
  );
}
