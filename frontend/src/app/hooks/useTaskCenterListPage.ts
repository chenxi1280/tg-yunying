import React from 'react';

import { api } from '../../shared/api/client';
import type { SchedulingSetting, TaskCenterAnyTaskType, TaskCenterListGroup, TaskCenterListItem, TaskCenterListPage, TaskCenterListSummary } from '../types';

const TASK_LIST_PAGE_SIZE = 20;
const TASK_LIST_POLL_INTERVAL_MS = 60000;

export type TaskCenterListQuery = Readonly<{
  page: number;
  pageSize: number;
  type: TaskCenterAnyTaskType | 'all';
  status: string;
  q: string;
  groupKey: string;
}>;

type TaskListRequest = Readonly<{
  sequence: number;
  queryKey: string;
  controller: AbortController;
}>;

const EMPTY_SUMMARY: TaskCenterListSummary = { total: 0, running: 0, failed: 0 };

function taskListParams(query: TaskCenterListQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set('page', String(query.page));
  params.set('page_size', String(query.pageSize));
  if (query.type !== 'all') params.set('type', query.type);
  if (query.status !== 'all') params.set('status', query.status);
  if (query.q) params.set('q', query.q);
  if (query.groupKey !== 'all') params.set('group_key', query.groupKey);
  return params;
}

function taskListQueryKey(query: TaskCenterListQuery): string {
  return JSON.stringify(query);
}

function requestError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function beginRequest(
  requestRef: React.MutableRefObject<{ sequence: number; queryKey: string }>,
  controllerRef: React.MutableRefObject<AbortController | null>,
  query: TaskCenterListQuery,
): TaskListRequest {
  controllerRef.current?.abort();
  const controller = new AbortController();
  const request = { sequence: requestRef.current.sequence + 1, queryKey: taskListQueryKey(query), controller };
  requestRef.current = request;
  controllerRef.current = controller;
  return request;
}

function isActiveRequest(
  requestRef: React.MutableRefObject<{ sequence: number; queryKey: string }>,
  request: TaskListRequest,
): boolean {
  return !request.controller.signal.aborted
    && requestRef.current.sequence === request.sequence
    && requestRef.current.queryKey === request.queryKey;
}

async function fetchTaskListPage(query: TaskCenterListQuery, signal: AbortSignal) {
  const params = taskListParams(query);
  return Promise.all([
    api<TaskCenterListPage>(`/tasks/page?${params.toString()}`, { signal }),
    api<SchedulingSetting>('/scheduling-settings', { signal }),
  ]);
}

export function useTaskCenterListPage() {
  const [query, setQuery] = React.useState<TaskCenterListQuery>({ page: 1, pageSize: TASK_LIST_PAGE_SIZE, type: 'all', status: 'all', q: '', groupKey: 'all' });
  const [items, setItems] = React.useState<TaskCenterListItem[]>([]);
  const [summary, setSummary] = React.useState<TaskCenterListSummary>(EMPTY_SUMMARY);
  const [groups, setGroups] = React.useState<TaskCenterListGroup[]>([]);
  const [total, setTotal] = React.useState(0);
  const [schedulingSetting, setSchedulingSetting] = React.useState<SchedulingSetting | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const queryRef = React.useRef(query);
  const requestRef = React.useRef({ sequence: 0, queryKey: '' });
  const controllerRef = React.useRef<AbortController | null>(null);
  queryRef.current = query;

  const load = React.useCallback(async (snapshot: TaskCenterListQuery): Promise<string> => {
    const request = beginRequest(requestRef, controllerRef, snapshot);
    setLoading(true);
    setError('');
    try {
      const [page, scheduling] = await fetchTaskListPage(snapshot, request.controller.signal);
      if (!isActiveRequest(requestRef, request)) return '';
      setItems(page.items);
      setSummary(page.summary);
      setGroups(page.groups);
      setTotal(page.total);
      setSchedulingSetting(scheduling);
      return '';
    } catch (failure) {
      if (!isActiveRequest(requestRef, request)) return '';
      const detail = requestError(failure);
      setError(`读取任务列表失败：${detail}`);
      return detail;
    } finally {
      if (isActiveRequest(requestRef, request)) setLoading(false);
    }
  }, []);

  const queryKey = taskListQueryKey(query);
  React.useEffect(() => {
    void load(queryRef.current);
    const timer = window.setInterval(() => void load(queryRef.current), TASK_LIST_POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      controllerRef.current?.abort();
    };
  }, [load, queryKey]);

  const reload = React.useCallback(() => load(queryRef.current), [load]);
  const setType = React.useCallback((type: TaskCenterListQuery['type']) => setQuery((current) => ({ ...current, page: 1, type, groupKey: 'all' })), []);
  const setSearch = React.useCallback((q: string) => setQuery((current) => ({ ...current, page: 1, q: q.trim(), groupKey: 'all' })), []);
  const setGroup = React.useCallback((groupKey: string) => setQuery((current) => ({ ...current, page: 1, groupKey })), []);
  const changePage = React.useCallback((page: number, pageSize: number) => {
    setQuery((current) => ({ ...current, page: pageSize === current.pageSize ? page : 1, pageSize }));
  }, []);

  return { query, items, summary, groups, total, schedulingSetting, loading, error, reload, setType, setSearch, setGroup, changePage } as const;
}
