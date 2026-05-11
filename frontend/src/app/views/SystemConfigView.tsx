import { Tabs } from 'antd';
import type {
  AiProvider,
  ConfirmPayload,
  ContentKeywordRule,
  DeveloperApp,
  Material,
  PromptTemplate,
  SchedulingSetting,
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
  schedulingSetting: SchedulingSetting | null;
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
  onEditScheduling: () => void;
  onCreatePromptTemplate: () => void;
  onCreateMaterial: () => void;
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
  schedulingSetting,
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
  onEditScheduling,
  onCreatePromptTemplate,
  onCreateMaterial,
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
          key: 'ai',
          label: 'AI 与规则基础配置',
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
              isActionPending={isActionPending}
            />
          ),
        },
      ]}
    />
  );
}
