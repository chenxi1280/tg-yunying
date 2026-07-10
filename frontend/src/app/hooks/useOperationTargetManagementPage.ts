import React from 'react';
import type { TablePaginationConfig } from 'antd/es/table';
import { apiWithMeta, ApiError } from '../../shared/api/client';
import type { OperationTarget } from '../types';

const OPERATION_TARGET_PAGE_SIZE = 20;

export type TargetListQuery = Readonly<{ page: number; pageSize: number; q: string }>;
type TargetListRequestIdentity = Readonly<{ sequence: number; queryKey: string }>;
type TargetListRequest = Readonly<{
  sequence: number;
  queryKey: string;
  query: TargetListQuery;
  controller: AbortController;
}>;
type TargetListResult = Readonly<{ targets: OperationTarget[]; total: number }>;

type FocusTarget = Readonly<{ targetId: number; nonce: number }>;
type ManagementPageOptions = Readonly<{
  focusTarget?: FocusTarget | null;
  onFocusTargetConsumed?: () => void;
  onOpenFocusedTarget: (target: OperationTarget) => void;
  onMissingFocusedTarget: (targetId: number) => void;
  setError: (error: string) => void;
}>;

function requestError(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

function targetListQueryKey(query: TargetListQuery) {
  return JSON.stringify(query);
}

function operationTargetListPath(query: TargetListQuery) {
  const params = new URLSearchParams();
  params.set('page', String(query.page));
  params.set('page_size', String(query.pageSize));
  if (query.q) params.set('q', query.q);
  return `/operation-targets?${params.toString()}`;
}

function responseTotal(headers: Headers) {
  const rawTotal = headers.get('x-total-count');
  if (rawTotal === null) throw new Error('运营目标分页响应缺少 x-total-count');
  const total = Number(rawTotal);
  if (!Number.isSafeInteger(total) || total < 0) throw new Error(`运营目标总数无效：${rawTotal}`);
  return total;
}

function beginRequest(
  identityRef: React.MutableRefObject<TargetListRequestIdentity>,
  controllerRef: React.MutableRefObject<AbortController | null>,
  query: TargetListQuery,
): TargetListRequest {
  controllerRef.current?.abort();
  const controller = new AbortController();
  const identity = { sequence: identityRef.current.sequence + 1, queryKey: targetListQueryKey(query) };
  identityRef.current = identity;
  controllerRef.current = controller;
  return { ...identity, query, controller };
}

function isActiveRequest(identityRef: React.MutableRefObject<TargetListRequestIdentity>, request: TargetListRequest) {
  return !request.controller.signal.aborted
    && identityRef.current.sequence === request.sequence
    && identityRef.current.queryKey === request.queryKey;
}

async function fetchTargetPage(request: TargetListRequest): Promise<TargetListResult> {
  const response = await apiWithMeta<OperationTarget[]>(operationTargetListPath(request.query), {
    signal: request.controller.signal,
  });
  return { targets: response.data, total: responseTotal(response.headers) };
}

function useTargetPolling(
  load: () => Promise<void>,
  controllerRef: React.MutableRefObject<AbortController | null>,
  query: TargetListQuery,
) {
  React.useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [load, query]);
  React.useEffect(() => {
    const timer = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(timer);
  }, [load]);
}

function useTargetList(setError: (error: string) => void) {
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [query, setQuery] = React.useState<TargetListQuery>({ page: 1, pageSize: OPERATION_TARGET_PAGE_SIZE, q: '' });
  const [total, setTotal] = React.useState(0);
  const [search, setSearch] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const queryRef = React.useRef(query);
  const identityRef = React.useRef<TargetListRequestIdentity>({ sequence: 0, queryKey: '' });
  const controllerRef = React.useRef<AbortController | null>(null);
  queryRef.current = query;
  const load = React.useCallback(async () => {
    const request = beginRequest(identityRef, controllerRef, queryRef.current);
    setLoading(true);
    setError('');
    try {
      const result = await fetchTargetPage(request);
      if (!isActiveRequest(identityRef, request)) return;
      setTargets(result.targets);
      setTotal(result.total);
    } catch (error) {
      if (isActiveRequest(identityRef, request)) setError(requestError(error));
    } finally {
      if (isActiveRequest(identityRef, request)) setLoading(false);
    }
  }, [setError]);
  useTargetPolling(load, controllerRef, query);
  return {
    targets, setTargets, query, setQuery, total, setTotal, search, setSearch,
    loading, setLoading, queryRef, identityRef, controllerRef, load,
  };
}

