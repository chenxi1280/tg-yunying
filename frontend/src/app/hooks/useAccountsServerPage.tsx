import React from 'react';
import { Input } from 'antd';
import type { TablePaginationConfig } from 'antd/es/table';
import { apiWithMeta } from '../../shared/api/client';
import type { Account } from '../types';

const ACCOUNT_PAGE_SIZE = 20;

interface Options {
  accounts: Account[];
  total: number;
  selectedPoolId: number | '';
  onPageLoaded: (rows: Account[], total: number) => void;
}

interface PaginationOptions {
  current: number;
  total: number;
  query: string;
  loadPage: (page: number, search: string) => Promise<void>;
}

function accountPagePath(page: number, selectedPoolId: number | '', search: string) {
  const params = new URLSearchParams({ page: String(page), page_size: String(ACCOUNT_PAGE_SIZE) });
  if (selectedPoolId) params.set('pool_id', String(selectedPoolId));
  if (search) params.set('search', search);
  return `/tg-accounts?${params.toString()}`;
}

export function useAccountsServerPage(options: Options) {
  const [query, setQueryState] = React.useState('');
  const [current, setCurrent] = React.useState(1);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const requestSeq = React.useRef(0);

  React.useEffect(() => {
    setCurrent(1);
    setQueryState('');
  }, [options.selectedPoolId]);

  async function requestPage(page: number, search: string, currentRequest: number) {
    const response = await apiWithMeta<Account[]>(accountPagePath(page, options.selectedPoolId, search));
    if (requestSeq.current !== currentRequest) return;
    setCurrent(page);
    options.onPageLoaded(response.data, Number(response.headers.get('X-Total-Count') || response.data.length));
  }

  async function loadPage(page: number, search: string) {
    requestSeq.current += 1;
    const currentRequest = requestSeq.current;
    setLoading(true);
    setError('');
    try {
      await requestPage(page, search, currentRequest);
    } catch (error) {
      if (requestSeq.current !== currentRequest) return;
      setError(error instanceof Error ? error.message : '读取账号列表失败');
    } finally {
      if (requestSeq.current === currentRequest) setLoading(false);
    }
  }

  function applyQuery(value: string) {
    const nextQuery = value.trim();
    setQueryState(nextQuery);
    void loadPage(1, nextQuery);
  }

  const pagination = createPagination({ current, total: options.total, query, loadPage });

  return {
    rows: options.accounts,
    pagination,
    query,
    setQuery: applyQuery,
    loading,
    error,
    searchInput: <AccountSearchInput query={query} setQuery={setQueryState} applyQuery={applyQuery} />,
  };
}

function createPagination(options: PaginationOptions): TablePaginationConfig {
  return {
    current: options.current,
    pageSize: ACCOUNT_PAGE_SIZE,
    total: options.total,
    showSizeChanger: false,
    showTotal: (total, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${total} 条`,
    onChange: (page) => void options.loadPage(page, options.query),
  };
}

function AccountSearchInput(props: { query: string; setQuery: (value: string) => void; applyQuery: (value: string) => void }) {
  return (
    <Input.Search
      allowClear
      className="table-search"
      value={props.query}
      placeholder="搜索账号 / 登录有问题 / username / 手机号 / 分组 / 状态 / 代理"
      onChange={(event) => props.setQuery(event.target.value)}
      onSearch={props.applyQuery}
      style={{ width: 320, maxWidth: '100%' }}
    />
  );
}
