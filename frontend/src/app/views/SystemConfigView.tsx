import { Tabs } from 'antd';
import type {
  AiProvider,
  ConfirmPayload,
  ContentKeywordRule,
  DeveloperApp,
  Material,
  PromptTemplate,
  Tenant,
  TenantAiSetting,
} from '../types';
import AISettingsView from './AISettingsView';
import DeveloperAppsView from './DeveloperAppsView';

interface Props {
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  materials: Material[];
  contentKeywordRules: ContentKeywordRule[];
  currentUserRole: string | undefined;
  onCreateDeveloperApp: () => void;
  onEditDeveloperApp: (app: DeveloperApp) => void;
  onCheckDeveloperApp: (app: DeveloperApp) => void;
  onToggleDeveloperApp: (app: DeveloperApp) => void;
  onEditTenant: (tenant: Tenant) => void;
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
  contentKeywordRules,
  currentUserRole,
  onCreateDeveloperApp,
  onEditDeveloperApp,
  onCheckDeveloperApp,
  onToggleDeveloperApp,
  onEditTenant,
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
      ]}
    />
  );
}
