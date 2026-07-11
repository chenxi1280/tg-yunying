import type { BadgeTone, TaskCenterStats, TaskRuntimeSummary } from '../types';

type RuntimeStageTask = Readonly<{
  status: string;
  next_run_at: string | null;
  last_error: string;
  stats: TaskCenterStats;
  runtime_stage?: Record<string, any>;
}>;

export type RuntimeStage = {
  primary_status?: string;
  primary_status_label?: string;
  stage_code?: string;
  stage_label?: string;
  severity?: BadgeTone;
  reason?: string;
  secondary_reasons?: string[];
  next_run_at?: string | null;
  last_error?: string;
};

const AI_MARKERS = ['AI 生成不可用', '没有健康 AI', 'read operation timed out', 'The read operation timed out'];
const CONTEXT_MARKERS = ['暂无新的真人上下文', '持续监听中', '等待新消息', '等待群内新消息', '上下文不足'];
const COOLDOWN_MARKERS = ['冷却', '慢速模式', 'FloodWait', '等待下一轮'];

export function runtimeStage(task: RuntimeStageTask, summary?: TaskRuntimeSummary | null, membershipPhase?: Record<string, any>): RuntimeStage {
  const provided = task.runtime_stage as RuntimeStage | undefined;
  if (provided?.stage_code) return provided;
  if (task.status === 'paused') return pausedStage(task);
  if (task.status === 'pending' || task.status === 'draft') {
    return fallbackStage(task, 'startup_checking', '启动校验中', 'warning', '任务尚未进入可执行调度');
  }
  const membership = membershipStage(task, membershipPhase);
  if (membership && hasMarker(task, AI_MARKERS)) {
    return withSecondaryReason(membership, task.last_error || 'AI 生成不可用，等待恢复后继续执行');
  }
  if (membership) return membership;
  if (task.status === 'running' && hasMarker(task, AI_MARKERS)) return fallbackStage(task, 'waiting_ai', '等待 AI', 'warning', task.last_error || 'AI 生成不可用，等待恢复后继续执行');
  if (task.status === 'running' && hasMarker(task, CONTEXT_MARKERS)) return fallbackStage(task, 'waiting_context', '等待上下文', 'warning', task.last_error || '等待群内新消息');
  if (task.status === 'running' && hasMarker(task, COOLDOWN_MARKERS)) return fallbackStage(task, 'waiting_cooldown', '等待冷却 / 下一轮', 'warning', task.last_error || '等待冷却或下一轮');
  if (summary?.pending_count || task.next_run_at) return fallbackStage(task, 'waiting_next_run', '等待下一轮', 'warning', task.last_error || '等待下一轮计划时间');
  return fallbackStage(task, task.status || 'draft', statusLabel(task.status), task.status === 'failed' ? 'danger' : 'warning', task.last_error || '任务可继续规划和执行');
}

export function runtimeStageLabel(task: RuntimeStageTask, summary?: TaskRuntimeSummary | null, membershipPhase?: Record<string, any>): string {
  const stage = runtimeStage(task, summary, membershipPhase);
  return stage.stage_label || statusLabel(task.status);
}

export function statusLabel(value?: string | null): string {
  if (['running', 'executing'].includes(value ?? '')) return '运行中';
  if (value === 'pending') return '启动中';
  if (value === 'code_waiting') return '等待验证码';
  if (value === 'two_fa_waiting') return '等待 2FA';
  if (value === 'paused') return '已暂停';
  if (value === 'draft') return '草稿';
  if (value === 'target_reached') return '已达标';
  if (value === 'wrapping_up') return '收尾中';
  if (value === 'stopped') return '人工停止';
  if (value === 'deleted') return '已删除';
  if (['completed', 'success', 'skipped', 'approved'].includes(value ?? '')) return '已完成';
  if (['failed', 'rejected', 'expired'].includes(value ?? '')) return '失败';
  return value || '未运行';
}

function pausedStage(task: RuntimeStageTask): RuntimeStage {
  const reason = task.last_error ? `任务已暂停，不会继续规划或执行新动作；最近错误：${task.last_error}` : '任务已暂停，不会继续规划或执行新动作';
  return fallbackStage(task, 'paused', '已暂停', 'danger', reason);
}

function fallbackStage(task: RuntimeStageTask, code: string, label: string, severity: BadgeTone, reason: string): RuntimeStage {
  return {
    primary_status: task.status,
    primary_status_label: statusLabel(task.status),
    stage_code: code,
    stage_label: label,
    severity,
    reason,
    next_run_at: task.next_run_at,
    last_error: task.last_error,
  };
}

function withSecondaryReason(stage: RuntimeStage, reason: string): RuntimeStage {
  if (!reason) return stage;
  return { ...stage, reason: `${stage.reason || ''}；同时存在：${reason}`, secondary_reasons: [reason] };
}

function hasMarker(task: RuntimeStageTask, markers: string[]): boolean {
  const text = `${task.last_error || ''} ${task.stats?.ai_unavailable_reason || ''} ${task.stats?.context_mode || ''}`;
  return markers.some((marker) => text.toLowerCase().includes(marker.toLowerCase()));
}

function membershipStage(task: RuntimeStageTask, phase?: Record<string, any>): RuntimeStage | null {
  const source = phase || task.stats?.membership_summary || {};
  const status = String(phase?.status || task.stats?.membership_stage || source.status || '');
  const pending = Number(phase?.pending_account_count ?? task.stats?.membership_need_join_count ?? source.pending_account_count ?? 0);
  const running = Number(phase?.running_account_count ?? source.running_account_count ?? 0);
  if (!['pending', 'running', 'partial_success', 'membership_pending', 'membership_running', 'membership_partial'].includes(status) || (!pending && !running)) return null;
  return fallbackStage(task, 'membership_preparing', '准入补齐中', 'warning', `目标准入补齐中：待准备 ${pending}，执行中 ${running}`);
}
