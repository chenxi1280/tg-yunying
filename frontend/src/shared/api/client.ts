export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api';
export const API_ORIGIN = API_BASE.replace(/\/api$/, '');
export const AUTH_EXPIRED_EVENT = 'tg-ops-auth-expired';

/** 结构化 API 错误，包含 HTTP 状态码和响应正文。 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: string,
  ) {
    super(`${status}: ${body}`);
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

export async function api<T>(path: string, options?: ApiRequestOptions): Promise<T> {
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
      const text = await response.text().catch(() => '');
      if (isAuthExpiredResponse(response.status, text)) {
        notifyAuthExpired(response.status, text);
        throw new AuthExpiredError(response.status, text);
      }
      throw new ApiError(response.status, text);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    const text = await response.text();
    if (!text.trim()) {
      return undefined as T;
    }
    return JSON.parse(text) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(408, 'request timeout');
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}
