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

const ACCOUNT_SNAPSHOT_PAGE_SIZE = 200;
const FIRST_ACCOUNT_PAGE = 1;

type ContentResourceSnapshot = {
  materials: Material[];
  materialCacheConfig: MaterialCacheConfig | null;
  materialCacheHealth: MaterialCacheHealth | null;
  materialImports: MaterialImportResult[];
  contentKeywordRules: ContentKeywordRule[];
};

type SnapshotPatch = Partial<Omit<AppSnapshot, 'me' | 'runtime'>>;
type LoaderContext = {
  me: CurrentUser;
  selectedPoolId: number | '';
  taskStatusFilter: string;
  auditFilters: AuditFilters;
};

function accountListPath(selectedPoolId: number | '', page: number): string {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(ACCOUNT_SNAPSHOT_PAGE_SIZE),
  });
  if (selectedPoolId) params.set('pool_id', String(selectedPoolId));
  return `/tg-accounts?${params.toString()}`;
}

async function loadAccountList(selectedPoolId: number | ''): Promise<Account[]> {
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
  tenantAiSetting: TenantAiSetting | null;
  contentResources: ContentResourceSnapshot | null;
  groups: Group[];
  tasks: MessageTask[];
  archives: ArchiveItem[];
  audits: AuditLog[];
};

function emptySnapshot(me: CurrentUser, runtime: RuntimeConfig): AppSnapshot {
  return {
    me,
    runtime,
    overview: {} as Overview,
    accountPools: [],
    accounts: [],
    developerApps: [],
    tenants: [],
    adminUsers: [],
    usageLedgers: [],
    usageSummary: null,
    aiProviders: [],
    promptTemplates: [],
    tenantAiSetting: null,
    contentResources: null,
    groups: [],
    tasks: [],
    archives: [],
    audits: [],
  };
}

export async function loadContentResources(): Promise<ContentResourceSnapshot> {
  const [materials, materialCacheHealth, materialCacheConfig, materialImports, contentKeywordRules] = await Promise.all([
    api<Material[]>('/materials'),
    api<MaterialCacheHealth>('/materials/cache/health'),
    api<MaterialCacheConfig>('/materials/cache/config'),
    api<MaterialImportResult[]>('/material-imports'),
    api<ContentKeywordRule[]>('/content-keyword-rules'),
  ]);
  return {
    materials,
    materialCacheHealth,
    materialCacheConfig,
    materialImports,
    contentKeywordRules,
  };
}

async function loadAccountsPage(context: LoaderContext): Promise<SnapshotPatch> {
  const [accountPools, accounts] = await Promise.all([
    api<AccountPool[]>('/account-pools'),
    loadAccountList(context.selectedPoolId),
  ]);
  return { accountPools, accounts };
}

function messageTaskPath(taskStatusFilter: string): string {
  return `/message-send-tasks${taskStatusFilter ? `?status=${encodeURIComponent(taskStatusFilter)}` : ''}`;
}

async function loadMessageTasks(taskStatusFilter: string): Promise<MessageTask[]> {
  return api<MessageTask[]>(messageTaskPath(taskStatusFilter));
}

function archiveListPath(): string {
  return '/archives';
}

async function loadArchives(): Promise<ArchiveItem[]> {
  return api<ArchiveItem[]>(archiveListPath());
}

async function loadOverviewPage(): Promise<SnapshotPatch> {
  return { overview: await api<Overview>('/overview') };
}

async function loadSystemPage(context: LoaderContext): Promise<SnapshotPatch> {
  const [developerApps, tenants] = await Promise.all([
    hasPermission(context.me, 'system.view') ? api<DeveloperApp[]>('/developer-apps') : [],
    hasPermission(context.me, 'system.view') ? api<Tenant[]>('/tenants') : [],
  ]);
  return { developerApps, tenants };
}

async function loadMessagePage(context: LoaderContext): Promise<SnapshotPatch> {
  const [accounts, contentResources, tasks] = await Promise.all([
    loadAccountList(context.selectedPoolId),
    loadContentResources(),
    loadMessageTasks(context.taskStatusFilter),
  ]);
  return { accounts, contentResources, tasks };
}

async function loadGroupPage(): Promise<SnapshotPatch> {
  const [groups, archives] = await Promise.all([
    api<Group[]>('/groups'),
    loadArchives(),
  ]);
  return { groups, archives };
}

async function loadAuditPage(context: LoaderContext): Promise<SnapshotPatch> {
  return { audits: await api<AuditLog[]>(auditQuery(context.auditFilters)) };
}

const VIEW_RESOURCE_LOADERS: Record<string, (context: LoaderContext) => Promise<SnapshotPatch>> = {
  overview: () => loadOverviewPage(),
  accounts: loadAccountsPage,
  messageSending: loadMessagePage,
  materials: async () => ({ contentResources: await loadContentResources() }),
  groupManagement: () => loadGroupPage(),
  archives: async () => ({ archives: await loadArchives() }),
  taskManagement: async () => ({}),
  systemConfig: loadSystemPage,
  audits: loadAuditPage,
};

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
  const runtime = await api<RuntimeConfig>('/config/runtime');
  const loader = VIEW_RESOURCE_LOADERS[activeView];
  const patch = loader ? await loader({ me, selectedPoolId, taskStatusFilter, auditFilters }) : {};
  return { ...emptySnapshot(me, runtime), ...patch };
}
