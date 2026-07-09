import type { TaskCenterTask } from '../types';

const UNKNOWN_TARGET_GROUP = '未关联群聊';
const UNKNOWN_CHANNEL = '未关联频道';

export type TaskCenterQuickGroup = {
  readonly id: string;
  readonly target_group_label: string;
  readonly associated_channel_label: string;
  readonly label: string;
  readonly task_count: number;
  readonly running_count: number;
  readonly failed_count: number;
  readonly task_ids: readonly string[];
};

type TaskGroupAccumulator = Omit<TaskCenterQuickGroup, 'label' | 'task_count' | 'running_count' | 'failed_count' | 'task_ids'> & { tasks: TaskCenterTask[] };

export function buildTaskQuickGroups(tasks: TaskCenterTask[]): TaskCenterQuickGroup[] {
  const groups: TaskGroupAccumulator[] = [];
  const byKey = new Map<string, TaskGroupAccumulator>();
  for (const task of tasks) {
    const key = `${targetGroupKey(task)}:${associatedChannelKey(task)}`;
    const existing = byKey.get(key);
    if (existing) {
      existing.tasks.push(task);
      continue;
    }
    const group = createAccumulator(key, task);
    byKey.set(key, group);
    groups.push(group);
  }
  return groups.map(toQuickGroup);
}

export function filterTasksByQuickGroup(tasks: TaskCenterTask[], selectedGroupId: string): TaskCenterTask[] {
  if (selectedGroupId === 'all') return tasks;
  const group = buildTaskQuickGroups(tasks).find((item) => item.id === selectedGroupId);
  const taskIds = new Set(group?.task_ids ?? []);
  return tasks.filter((task) => taskIds.has(task.id));
}

export function targetGroupLabel(task: TaskCenterTask): string {
  const config = task.type_config ?? {};
  if (task.type === 'group_ai_chat') {
    return firstText([task.target_summary, config.target_group_name, idLabel(config.target_operation_target_id, '运营目标'), idLabel(config.target_group_id, '群')]) || UNKNOWN_TARGET_GROUP;
  }
  if (task.type === 'group_relay') {
    return firstText([task.target_summary, config.target_group_name, multiIdLabel(config.target_operation_target_ids, '运营目标')]) || UNKNOWN_TARGET_GROUP;
  }
  if (task.type === 'search_join_group') {
    return firstText([task.target_summary, config.target_title, idLabel(config.target_operation_target_id, '运营目标'), idLabel(config.target_group_id, '群')]) || UNKNOWN_TARGET_GROUP;
  }
  if (task.type === 'search_rank_deboost') {
    return firstText([task.target_summary, multiIdLabel(config.target_group_ids, '运营目标')]) || UNKNOWN_TARGET_GROUP;
  }
  return firstText([config.linked_group_name, config.discussion_group_name, config.target_group_name, idLabel(config.linked_group_id, '群'), idLabel(config.discussion_group_id, '群')]) || UNKNOWN_TARGET_GROUP;
}

export function associatedChannelLabel(task: TaskCenterTask): string {
  const config = task.type_config ?? {};
  if (String(task.type).startsWith('channel_')) {
    return firstText([task.target_summary, config.target_channel_name, config.channel_title, idLabel(config.target_channel_id, '频道')]) || UNKNOWN_CHANNEL;
  }
  const channels = [
    ...textList(config.required_channels),
    ...textList(config.required_channel_refs),
    ...textList(config.linked_channels),
    ...textList(config.associated_channels),
    firstText([config.linked_channel_name, config.required_channel_name, config.target_channel_name]),
  ].filter(Boolean);
  return uniqueValues(channels).join('、') || UNKNOWN_CHANNEL;
}

function createAccumulator(key: string, task: TaskCenterTask): TaskGroupAccumulator {
  return {
    id: `task-group:${key}`,
    target_group_label: targetGroupLabel(task),
    associated_channel_label: associatedChannelLabel(task),
    tasks: [task],
  };
}

function toQuickGroup(group: TaskGroupAccumulator): TaskCenterQuickGroup {
  const taskCount = group.tasks.length;
  const runningCount = group.tasks.filter((task) => task.status === 'running').length;
  const failedCount = group.tasks.filter((task) => task.status === 'failed').length;
  return {
    id: group.id,
    target_group_label: group.target_group_label,
    associated_channel_label: group.associated_channel_label,
    label: `${group.target_group_label} / ${group.associated_channel_label} ${taskCount}`,
    task_count: taskCount,
    running_count: runningCount,
    failed_count: failedCount,
    task_ids: group.tasks.map((task) => task.id),
  };
}

function targetGroupKey(task: TaskCenterTask): string {
  const config = task.type_config ?? {};
  return keyPart([config.target_operation_target_id, config.target_group_id, config.linked_group_id, config.discussion_group_id, targetGroupLabel(task)]);
}

function associatedChannelKey(task: TaskCenterTask): string {
  const config = task.type_config ?? {};
  return keyPart([config.target_channel_id, associatedChannelLabel(task)]);
}

function keyPart(values: unknown[]): string {
  const value = firstText(values);
  return value.toLowerCase().replace(/\s+/g, '-');
}

function idLabel(value: unknown, prefix: string): string {
  const text = primitiveText(value);
  return text ? `${prefix} #${text}` : '';
}

function multiIdLabel(value: unknown, prefix: string): string {
  const items = textList(value);
  if (!items.length) return '';
  if (items.length === 1) return `${prefix} #${items[0]}`;
  return `${items.length} 个${prefix}`;
}

function firstText(values: unknown[]): string {
  return values.map(primitiveText).find(Boolean) ?? '';
}

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return primitiveText(value) ? [primitiveText(value)] : [];
  return value.map((item) => primitiveText(item)).filter(Boolean);
}

function primitiveText(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (value && typeof value === 'object') return firstTextObject(value as Record<string, unknown>);
  return '';
}

function firstTextObject(value: Record<string, unknown>): string {
  return firstText([value.title, value.name, value.username, value.label, value.id]);
}

function uniqueValues(values: string[]): string[] {
  return Array.from(new Set(values));
}
