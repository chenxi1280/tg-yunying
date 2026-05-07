/** App-level view-to-path and path-to-view routing maps. */

export const VIEW_ROUTES: Record<string, string> = {
  overview: '/dashboard',
  systemConfig: '/system-config',
  accounts: '/accounts',
  groupManagement: '/group-management',
  taskManagement: '/campaigns',
  usageReports: '/usage-reports',
  audits: '/audit',
};

export const ROUTE_VIEWS: Record<string, string> = Object.fromEntries(
  Object.entries(VIEW_ROUTES).map(([view, route]) => [route, view]),
);

Object.assign(ROUTE_VIEWS, {
  '/developer-apps': 'systemConfig',
  '/ai-config': 'systemConfig',
  '/activation-codes': 'systemConfig',
  '/account-pools': 'accounts',
  '/groups': 'groupManagement',
  '/archives': 'groupManagement',
});

export function viewFromPath(pathname: string): string {
  return ROUTE_VIEWS[pathname] ?? 'overview';
}
