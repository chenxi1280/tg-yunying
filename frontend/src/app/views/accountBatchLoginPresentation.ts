export const ACTIVE_LOGIN_BATCH_STATUSES = new Set(['queued', 'running', 'cancelling']);
export const TERMINAL_LOGIN_BATCH_STATUSES = new Set([
  'completed', 'completed_with_manual', 'completed_with_failures',
  'completed_with_unresolved', 'cancelled',
]);

export function isActiveLoginBatch(status: string) {
  return ACTIVE_LOGIN_BATCH_STATUSES.has(status);
}

export function loginStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: '排队中', running: '执行中', waiting: '等待中', reconciling: '确认中',
    unresolved: '需持续对账', cancelling: '取消中', completed: '已完成',
    completed_with_manual: '完成但需人工', completed_with_failures: '完成但有失败',
    completed_with_unresolved: '完成但有未解', cancelled: '已取消', succeeded: '成功',
    succeeded_with_warning: '成功但有警告', failed: '失败', skipped: '已跳过', pending: '待执行',
    post_initialization_waiting: '已授权，初始化中', waiting_abc_approval: '等待 ABC 审批',
    waiting_profile: '资料初始化中', waiting_abc: 'ABC 初始化中', waiting_approval: '等待审批',
    approved: '已批准', manual_required: '需人工处理', not_requested: '未要求', required: '待处理',
  };
  return labels[status] || status;
}

export function loginStatusColor(status: string) {
  if (status === 'failed' || status === 'cancelled' || status === 'completed_with_failures') return 'red';
  if (status === 'manual_required' || status === 'unresolved' || status === 'reconciling' || status === 'succeeded_with_warning' || status === 'completed_with_manual' || status === 'completed_with_unresolved') return 'orange';
  if (status === 'succeeded' || status === 'completed') return 'green';
  return 'blue';
}
