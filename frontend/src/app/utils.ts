export { statusTone, riskTone, healthTone, statusAccent, operationLabel, syncTypeLabel } from './components/shared';
import type { CurrentUser } from './types';

export const VIEW_PERMISSION: Record<string, string> = {
  overview: 'overview.view',
  accounts: 'accounts.view',
  targetManagement: 'targets.view',
  messageSending: 'message_sending.view',
  taskManagement: 'tasks.view',
  listenerCenter: 'listeners.view',
  ruleCenter: 'rules.view',
  riskControl: 'risk.view',
  archives: 'archives.view',
  usageReports: 'usage.view',
  systemConfig: 'system.view',
  audits: 'audits.view',
};

export function userPermissions(user: CurrentUser | null | undefined): string[] {
  return user?.permissions ?? user?.menu_permissions ?? [];
}

export function hasPermission(user: CurrentUser | null | undefined, permission: string): boolean {
  const permissions = userPermissions(user);
  return Boolean(user?.is_super_admin || permissions.includes('*') || permissions.includes(permission));
}

export function canView(user: CurrentUser | null | undefined, viewId: string): boolean {
  const permission = VIEW_PERMISSION[viewId];
  return permission ? hasPermission(user, permission) : true;
}
