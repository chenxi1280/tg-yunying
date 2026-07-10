import React from 'react';
import type { TablePaginationConfig } from 'antd/es/table';
import { api, apiWithMeta, ApiError } from '../../shared/api/client';
import type { OperationCenterSummary, OperationIssue, OperationPlan, OperationTarget, TargetRuntimeSummary } from '../types';

const TARGET_WORKBENCH_PAGE_SIZE = 8;

export type TargetPageQuery = Readonly<{ page: number; pageSize: number }>;
type BaseRequestIdentity = Readonly<{ sequence: number }>;
type BaseRequest = Readonly<{ sequence: number; controller: AbortController }>;
type TargetRequestIdentity = Readonly<{ sequence: number; queryKey: string }>;
type TargetRequest = Readonly<{
  sequence: number;
  queryKey: string;
  query: TargetPageQuery;
  controller: AbortController;
}>;
type BaseResult = Readonly<{
  plans: OperationPlan[];
  center: OperationCenterSummary;
  issues: OperationIssue[];
}>;
type TargetResult = Readonly<{
  targets: OperationTarget[];
  summaries: TargetRuntimeSummary[];
  total: number;
}>;

function requestError(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

function targetPageQueryKey(query: TargetPageQuery) {
  return JSON.stringify(query);
}

function operationTargetPagePath(query: TargetPageQuery) {
  const params = new URLSearchParams();
  params.set('page', String(query.page));
  params.set('page_size', String(query.pageSize));
  return `/operation-targets?${params.toString()}`;
}

function targetRuntimeSummaryPath(targetIds: readonly number[]) {
  const params = new URLSearchParams();
  if (!targetIds.length) params.append('target_ids', '');
  for (const targetId of targetIds) params.append('target_ids', String(targetId));
  return `/operation-targets/runtime-summary?${params.toString()}`;
}

function responseTotal(headers: Headers) {
  const rawTotal = headers.get('x-total-count');
  if (rawTotal === null) throw new Error('运营目标分页响应缺少 x-total-count');
  const total = Number(rawTotal);
  if (!Number.isSafeInteger(total) || total < 0) throw new Error(`运营目标总数无效：${rawTotal}`);
  return total;
}

function beginBaseRequest(
  identityRef: React.MutableRefObject<BaseRequestIdentity>,
  controllerRef: React.MutableRefObject<AbortController | null>,
): BaseRequest {
  controllerRef.current?.abort();
  const controller = new AbortController();
  const identity = { sequence: identityRef.current.sequence + 1 };
  identityRef.current = identity;
  controllerRef.current = controller;
  return { ...identity, controller };
}

function isActiveBaseRequest(identityRef: React.MutableRefObject<BaseRequestIdentity>, request: BaseRequest) {
  return !request.controller.signal.aborted && identityRef.current.sequence === request.sequence;
}

function beginTargetRequest(
  identityRef: React.MutableRefObject<TargetRequestIdentity>,
  controllerRef: React.MutableRefObject<AbortController | null>,
  query: TargetPageQuery,
): TargetRequest {
  controllerRef.current?.abort();
  const controller = new AbortController();
  const identity = { sequence: identityRef.current.sequence + 1, queryKey: targetPageQueryKey(query) };
  identityRef.current = identity;
  controllerRef.current = controller;
  return { ...identity, query, controller };
}

function isActiveTargetRequest(identityRef: React.MutableRefObject<TargetRequestIdentity>, request: TargetRequest) {
  return !request.controller.signal.aborted
    && identityRef.current.sequence === request.sequence
    && identityRef.current.queryKey === request.queryKey;
}

async function fetchOperationBase(request: BaseRequest): Promise<BaseResult> {
  const options = { signal: request.controller.signal };
  const [plans, center, issues] = await Promise.all([
    api<OperationPlan[]>('/operation-plans', options),
    api<OperationCenterSummary>('/operation-center/overview', options),
    api<OperationIssue[]>('/operation-issues', options),
  ]);
  return { plans, center, issues };
}

async function fetchTargetPage(request: TargetRequest): Promise<TargetResult> {
  const options = { signal: request.controller.signal };
  const targetResponse = await apiWithMeta<OperationTarget[]>(operationTargetPagePath(request.query), options);
  const runtimePath = targetRuntimeSummaryPath(targetResponse.data.map((target) => target.id));
  const summaries = await api<TargetRuntimeSummary[]>(runtimePath, options);
  return { targets: targetResponse.data, summaries, total: responseTotal(targetResponse.headers) };
}

function useOperationBase(initialCenter: OperationCenterSummary | null) {
  const [plans, setPlans] = React.useState<OperationPlan[]>([]);
  const [center, setCenter] = React.useState<OperationCenterSummary | null>(initialCenter);
  const [issues, setIssues] = React.useState<OperationIssue[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const identityRef = React.useRef<BaseRequestIdentity>({ sequence: 0 });
  const controllerRef = React.useRef<AbortController | null>(null);
  const load = React.useCallback(async () => {
    const request = beginBaseRequest(identityRef, controllerRef);
    setLoading(true);
    setError('');
    try {
      const result = await fetchOperationBase(request);
      if (!isActiveBaseRequest(identityRef, request)) return;
      setPlans(result.plans);
      setCenter(result.center);
      setIssues(result.issues);
    } catch (requestFailure) {
      if (isActiveBaseRequest(identityRef, request)) setError(requestError(requestFailure));
    } finally {
      if (isActiveBaseRequest(identityRef, request)) setLoading(false);
    }
  }, []);
  React.useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [load]);
  return {
    plans, setPlans, center, setCenter, issues, setIssues,
    loading, error, setLoading, setError, identityRef, controllerRef, load,
  };
}

async function refreshOperationBase(
  state: ReturnType<typeof useOperationBase>,
  actionLabel: string,
) {
  const request = beginBaseRequest(state.identityRef, state.controllerRef);
  state.setLoading(true);
  state.setError('');
  try {
    const result = await fetchOperationBase(request);
    if (!isActiveBaseRequest(state.identityRef, request)) return;
    state.setPlans(result.plans);
    state.setCenter(result.center);
    state.setIssues(result.issues);
  } catch (error) {
    if (!isActiveBaseRequest(state.identityRef, request)) return;
    state.setError(`运营中心数据刷新失败：${actionLabel}操作已完成，但刷新运营中心基础数据失败：${requestError(error)}`);
  } finally {
    if (isActiveBaseRequest(state.identityRef, request)) state.setLoading(false);
  }
}

function useTargetPage() {
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [summaries, setSummaries] = React.useState<TargetRuntimeSummary[]>([]);
  const [query, setQuery] = React.useState<TargetPageQuery>({ page: 1, pageSize: TARGET_WORKBENCH_PAGE_SIZE });
  const [total, setTotal] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const queryRef = React.useRef(query);
  const identityRef = React.useRef<TargetRequestIdentity>({ sequence: 0, queryKey: '' });
  const controllerRef = React.useRef<AbortController | null>(null);
  queryRef.current = query;
  const load = React.useCallback(async () => {
    const request = beginTargetRequest(identityRef, controllerRef, queryRef.current);
    setLoading(true);
    setError('');
    try {
      const result = await fetchTargetPage(request);
      if (!isActiveTargetRequest(identityRef, request)) return;
      setTargets(result.targets);
      setSummaries(result.summaries);
      setTotal(result.total);
    } catch (requestFailure) {
      if (isActiveTargetRequest(identityRef, request)) setError(requestError(requestFailure));
    } finally {
      if (isActiveTargetRequest(identityRef, request)) setLoading(false);
    }
  }, []);
  React.useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [load, query]);
  return { targets, setTargets, summaries, setSummaries, query, setQuery, total, setTotal, loading, setLoading, error, setError, queryRef, identityRef, controllerRef, load };
}

