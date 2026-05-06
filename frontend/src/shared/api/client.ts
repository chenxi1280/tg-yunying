export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api';
export const API_ORIGIN = API_BASE.replace(/\/api$/, '');

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem('tg_ops_token');
  const isFormData = options?.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options?.headers ?? {}),
    },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${response.status}: ${await response.text()}`);
  }
  return response.json();
}
