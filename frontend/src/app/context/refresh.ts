import { api } from '../../shared/api/client';
import { hasPermission } from '../utils';
import type {
  Account,
  AccountPool,
  AdminUser,
  AiProvider,
  ArchiveItem,
  AuditFilters,
  AuditLog,
  ContentKeywordRule,
  CurrentUser,
  DeveloperApp,
  Group,
  Material,
  MaterialCacheConfig,
  MaterialCacheHealth,
  MaterialImportResult,
  MessageTask,
  Overview,
  PromptTemplate,
  RuntimeConfig,
  Tenant,
  TenantAiSetting,
  UsageLedger,
  UsageSummary,
} from '../types';

function auditQuery(auditFilters: AuditFilters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(auditFilters)) {
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return query ? `/audit-logs?${query}` : '/audit-logs';
}

function settledValue<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === 'fulfilled' ? result.value : fallback;
}

export type AppSnapshot = {
  me: CurrentUser;
  runtime: RuntimeConfig;
  overview: Overview;
  accountPools: AccountPool[];
  accounts: Account[];
  developerApps: DeveloperApp[];
  tenants: Tenant[];
  adminUsers: AdminUser[];
  usageLedgers: UsageLedger[];
  usageSummary: UsageSummary | null;
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting;
  materials: Material[];
  materialCacheConfig: MaterialCacheConfig | null;
  materialCacheHealth: MaterialCacheHealth | null;
  materialImports: MaterialImportResult[];
  contentKeywordRules: ContentKeywordRule[];
  groups: Group[];
  tasks: MessageTask[];
  archives: ArchiveItem[];
  audits: AuditLog[];
};

export async function loadAppSnapshot({
  selectedPoolId,
  taskStatusFilter,
  auditFilters,
}: {
  selectedPoolId: number | '';
  taskStatusFilter: string;
  auditFilters: AuditFilters;
}): Promise<AppSnapshot> {
  const me = await api<CurrentUser>('/auth/me');
  const accountQuery = selectedPoolId ? `/tg-accounts?pool_id=${selectedPoolId}` : '/tg-accounts';
  const results = await Promise.allSettled([
    api<RuntimeConfig>('/config/runtime'),
    api<Overview>('/overview'),
    api<AccountPool[]>('/account-pools'),
    api<Account[]>(accountQuery),
    api<Group[]>('/groups'),
    api<MessageTask[]>(`/message-send-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`),
    api<ArchiveItem[]>('/archives'),
    api<AuditLog[]>(auditQuery(auditFilters)),
    api<AiProvider[]>('/ai-providers'),
    api<PromptTemplate[]>('/prompt-templates'),
    api<TenantAiSetting>('/tenant-ai-settings'),
    api<Material[]>('/materials'),
    api<MaterialCacheHealth>('/materials/cache/health'),
    api<MaterialCacheConfig>('/materials/cache/config'),
    api<MaterialImportResult[]>('/material-imports'),
    api<ContentKeywordRule[]>('/content-keyword-rules'),
  ]);
  const developerApps = hasPermission(me, 'system.view') ? await api<DeveloperApp[]>('/developer-apps').catch(() => [] as DeveloperApp[]) : [];
  const tenants = hasPermission(me, 'system.view') ? await api<Tenant[]>('/tenants').catch(() => [] as Tenant[]) : [];
  const adminUsers = hasPermission(me, 'permissions.view') ? await api<AdminUser[]>('/admin/users').catch(() => [] as AdminUser[]) : [];
  if (results[3].status === 'rejected') throw results[3].reason;
  return {
    me,
    runtime: settledValue(results[0], {} as RuntimeConfig),
    overview: settledValue(results[1], {} as Overview),
    accountPools: settledValue(results[2], [] as AccountPool[]),
    accounts: results[3].value,
    groups: settledValue(results[4], [] as Group[]),
    tasks: settledValue(results[5], [] as MessageTask[]),
    archives: settledValue(results[6], [] as ArchiveItem[]),
    audits: settledValue(results[7], [] as AuditLog[]),
    aiProviders: settledValue(results[8], [] as AiProvider[]),
    promptTemplates: settledValue(results[9], [] as PromptTemplate[]),
    tenantAiSetting: settledValue(results[10], {} as TenantAiSetting),
    materials: settledValue(results[11], [] as Material[]),
    materialCacheHealth: settledValue(results[12], null as MaterialCacheHealth | null),
    materialCacheConfig: settledValue(results[13], null as MaterialCacheConfig | null),
    materialImports: settledValue(results[14], [] as MaterialImportResult[]),
    contentKeywordRules: settledValue(results[15], [] as ContentKeywordRule[]),
    developerApps,
    tenants,
    adminUsers,
    usageLedgers: [],
    usageSummary: null,
  };
}
