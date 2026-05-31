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

const CONTENT_RESOURCE_VIEWS = new Set(['materials', 'messageSending', 'systemConfig']);
const ACCOUNT_SNAPSHOT_PAGE_SIZE = 200;
const FIRST_ACCOUNT_PAGE = 1;

export function viewNeedsContentResources(activeView: string): boolean {
  return CONTENT_RESOURCE_VIEWS.has(activeView);
}

type ContentResourceSnapshot = {
  materials: Material[];
  materialCacheConfig: MaterialCacheConfig | null;
  materialCacheHealth: MaterialCacheHealth | null;
  materialImports: MaterialImportResult[];
  contentKeywordRules: ContentKeywordRule[];
};

function accountListPath(selectedPoolId: number | '', page: number): string {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(ACCOUNT_SNAPSHOT_PAGE_SIZE),
  });
  if (selectedPoolId) params.set('pool_id', String(selectedPoolId));
  return `/tg-accounts?${params.toString()}`;
}

async function loadAccountsForPool(selectedPoolId: number | ''): Promise<Account[]> {
  const accounts: Account[] = [];
  for (let page = FIRST_ACCOUNT_PAGE; ; page += 1) {
    const rows = await api<Account[]>(accountListPath(selectedPoolId, page));
    accounts.push(...rows);
    if (rows.length < ACCOUNT_SNAPSHOT_PAGE_SIZE) return accounts;
  }
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
  contentResources: ContentResourceSnapshot | null;
  groups: Group[];
  tasks: MessageTask[];
  archives: ArchiveItem[];
  audits: AuditLog[];
};

export async function loadContentResources(): Promise<ContentResourceSnapshot> {
  const results = await Promise.allSettled([
    api<Material[]>('/materials'),
    api<MaterialCacheHealth>('/materials/cache/health'),
    api<MaterialCacheConfig>('/materials/cache/config'),
    api<MaterialImportResult[]>('/material-imports'),
    api<ContentKeywordRule[]>('/content-keyword-rules'),
  ]);
  return {
    materials: settledValue(results[0], [] as Material[]),
    materialCacheHealth: settledValue(results[1], null as MaterialCacheHealth | null),
    materialCacheConfig: settledValue(results[2], null as MaterialCacheConfig | null),
    materialImports: settledValue(results[3], [] as MaterialImportResult[]),
    contentKeywordRules: settledValue(results[4], [] as ContentKeywordRule[]),
  };
}

async function loadBaseSnapshotResults(selectedPoolId: number | '', taskStatusFilter: string, auditFilters: AuditFilters) {
  return Promise.allSettled([
    api<RuntimeConfig>('/config/runtime'),
    api<Overview>('/overview'),
    api<AccountPool[]>('/account-pools'),
    loadAccountsForPool(selectedPoolId),
    api<Group[]>('/groups'),
    api<MessageTask[]>(`/message-send-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`),
    api<ArchiveItem[]>('/archives'),
    api<AuditLog[]>(auditQuery(auditFilters)),
    api<AiProvider[]>('/ai-providers'),
    api<PromptTemplate[]>('/prompt-templates'),
    api<TenantAiSetting>('/tenant-ai-settings'),
  ]);
}

export async function loadAppSnapshot({
  activeView,
  selectedPoolId,
  taskStatusFilter,
  auditFilters,
}: {
  activeView: string;
  selectedPoolId: number | '';
  taskStatusFilter: string;
  auditFilters: AuditFilters;
}): Promise<AppSnapshot> {
  const me = await api<CurrentUser>('/auth/me');
  const results = await loadBaseSnapshotResults(selectedPoolId, taskStatusFilter, auditFilters);
  const contentResources = viewNeedsContentResources(activeView) ? await loadContentResources() : null;
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
    contentResources,
    developerApps,
    tenants,
    adminUsers,
    usageLedgers: [],
    usageSummary: null,
  };
}
