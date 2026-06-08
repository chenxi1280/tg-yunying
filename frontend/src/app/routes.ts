/** App-level view-to-path and path-to-view routing maps. */

export const VIEW_ROUTES: Record<string, string> = {
  overview: '/dashboard',
  accounts: '/accounts',
  targetManagement: '/targets',
  targetProfile: '/target-profile',
  messageSending: '/message-sending',
  materials: '/materials',
  taskManagement: '/task-center',
  listenerCenter: '/listeners',
  ruleCenter: '/rules',
  riskControl: '/risk-control',
  archives: '/archives',
  usageReports: '/usage-reports',
  systemConfig: '/system-config',
  audits: '/audit',
  adminManual: '/manual',
};

export const ROUTE_VIEWS: Record<string, string> = Object.fromEntries(
  Object.entries(VIEW_ROUTES).map(([view, route]) => [route, view]),
);

Object.assign(ROUTE_VIEWS, {
  '/developer-apps': 'systemConfig',
  '/ai-config': 'systemConfig',
  '/account-pools': 'accounts',
  '/groups': 'targetManagement',
  '/group-management': 'targetManagement',
  '/operation-targets': 'targetManagement',
  '/material-center': 'materials',
  '/tasks': 'taskManagement',
  '/group-archives': 'archives',
  '/rule-center': 'ruleCenter',
  '/listener-center': 'listenerCenter',
  '/risk-center': 'riskControl',
});

export function viewFromPath(pathname: string): string {
  return ROUTE_VIEWS[pathname] ?? 'overview';
}