async function refreshTargetList(
  state: ReturnType<typeof useTargetList>,
  setError: (error: string) => void,
  actionLabel: string,
) {
  const request = beginRequest(state.identityRef, state.controllerRef, state.queryRef.current);
  state.setLoading(false);
  try {
    const result = await fetchTargetPage(request);
    if (!isActiveRequest(state.identityRef, request)) return;
    state.setTargets(result.targets);
    state.setTotal(result.total);
  } catch (error) {
    if (!isActiveRequest(state.identityRef, request)) return;
    setError(`运营目标数据刷新失败：${actionLabel}操作已完成，但刷新运营目标列表失败：${requestError(error)}`);
  }
}

function focusedTargetPath(targetId: number) {
  const params = new URLSearchParams();
  params.set('page', '1');
  params.set('page_size', '1');
  params.append('ids', String(targetId));
  return `/operation-targets?${params.toString()}`;
}

function useFocusedTarget(targets: OperationTarget[], options: ManagementPageOptions) {
  const appliedNonce = React.useRef<number | null>(null);
  const controllerRef = React.useRef<AbortController | null>(null);
  const optionsRef = React.useRef(options);
  optionsRef.current = options;
  React.useEffect(() => {
    const focus = options.focusTarget;
    if (!focus || appliedNonce.current === focus.nonce) return;
    const current = targets.find((target) => target.id === focus.targetId);
    if (current) return consumeFocusedTarget({ target: current, nonce: focus.nonce, appliedNonce, optionsRef });
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    void apiWithMeta<OperationTarget[]>(focusedTargetPath(focus.targetId), { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted || appliedNonce.current === focus.nonce) return;
        const target = response.data.find((item) => item.id === focus.targetId);
        if (target) return consumeFocusedTarget({ target, nonce: focus.nonce, appliedNonce, optionsRef });
        appliedNonce.current = focus.nonce;
        optionsRef.current.onMissingFocusedTarget(focus.targetId);
        optionsRef.current.onFocusTargetConsumed?.();
      })
      .catch((error) => {
        if (!controller.signal.aborted) optionsRef.current.setError(requestError(error));
      });
    return () => controller.abort();
  }, [options.focusTarget, targets]);
}

function consumeFocusedTarget(
  context: Readonly<{
    target: OperationTarget;
    nonce: number;
    appliedNonce: React.MutableRefObject<number | null>;
    optionsRef: React.MutableRefObject<ManagementPageOptions>;
  }>,
) {
  context.appliedNonce.current = context.nonce;
  context.optionsRef.current.onOpenFocusedTarget(context.target);
  context.optionsRef.current.onFocusTargetConsumed?.();
}

export function useOperationTargetManagementPage(options: ManagementPageOptions) {
  const state = useTargetList(options.setError);
  useFocusedTarget(state.targets, options);
  const refreshAfterAction = React.useCallback(
    (actionLabel: string) => refreshTargetList(state, options.setError, actionLabel),
    [options.setError, state],
  );
  const submitSearch = (value: string) => {
    state.setSearch(value);
    state.setQuery((current) => ({ ...current, page: 1, q: value.trim() }));
  };
  const changePage = (pagination: TablePaginationConfig) => {
    const pageSize = pagination.pageSize ?? state.query.pageSize;
    const page = pageSize === state.query.pageSize ? pagination.current ?? 1 : 1;
    state.setQuery((current) => ({ ...current, page, pageSize }));
  };
  return { ...state, load: state.load, refreshAfterAction, submitSearch, changePage };
}
