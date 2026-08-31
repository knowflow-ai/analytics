const TOKEN_KEY = 'knowflow-analytics.access-token';
export const UNAUTHORIZED_EVENT = 'knowflow-analytics:unauthorized';

import { API_ROOT, EDITION } from './edition';

export { EDITION };

const CORE_PREFIX = '/v1/analytics';

function rewritePath(path: string): string {
  if (!API_ROOT || !path.startsWith(`${CORE_PREFIX}/`)) return path;
  return API_ROOT + path.slice(CORE_PREFIX.length);
}

/** Error with the core service's business code, so pages can translate it. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly stage?: string;

  constructor(status: number, code: string, message: string, stage?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.stage = stage;
  }

  get isConflict() {
    return this.status === 409;
  }
}

export function getAccessToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setAccessToken(token: string) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private mode: the session simply needs to log in again next time.
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  projectId?: string;
  query?: Record<string, string | number | boolean | undefined>;
}

function errorFromPayload(status: number, payload: unknown): ApiError {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    const error = record.error as Record<string, unknown> | undefined;
    if (error && typeof error === 'object') {
      return new ApiError(
        status,
        String(error.code ?? 'ERROR'),
        String(error.message ?? '请求失败'),
        error.stage ? String(error.stage) : undefined,
      );
    }
    const detail = record.detail;
    if (typeof detail === 'string') return new ApiError(status, detail, detail);
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      const structured = detail as Record<string, unknown>;
      if (structured.code || structured.message) {
        return new ApiError(
          status,
          String(structured.code ?? 'ERROR'),
          String(structured.message ?? '请求失败'),
        );
      }
    }
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string; loc?: unknown[] } | undefined;
      const location = Array.isArray(first?.loc) ? first?.loc.join('.') : '';
      return new ApiError(
        status,
        'VALIDATION_ERROR',
        `${location ? `${location}: ` : ''}${first?.msg ?? '请求参数无效'}`,
      );
    }
  }
  return new ApiError(status, 'HTTP_ERROR', `请求失败（HTTP ${status}）`);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(rewritePath(path), window.location.origin);
  Object.entries(options.query ?? {}).forEach(([key, value]) => {
    if (value !== undefined) url.searchParams.set(key, String(value));
  });
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (EDITION === 'embedded') {
    const hostToken = safeLocalStorage('Authorization');
    if (hostToken) headers.Authorization = hostToken;
  } else {
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (options.projectId) headers['X-KnowFlow-Project-Id'] = options.projectId;
  const method = options.method ?? 'GET';
  // The core refuses bodyless writes (411), so every write carries JSON.
  const body =
    method === 'GET' ? undefined : JSON.stringify(options.body ?? {});
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await fetch(url, { method, headers, body });
  const text = await response.text();
  const payload = text ? safeJson(text) : null;
  if (response.status === 401 && EDITION === 'embedded') {
    // 宿主 token 失效:回宿主登录页,登录后由宿主带回。
    window.location.assign('/login');
    throw errorFromPayload(response.status, payload);
  }
  if (response.status === 401 && getAccessToken()) {
    // A rotated or mistyped password: forget it and let the app show the login page.
    setAccessToken('');
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  if (!response.ok) throw errorFromPayload(response.status, payload);
  return payload as T;
}

function safeLocalStorage(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? '';
  } catch {
    return '';
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
