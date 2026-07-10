import React from 'react';
import { apiWithMeta } from '../../shared/api/client';
import type { OperationTarget, OperationTargetOptionQuery } from '../types/operations';

const OPERATION_TARGET_PAGE_SIZE = 50;

type NormalizedTargetQuery = Readonly<{
  q: string;
  targetType?: OperationTarget['target_type'];
  accountId?: number;
  capability?: OperationTargetOptionQuery['capability'];
  ids: readonly number[];
}>;

type OperationTargetRequestIdentity = Readonly<{
  sequence: number;
  queryKey: string;
}>;

type RequestIdentityRef = React.MutableRefObject<OperationTargetRequestIdentity>;

function normalizeIds(ids: readonly number[] | undefined): readonly number[] {
  return [...new Set(ids ?? [])].sort((left, right) => left - right);
}

function normalizeQuery(
  query: OperationTargetOptionQuery,
  q: string,
  ids: readonly number[],
): NormalizedTargetQuery {
  return {
    q: q.trim(),
    targetType: query.targetType,
    accountId: query.accountId,
    capability: query.capability,
    ids,
  };
}

function queryIdentity(query: NormalizedTargetQuery): string {
  return JSON.stringify(query);
}

function operationTargetParams(query: NormalizedTargetQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set('page', '1');
  params.set('page_size', String(OPERATION_TARGET_PAGE_SIZE));
  if (query.q) params.set('q', query.q);
  if (query.targetType) params.set('target_type', query.targetType);
  if (query.accountId !== undefined) params.set('account_id', String(query.accountId));
  if (query.capability) params.set('capability', query.capability);
  for (const id of query.ids) params.append('ids', String(id));
  return params;
}

function beginRequest(ref: RequestIdentityRef, queryKey: string): OperationTargetRequestIdentity {
  const request = { sequence: ref.current.sequence + 1, queryKey };
  ref.current = request;
  return request;
}

function isCurrentRequest(ref: RequestIdentityRef, request: OperationTargetRequestIdentity): boolean {
  return ref.current.sequence === request.sequence && ref.current.queryKey === request.queryKey;
}

function responseTotal(rawTotal: string | null): number {
  if (rawTotal === null) throw new Error('运营目标分页响应缺少 x-total-count');
  const total = Number(rawTotal);
  if (!Number.isSafeInteger(total) || total < 0) throw new Error(`运营目标总数无效：${rawTotal}`);
  return total;
}

function requestErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function targetIdBatches(ids: readonly number[]): readonly (readonly number[])[] {
  const batches: number[][] = [];
  for (let index = 0; index < ids.length; index += OPERATION_TARGET_PAGE_SIZE) {
    batches.push(ids.slice(index, index + OPERATION_TARGET_PAGE_SIZE));
  }
  return batches;
}

export function mergeOperationTargets(
  current: readonly OperationTarget[],
  incoming: readonly OperationTarget[],
): OperationTarget[] {
  const byId = new Map(current.map((target) => [target.id, target]));
  for (const target of incoming) byId.set(target.id, target);
  return [...byId.values()];
}

async function loadTargetIdBatches(query: NormalizedTargetQuery) {
  return Promise.all(targetIdBatches(query.ids).map((ids) => {
    const batchQuery = { ...query, ids };
    return apiWithMeta<OperationTarget[]>(`/operation-targets?${operationTargetParams(batchQuery).toString()}`);
  }));
}

function useOperationTargetSearch(query: OperationTargetOptionQuery, searchText: string, reloadVersion: number) {
  const [pageTargets, setPageTargets] = React.useState<OperationTarget[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const [total, setTotal] = React.useState(0);
  const searchRequestRef = React.useRef<OperationTargetRequestIdentity>({ sequence: 0, queryKey: '' });
  const loadSearchPage = React.useCallback(async () => {
    const normalized = normalizeQuery(query, searchText, []);
    const request = beginRequest(searchRequestRef, queryIdentity(normalized));
    setLoading(true);
    setError('');
    setPageTargets([]);
    try {
      const response = await apiWithMeta<OperationTarget[]>(`/operation-targets?${operationTargetParams(normalized).toString()}`);
      if (!isCurrentRequest(searchRequestRef, request)) return;
      setPageTargets(response.data);
      setTotal(responseTotal(response.headers.get('x-total-count')));
    } catch (error) {
      if (!isCurrentRequest(searchRequestRef, request)) return;
      setError(requestErrorMessage(error));
    } finally {
      if (isCurrentRequest(searchRequestRef, request)) setLoading(false);
    }
  }, [query.accountId, query.capability, query.targetType, searchText]);
  React.useEffect(() => { void loadSearchPage(); }, [loadSearchPage, reloadVersion]);
  return { pageTargets, loading, error, total } as const;
}

function useOperationTargetHydration(query: OperationTargetOptionQuery, selectedIds: readonly number[], reloadVersion: number) {
  const [selectedTargets, setSelectedTargets] = React.useState<OperationTarget[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const hydrationRequestRef = React.useRef<OperationTargetRequestIdentity>({ sequence: 0, queryKey: '' });
  const ensureIds = React.useCallback(async (ids: readonly number[]) => {
    const normalizedIds = normalizeIds(ids);
    const normalized = normalizeQuery(query, '', normalizedIds);
    const request = beginRequest(hydrationRequestRef, queryIdentity(normalized));
    setError('');
    if (!normalizedIds.length) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const responses = await loadTargetIdBatches(normalized);
      if (!isCurrentRequest(hydrationRequestRef, request)) return;
      for (const response of responses) {
        setSelectedTargets((current) => mergeOperationTargets(current, response.data));
      }
    } catch (error) {
      if (!isCurrentRequest(hydrationRequestRef, request)) return;
      setError(requestErrorMessage(error));
    } finally {
      if (isCurrentRequest(hydrationRequestRef, request)) setLoading(false);
    }
  }, [query.accountId, query.capability, query.targetType]);
  const selectedIdsKey = selectedIds.join(',');
  React.useEffect(() => {
    const allowedIds = new Set(selectedIds);
    setSelectedTargets((current) => current.filter((target) => allowedIds.has(target.id)));
    void ensureIds(selectedIds);
  }, [ensureIds, reloadVersion, selectedIdsKey]);
  return { selectedTargets, loading, error, ensureIds } as const;
}

export function useOperationTargetOptions(query: OperationTargetOptionQuery) {
  const [searchText, setSearchText] = React.useState(query.q?.trim() ?? '');
  const [reloadVersion, setReloadVersion] = React.useState(0);
  const inputIdsKey = (query.ids ?? []).join(',');
  const selectedIds = React.useMemo(() => normalizeIds(query.ids), [inputIdsKey]);
  const searchState = useOperationTargetSearch(query, searchText, reloadVersion);
  const hydrationState = useOperationTargetHydration(query, selectedIds, reloadVersion);
  React.useEffect(() => setSearchText(query.q?.trim() ?? ''), [query.q]);

  const targets = React.useMemo(
    () => mergeOperationTargets(hydrationState.selectedTargets, searchState.pageTargets),
    [hydrationState.selectedTargets, searchState.pageTargets],
  );
  const search = React.useCallback((value: string) => setSearchText(value.trim()), []);
  const reload = React.useCallback(() => setReloadVersion((current) => current + 1), []);

  return {
    targets,
    loading: searchState.loading || hydrationState.loading,
    error: hydrationState.error || searchState.error,
    total: searchState.total,
    search,
    ensureIds: hydrationState.ensureIds,
    reload,
  } as const;
}
