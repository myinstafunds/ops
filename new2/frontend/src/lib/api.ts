/**
 * The single HTTP boundary.
 *
 * Two rules are enforced here rather than in components:
 *
 *  1. The bearer token is attached in one place, so no screen can forget it.
 *  2. Backend failures are unwrapped into a typed `ApiError`, so callers branch
 *     on `err.code` instead of digging through response shapes.
 *
 * Note that every list endpoint answers with `{data, meta, trace_id}` while
 * single-object endpoints answer with the bare object. `list` and `send` below
 * are deliberately separate helpers because that difference is real.
 */

import axios, { type AxiosError, type AxiosInstance } from 'axios'

import type { ErrorResponse } from '@/types'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown>
  readonly traceId: string | null

  constructor(
    message: string,
    code: string,
    status: number,
    details: Record<string, unknown> = {},
    traceId: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
    this.traceId = traceId
  }

  /** The permissions a 403 named, when the guard reported them. */
  get requiredPermissions(): string[] {
    const value = this.details['required_permissions']
    return Array.isArray(value) ? value.map(String) : []
  }
}

const TOKEN_KEY = 'ops.access_token'
const REFRESH_KEY = 'ops.refresh_token'

export const tokenStore = {
  get access(): string | null {
    return localStorage.getItem(TOKEN_KEY)
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY)
  },
  set(access: string, refresh: string | null): void {
    localStorage.setItem(TOKEN_KEY, access)
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear(): void {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

export const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

http.interceptors.request.use((config) => {
  const token = tokenStore.access
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

/** Raised once per failed refresh so the app can bounce to the login screen. */
type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler = () => {}
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler
}

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error

  if (axios.isAxiosError(error)) {
    const axiosError = error as AxiosError<ErrorResponse>
    const status = axiosError.response?.status ?? 0
    const body = axiosError.response?.data

    if (body && typeof body === 'object' && 'error' in body && body.error) {
      return new ApiError(
        body.error.message || 'The request failed.',
        body.error.code || 'UNKNOWN',
        status,
        body.error.details ?? {},
        body.trace_id ?? null,
      )
    }

    if (!axiosError.response) {
      return new ApiError(
        'Cannot reach the Ops API. Check that the backend is running.',
        'NETWORK_ERROR',
        0,
      )
    }
    return new ApiError(axiosError.message, 'HTTP_ERROR', status)
  }

  return new ApiError(String(error), 'UNKNOWN', 0)
}

http.interceptors.response.use(
  (response) => response,
  (error: unknown) => Promise.reject(toApiError(error)),
)

/** A request whose body is a single object; the API returns that object bare. */
export async function send<T>(
  method: 'get' | 'post' | 'patch' | 'put' | 'delete',
  path: string,
  body?: unknown,
  params?: Record<string, unknown>,
): Promise<T> {
  try {
    const response = await http.request<T>({ method, url: path, data: body, params })
    return response.data
  } catch (error) {
    const apiError = toApiError(error)
    if (apiError.status === 401) onUnauthorized()
    throw apiError
  }
}

/** A request against a paginated endpoint; unwraps `{data, meta, trace_id}`. */
export async function list<T>(
  path: string,
  params?: Record<string, unknown>,
): Promise<{ data: T[]; meta: import('@/types').PageMeta; trace_id: string | null }> {
  try {
    const response = await http.get<{ data: T[]; meta: import('@/types').PageMeta; trace_id: string | null }>(
      path,
      { params },
    )
    return response.data
  } catch (error) {
    const apiError = toApiError(error)
    if (apiError.status === 401) onUnauthorized()
    throw apiError
  }
}

export { toApiError }
