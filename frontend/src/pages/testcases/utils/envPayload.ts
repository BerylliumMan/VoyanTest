/**
 * 环境变量 / 公共请求头 的表单 <-> 后端 payload 转换。
 *
 * 后端契约（specs/029-api-testing/contracts/api-test-contract.md §1.6）：
 * - GET 回显时 secret=true 的项 value 恒为 "******"，并用 has_value 表示库里是否真有值。
 * - PUT 更新幂等：secret 项若回传 value === "******" 表示未修改，后端保留库中原值；
 *   回传其它值则覆盖。因此表单里「保持不变」就是原样保留 "******"，用户一输入即为新值。
 * - has_value=false（库里为空）的 secret 项在表单里归一为 ""，配合「未设置」占位符显示。
 */
import type { EnvHeader, EnvVariable } from '../types';

export const MASKED_VALUE = '******';

export interface EnvVariableForm {
  key: string;
  value: string;
  secret: boolean;
  enable: boolean;
  /** 仅用于 UI 判断是否显示「未设置」占位，不回传后端 */
  has_value: boolean;
}

export interface EnvVariablePayload {
  key: string;
  value: string;
  secret: boolean;
  enable: boolean;
}

export interface EnvHeaderForm {
  key: string;
  value: string;
  enable: boolean;
}

export interface EnvHeaderPayload {
  key: string;
  value: string;
  enable: boolean;
}

/** 后端回显 -> 表单可编辑值。secret 且库里无值 -> ""（由占位符提示未设置）。 */
export function toEnvVariablesForm(variables?: EnvVariable[] | null): EnvVariableForm[] {
  return (variables || []).map((v) => {
    const secret = !!v.secret;
    const value = secret
      ? (v.has_value === false ? '' : MASKED_VALUE)
      : (v.value ?? '');
    return {
      key: v.key ?? '',
      value,
      secret,
      enable: v.enable !== false,
      has_value: !!v.has_value,
    };
  });
}

export function toEnvHeadersForm(headers?: EnvHeader[] | null): EnvHeaderForm[] {
  return (headers || []).map((h) => ({
    key: h.key ?? '',
    value: h.value ?? '',
    enable: h.enable !== false,
  }));
}

/** 表单值 -> 后端 payload。剔除 has_value；丢弃空 key 行以避免后端 400。 */
export function serializeEnvVariables(variables?: EnvVariableForm[] | null): EnvVariablePayload[] {
  return (variables || [])
    .filter((v) => String(v?.key ?? '').trim() !== '')
    .map((v) => ({
      key: v.key,
      value: v.value ?? '',
      secret: !!v.secret,
      enable: v.enable !== false,
    }));
}

export function serializeEnvHeaders(headers?: EnvHeaderForm[] | null): EnvHeaderPayload[] {
  return (headers || [])
    .filter((h) => String(h?.key ?? '').trim() !== '')
    .map((h) => ({
      key: h.key,
      value: h.value ?? '',
      enable: h.enable !== false,
    }));
}
