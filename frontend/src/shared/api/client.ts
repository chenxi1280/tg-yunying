export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api';
export const API_ORIGIN = API_BASE.replace(/\/api$/, '');

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

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
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
