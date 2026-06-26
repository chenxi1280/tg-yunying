export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api';
export const API_ORIGIN = API_BASE.replace(/\/api$/, '');
export const AUTH_EXPIRED_EVENT = 'tg-ops-auth-expired';

function apiDetailMessage(detail: unknown) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((item: any) => {
      const path = Array.isArray(item.loc) ? item.loc.join('.') : String(item.loc ?? '');
      const message = item.msg ?? JSON.stringify(item);
      return path ? `${path}: ${message}` : message;
    }).join('；');
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>;
    const message = String(record.message ?? record.failure_detail ?? '');
    const traceId = String(record.trace_id ?? '');
    if (message && traceId) return `${message}（trace_id: ${traceId}）`;
    return message || '';
  }
  return '';
}

function apiErrorMessage(status: number, body: string) {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    const detailMessage = apiDetailMessage(parsed.detail);
    if (detailMessage) return detailMessage;
  } catch {
    // Fall back to the raw response body below.
  }
  return body || `HTTP ${status}`;
}

/** 结构化 API 错误，包含 HTTP 状态码和响应正文。 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: string,
  ) {
    super(apiErrorMessage(status, body));
    this.name = 'ApiError';
  }
}

export class AuthExpiredError extends ApiError {
  constructor(status: number, body: string) {
    super(status, body);
    this.name = 'AuthExpiredError';
  }
}

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
}

export interface ApiResponse<T> {
  data: T;
  headers: Headers;
  status: number;
}

export function isAuthExpiredError(error: unknown): boolean {
  return error instanceof AuthExpiredError || (error instanceof ApiError && isAuthExpiredResponse(error.status, error.body));
}

function isAuthExpiredResponse(status: number, body: string): boolean {
  if (status !== 401) return false;
  return /token expired|permission version expired|invalid token|missing bearer token/i.test(body);
}

function notifyAuthExpired(status: number, body: string): void {
  window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT, { detail: { status, body } }));
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => '');
  if (isAuthExpiredResponse(response.status, text)) {
    notifyAuthExpired(response.status, text);
    return new AuthExpiredError(response.status, text);
  }
  return new ApiError(response.status, text);
}

export async function api<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  return (await apiWithMeta<T>(path, options)).data;
}

export async function apiWithMeta<T>(path: string, options?: ApiRequestOptions): Promise<ApiResponse<T>> {
  const token = localStorage.getItem('tg_ops_token');
  const isFormData = options?.body instanceof FormData;
  const controller = new AbortController();
  const { timeoutMs = 15_000, ...fetchOptions } = options ?? {};
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: {
        ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(fetchOptions.headers ?? {}),
      },
      signal: controller.signal,
      ...fetchOptions,
    });
    if (!response.ok) {
      throw await apiErrorFromResponse(response);
    }
    if (response.status === 204) {
      return { data: undefined as T, headers: response.headers, status: response.status };
    }
    const text = await response.text();
    if (!text.trim()) {
      return { data: undefined as T, headers: response.headers, status: response.status };
    }
    return { data: JSON.parse(text) as T, headers: response.headers, status: response.status };
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(408, 'request timeout');
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}
