/** App-level view-to-path and path-to-view routing maps. */

export const VIEW_ROUTES: Record<string, string> = {
  overview: '/dashboard',
  developerApps: '/developer-apps',
  aiSettings: '/ai-config',
  activationCodes: '/activation-codes',
  usageReports: '/usage-reports',
  accounts: '/account-pools',
  groups: '/groups',
  taskManagement: '/campaigns',
  archives: '/archives',
  audits: '/audit',
};

export const ROUTE_VIEWS: Record<string, string> = Object.fromEntries(
  Object.entries(VIEW_ROUTES).map(([view, route]) => [route, view]),
);

export function viewFromPath(pathname: string): string {
  return ROUTE_VIEWS[pathname] ?? 'overview';
}
