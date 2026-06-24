import axios, { AxiosRequestConfig } from 'axios';
import { Message } from '@arco-design/web-react';

/**
 * 统一 API 错误：把 axios 抛出的任意错误归一成可识别的 ApiError 实例。
 *
 * 兼容旧的 `e.response.data.detail` 取法：
 *   - `message` 取后端 detail（或上层传入的 fallback）
 *   - `status` 取 HTTP 状态码（401/500 等），0 表示网络层失败
 */
export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export interface ApiRequestOptions {
  /** 成功时的 Message.success 提示文案；不传则不弹 */
  successMessage?: string;
  /** 失败时的兜底错误文案；后端 detail 缺失时使用 */
  errorMessage?: string;
  /** 是否弹 Message.success；GET 默认 false，其余默认 true */
  showSuccess?: boolean;
  /** 是否弹 Message.error；默认 true */
  showError?: boolean;
}

/**
 * 统一 axios 包装：自动弹成功/失败消息，错误归一为 ApiError 抛出。
 *
 * 用法：
 *   const data = await apiRequest<Todo[]>({ method: 'GET', url: '/api/todos' });
 *   await apiRequest({ method: 'POST', url: '/api/todos', data }, {
 *     successMessage: t['create.success'],
 *   });
 *
 * 行为约定：
 *   - 默认 `showSuccess` 规则：GET 不弹，其余方法在传了 `successMessage` 时弹
 *   - 默认 `showError` 为 true；想静默失败时传 `{ showError: false }`
 *   - 不修改 main.tsx 中的全局 axios 拦截器（401 重定向仍由拦截器处理）
 */
export async function apiRequest<T = unknown>(
  config: AxiosRequestConfig,
  options?: ApiRequestOptions
): Promise<T> {
  const { successMessage, errorMessage, showSuccess, showError } = {
    showSuccess: (config.method || 'get').toLowerCase() !== 'get',
    showError: true,
    ...options,
  };

  try {
    const response = await axios(config);
    if (showSuccess && successMessage) {
      Message.success(successMessage);
    }
    return response.data as T;
  } catch (e: unknown) {
    const err = e as {
      response?: { data?: { detail?: string }; status?: number };
      message?: string;
    };
    const detail =
      err?.response?.data?.detail || errorMessage || '操作失败';
    if (showError) {
      Message.error(detail);
    }
    throw new ApiError(detail, err?.response?.status || 0);
  }
}

/** GET 便捷方法：默认不弹成功消息（纯查询） */
export const apiGet = <T = unknown>(url: string, params?: Record<string, unknown>) =>
  apiRequest<T>({ method: 'GET', url, params }, { showSuccess: false });

/** POST 便捷方法：传 successMessage 时弹成功提示 */
export const apiPost = <T = unknown>(url: string, data?: unknown, msg?: string) =>
  apiRequest<T>({ method: 'POST', url, data }, { successMessage: msg });

/** PUT 便捷方法：传 successMessage 时弹成功提示 */
export const apiPut = <T = unknown>(url: string, data?: unknown, msg?: string) =>
  apiRequest<T>({ method: 'PUT', url, data }, { successMessage: msg });

/** DELETE 便捷方法：传 successMessage 时弹成功提示 */
export const apiDelete = <T = unknown>(url: string, msg?: string) =>
  apiRequest<T>({ method: 'DELETE', url }, { successMessage: msg });