async function refreshTargetPage(state: ReturnType<typeof useTargetPage>, actionLabel: string) {
  const request = beginTargetRequest(state.identityRef, state.controllerRef, state.queryRef.current);
  state.setLoading(true);
  state.setError('');
  try {
    const result = await fetchTargetPage(request);
    if (!isActiveTargetRequest(state.identityRef, request)) return;
    state.setTargets(result.targets);
    state.setSummaries(result.summaries);
    state.setTotal(result.total);
  } catch (error) {
    if (!isActiveTargetRequest(state.identityRef, request)) return;
    state.setError(`运营中心数据刷新失败：${actionLabel}操作已完成，但刷新目标工作台数据失败：${requestError(error)}`);
  } finally {
    if (isActiveTargetRequest(state.identityRef, request)) state.setLoading(false);
  }
}

export function useOverviewOperationData(initialCenter: OperationCenterSummary | null) {
  const base = useOperationBase(initialCenter);
  const targetPage = useTargetPage();
  const loadOperationData = React.useCallback(
    async () => { await Promise.all([base.load(), targetPage.load()]); },
    [base.load, targetPage.load],
  );
  const refreshOperationDataAfterAction = React.useCallback(
    async (actionLabel: string) => {
      await Promise.all([refreshOperationBase(base, actionLabel), refreshTargetPage(targetPage, actionLabel)]);
    },
    [base, targetPage],
  );
  const changeTargetPage = (pagination: TablePaginationConfig) => {
    const pageSize = pagination.pageSize ?? targetPage.query.pageSize;
    const page = pageSize === targetPage.query.pageSize ? pagination.current ?? 1 : 1;
    targetPage.setQuery({ page, pageSize });
  };
  return {
    plans: base.plans, operationCenter: base.center, issues: base.issues,
    targets: targetPage.targets, targetSummaries: targetPage.summaries,
    targetPageQuery: targetPage.query, targetTotal: targetPage.total,
    operationLoading: base.loading || targetPage.loading,
    operationError: [base.error, targetPage.error].filter(Boolean).join('；'),
    loadOperationData, refreshOperationDataAfterAction, changeTargetPage,
  };
}
