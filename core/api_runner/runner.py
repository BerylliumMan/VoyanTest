# core/api_runner/runner.py
"""接口用例执行器（029-api-testing T029）。

契约：specs/029-api-testing/contracts/api-test-contract.md §2（执行语义）与
data-model.md §5（steps 形状为前端 StepDetail 超集）。

职责：
- 归一 api_spec（normalize_api_spec）→ 按 order 升序执行步骤（enable=False 跳过）
- 每步：build_request 渲染 → httpx 发送 → run_assertions → run_extractors
  （scope=case 写回 runtime 层，scope=environment 写回 env 层）→ 组装冻结字段步骤结果
- fail_policy：fail_fast（失败即停，后续步骤 skipped）/ continue（继续执行，
  整体 status=failed）
- 脱敏：步骤结果与 rendered_requests 的请求头经 mask_headers；响应体不脱敏
  （调用方写报告时处理）
- 纯 async：除 httpx 外无 IO；不写数据库、不写文件（落库由调用方负责）；
  httpx 请求异常不得冒泡出 run_api_case
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
import json
import re
from typing import Any, Optional

import httpx

from core.api_runner.assertions import ResponseLike, run_assertions
from core.api_runner.extractors import ExtractionError, run_extractors
from core.api_runner.iteration import (
    dataset_mode_notes,
    expand_iteration_rows,
    plan_iterations,
    tag_iteration,
)
from core.api_runner.request_builder import (
    RenderedRequest,
    build_request,
    parse_platform_ref,
)
from core.api_runner.variables import (
    VariableScope,
    mask_headers,
    render,
    variables_to_map,
)
from core.api_spec import normalize_api_spec

SKIPPED_REASON_PREVIOUS_FAILURE = "因前序步骤失败跳过"
# 031（US9）：fail_fast 下后续迭代不执行，但要在报告中可见（不静默丢弃）
SKIPPED_REASON_PREVIOUS_ITERATION = "前序迭代失败（fail_fast）未执行"
SKIPPED_REASON_STEP_LIMIT = "因 step_limit 截断未执行"
SKIPPED_REASON_DISABLED = "步骤已禁用"


@dataclass
class ApiRunResult:
    """一次接口用例执行的完整结果（供调用方落库/报告）。

    ``variables`` 为最终变量快照（六级作用域合并，含 baseUrl）；
    ``environment_variables`` 仅含提取器 scope="environment" 产出的变量
    （供调用方写回 environments.variables，跨用例可见）。
    """

    status: str = "failed"  # "passed" | "failed"
    steps: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: int = 0
    rendered_requests: list[dict] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    environment_variables: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # dataset_mode 等降级说明
    # 本次结束时的 runtime（提取结果）；场景把这份字典传给下一步
    runtime: dict[str, str] = field(default_factory=dict)


def _create_default_client(verify_ssl: bool = True) -> httpx.AsyncClient:
    """client 未注入时的默认客户端（测试可 monkeypatch）。

    httpx 的 ``verify`` 是**客户端级**参数（不能按请求传），所以 TLS 校验
    按 Postman 语义在 run 级决定：任一步骤声明 ``verify_ssl=false`` → 整个 run 关闭校验。
    """
    return httpx.AsyncClient(verify=verify_ssl)


def _resolve_verify_ssl(spec: dict) -> bool:
    """run 级 TLS 校验开关：任一步骤要求跳过校验则为 False（自签/内网 HTTPS 场景）。"""
    for step in spec.get("steps") or []:
        if step.get("enable") is False:
            continue
        if (step.get("request") or {}).get("verify_ssl") is False:
            return False
    return True


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _step_number(step: dict) -> int:
    try:
        return int(step.get("order") or 0)
    except (TypeError, ValueError):
        return 0


def _build_scope(
    api_spec: dict,
    environment: Optional[dict],
    case_variables: Optional[list[dict]],
    dataset_row: Optional[dict],
    seed_runtime: Optional[dict] = None,
) -> VariableScope:
    """组装六级作用域：runtime > step > case > dataset > env > baseUrl。

    case 层 = api_spec.variables 合并参数 case_variables（后者覆盖）。
    ``seed_runtime`` 是场景上一步提取进来的变量，优先级仍最高。
    """
    env = environment or {}
    spec_vars = variables_to_map(api_spec.get("variables"))
    param_vars = variables_to_map(case_variables)
    case_vars = {**spec_vars, **param_vars}
    return VariableScope(
        runtime=dict(seed_runtime or {}),
        case_variables=case_vars,
        dataset_row=dict(dataset_row or {}),
        # 031（US4）：多域名服务表（{"auth": "http://a:8000"}）
        services=dict(env.get("services") or {}),
        env_variables=variables_to_map(env.get("variables")),
        base_url=str(env.get("base_url") or ""),
    )


def _render_header_items(items: Any, scope: VariableScope) -> dict[str, str]:
    """渲染 ``[{key,value,enable}]`` → dict（环境公共请求头用）。"""
    out: dict[str, str] = {}
    for it in items or []:
        if not isinstance(it, dict) or it.get("enable") is False:
            continue
        key = render(it.get("key"), scope)
        if not key:
            continue
        out[key] = render(it.get("value"), scope)
    return out


def _merge_headers(base: dict, override: dict) -> dict[str, str]:
    """大小写不敏感合并：override 覆盖 base 同名项，保留新 key 原始大小写。"""
    out: dict[str, str] = {}
    for key, value in base.items():
        out[key] = value
    for key, value in override.items():
        for existing in list(out):
            if existing.lower() == key.lower():
                del out[existing]
        out[key] = value
    return out


def _request_dict(req: RenderedRequest) -> dict:
    """步骤结果/rendered_requests 的请求形状（headers 脱敏）。"""
    return {
        "method": req.method,
        "url": req.url,
        "headers": mask_headers(dict(req.headers)),
        "body": req.content,
        "timeout_ms": req.timeout_ms,
        "follow_redirects": req.follow_redirects,
        "verify_ssl": req.verify_ssl,
    }


def _response_dict(resp: httpx.Response, duration_ms: int) -> dict:
    return {
        "status": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text,
        "duration_ms": duration_ms,
    }


def _assertion_dict(a: Any) -> dict:
    return {
        "name": a.name,
        "type": a.type,
        "condition": a.condition,
        "expected": a.expected,
        "actual": a.actual,
        "passed": a.passed,
        "error": a.error,
    }


def _extractor_dict(e: Any) -> dict:
    return {
        "variable": e.variable,
        "value": e.value,
        "scope": e.scope,
        "type": e.type,
        "ok": e.ok,
        "error": e.error,
    }


def _skipped_step(step: dict, reason: Optional[str]) -> dict:
    return {
        "step_number": _step_number(step),
        "name": str(step.get("name") or ""),
        "description": reason or SKIPPED_REASON_DISABLED,
        "status": "skipped",
        "success": False,
        "error": reason,
        "request": None,
        "response": None,
        "assertions": [],
        "extracted": [],
        "script_errors": [],
        "attempts": 0,
        "warnings": [],
    }


def _failed_step(
    step: dict,
    step_number: int,
    error: str,
    *,
    description: str,
    request: Optional[dict] = None,
    response: Optional[dict] = None,
    assertions: Optional[list] = None,
    extracted: Optional[list] = None,
    script_errors: Optional[list] = None,
    attempts: int = 0,
) -> dict:
    return {
        "step_number": step_number,
        "name": str(step.get("name") or ""),
        "description": description,
        "status": "failed",
        "success": False,
        "error": error,
        "request": request,
        "response": response,
        "assertions": assertions or [],
        "extracted": extracted or [],
        # 031（US3）：前后置脚本错误（成功时为空数组，报告可直接渲染）
        "script_errors": script_errors or [],
        # 031（US5）：实际发送次数（含重试）
        "attempts": attempts,
        "warnings": [],
    }


def session_expired_hint(result: Any) -> Optional[str]:
    """031（US10）：复用会话模式下 401/403 失败 → 追加「会话可能已过期」分类提示。

    仅作为**提示**（不改变判定）：复用会话时前序登录态可能过期/被清理，
    与「用例本身断言错」区分开有助于排查。非 401/403 或已通过 → None。
    """
    if str(getattr(result, "status", "")) == "passed":
        return None
    haystack = str(getattr(result, "error", "") or "")
    for step in getattr(result, "steps", None) or []:
        if isinstance(step, dict):
            haystack += " " + str(step.get("error") or "")
    if not haystack.strip():
        return None
    if "401" not in haystack and "403" not in haystack:
        return None
    if "未授权" in haystack or "Unauthorized" in haystack or "Forbidden" in haystack or "401" in haystack:
        return "复用会话模式下返回 401/403，会话可能已过期（请检查登录前置用例或关闭「复用会话」）"
    return None


def _resolve_script_timeout(step: dict) -> int:
    """脚本超时：步骤级 script_timeout_ms 覆盖 → app/config.py::api_script_timeout_ms（默认 2000）。"""
    step_timeout = step.get("script_timeout_ms")
    if isinstance(step_timeout, int) and 200 <= step_timeout <= 10000:
        return step_timeout
    try:
        from app.config import get_settings

        return int(get_settings().api_script_timeout_ms)
    except Exception:  # noqa: BLE001 - core 独立运行（无 app 配置）时用默认
        return 2000


def _response_script_context(resp, duration_ms: int) -> dict:
    """后置脚本的响应上下文（031 US3）：status_code/duration_ms/headers/body/body_text。"""
    body = None
    text = getattr(resp, "text", "") or ""
    if text.strip():
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = None
    return {
        "status_code": getattr(resp, "status_code", 0),
        "duration_ms": duration_ms,
        "headers": {str(k).lower(): v for k, v in (getattr(resp, "headers", {}) or {}).items()},
        "body": body,
        "body_text": text,
    }


def _http_error_label(exc: Exception) -> str:
    """httpx 异常的可读标签（含异常类型名）。"""
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectError):
        return f"连接失败 ({name})"
    if isinstance(exc, httpx.TimeoutException):
        return f"超时 ({name})"
    return f"请求异常 ({name})"


def _default_agent_manager():
    """延迟取全局 AgentManager（避免服务端模块在纯客户端/测试环境硬依赖 agent 包）。"""
    try:
        from agent.manager import agent_manager

        return agent_manager
    except Exception:  # pragma: no cover - 仅在 agent 包不可用时走到
        return None


class _AgentBridgeClient:
    """T050：把 ``httpx.AsyncClient.request()`` 语义代理到客户端 Agent 执行。

    契约（contracts §4）：payload 字段名与 WS ``api_request`` 一致；Agent 侧脱敏由
    客户端负责，服务端只保证传出去的 header 已是渲染后的真实值（报告落库前统一脱敏）。
    返回真实 ``httpx.Response``，让上层解析/断言逻辑零改动复用。
    """

    def __init__(self, manager, agent_name: str, timeout: float = 60.0) -> None:
        self._manager = manager
        self._agent_name = agent_name
        self._timeout = timeout

    async def request(  # noqa: PLR0913 - 与 httpx 的 request 签名对齐
        self,
        *,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        content: Any = None,
        data: Any = None,
        timeout: Optional[float] = None,
        follow_redirects: bool = True,
        verify: bool = True,
        **_ignored: Any,
    ) -> httpx.Response:
        is_form = data is not None
        body_content = data if is_form else (content if content is not None else "")
        payload = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "body": {
                "type": "form" if is_form else "json",
                "content": body_content if isinstance(body_content, str) else json.dumps(body_content, ensure_ascii=False, default=str),
            },
            "timeout_ms": int((timeout or self._timeout) * 1000),
            "follow_redirects": bool(follow_redirects),
            "verify_ssl": bool(verify),
        }
        result = await self._manager.request_api_call(self._agent_name, payload, timeout=self._timeout)
        if not result.get("success"):
            # 统一走连接类异常：上层既有错误分类会记成人读错误，报告不缺证据
            raise httpx.ConnectError(result.get("error") or "客户端执行失败")
        return httpx.Response(
            status_code=int(result.get("status") or 0),
            headers=result.get("headers") or {},
            content=str(result.get("body") or "").encode("utf-8"),
            request=httpx.Request(method, url),
        )

    async def aclose(self) -> None:
        """与 httpx.AsyncClient 的生命周期接口对齐（无连接可关）。"""


def _prepare_multipart(
    req: RenderedRequest, platform_files: Optional[dict[int, str]]
) -> tuple[list, list]:
    """031（US1）：把 multipart 描述解析为 httpx ``files=`` 参数（纯 multipart 通道）。

    实现要点（httpx 0.28 实测）：
      · AsyncClient 不接受同步文件句柄，也不接受 ``data=`` 的 list[tuple] 形式
        （均报 "Attempted to send an sync request with an AsyncClient instance"）；
      · 因此文件内容整读为 **bytes**，普通字段用 ``(key, (None, value))`` 带进 files，
        既绕开 data 限制又保留重复键语义；
      · 平台文件上限 50MB（api_test_file_max_mb），整读内存可接受。
    失败抛 ValueError（步骤失败且不重试）：引用缺失 / 文件不存在 / 超过大小上限。
    """
    from pathlib import Path

    from app.config import get_settings

    limit = max(1, int(get_settings().api_test_file_max_mb)) * 1024 * 1024
    files: list = []
    for item in req.files or []:
        raw_path = str(item.get("path") or "")
        file_id = parse_platform_ref(raw_path)
        resolved = raw_path
        if file_id is not None:
            resolved = str((platform_files or {}).get(file_id) or "")
            if not resolved:
                raise ValueError(
                    f"引用的测试文件不存在（platform://{file_id}），请重新上传并在用例中选择"
                )
        path = Path(resolved)
        if not path.is_file():
            raise ValueError(f"文件不存在: {resolved}")
        content = path.read_bytes()
        if len(content) > limit:
            raise ValueError(
                f"文件超过上限 {limit // (1024 * 1024)}MB（实际 {len(content)} 字节）: {path.name}"
            )
        # content-disposition 用"逻辑文件名"：剥掉平台存储的 uuid 前缀
        # （存储形如 <32hex>_<原名>，见 app/crud/api_file.py::create_test_file）
        display_name = re.sub(r"^[0-9a-f]{32}_", "", path.name) or path.name
        files.append(
            (item["key"], (display_name, content, item.get("content_type") or None))
        )
    for key, value in req.form_data or []:
        files.append((key, (None, value)))
    return files


async def _send_request(
    client: httpx.AsyncClient,
    req: RenderedRequest,
    *,
    files: Optional[list] = None,
    data: Optional[list] = None,
) -> httpx.Response:
    """发送渲染后的请求；form 类型（dict content）走 data=，其余走 content=。

    契约要点：``req.url`` 已含完整 query（request_builder 用 urlencode 编码），
    因此**不能再传 params** —— httpx 会把 params 追加到已有 query 上，
    导致 `?page=2&page=2` 这类重复参数。``req.params`` 仅供展示与报告。
    """
    content = req.content
    if files is None:
        data = None
        if isinstance(content, dict):
            data = content
            content = None
    else:
        # 031（US1）multipart：Content-Type 由 httpx 带 boundary 自动生成
        content = None
    return await client.request(
        method=req.method,
        url=req.url,
        headers=req.headers,
        content=content,
        data=data,
        files=files,
        timeout=req.timeout_ms / 1000,
        follow_redirects=req.follow_redirects,
    )


async def _run_hook_steps(
    items: Any,
    scope: VariableScope,
    warnings: list[str],
    *,
    label: str,
    write_runtime: bool,
) -> None:
    """前置 / 后置动作：设置变量或延时。不执行任意脚本。

    前置写入 case 层，供本次请求渲染使用。后置写入 runtime，供后续步骤使用。
    变量值支持 ``{{var}}``。延时上限 5 秒。
    """
    for item in items or []:
        if not isinstance(item, dict):
            warnings.append(f"{label} 项必须是对象: {item!r}")
            continue
        kind = str(item.get("type") or "")
        if kind == "set_variable":
            key = str(item.get("key") or "").strip()
            if not key:
                warnings.append(f"{label} set_variable 缺少 key")
                continue
            try:
                value = render(item.get("value") or "", scope)
            except Exception as exc:
                warnings.append(f"{label} 变量 {key} 渲染失败: {exc}")
                continue
            if write_runtime:
                scope.set_runtime(key, value)
            else:
                scope.case[key] = value
        elif kind == "delay":
            try:
                ms = int(item.get("ms") or 0)
            except (TypeError, ValueError):
                warnings.append(f"{label} delay.ms 非法: {item.get('ms')!r}")
                continue
            await asyncio.sleep(max(0, min(ms, 5000)))
        else:
            warnings.append(f"未知 {label} 类型: {kind!r}")


async def _run_pre_steps(pre_steps: Any, scope: VariableScope, warnings: list[str]) -> None:
    await _run_hook_steps(pre_steps, scope, warnings, label="前置", write_runtime=False)


async def _run_step(
    client: httpx.AsyncClient,
    step: dict,
    scope: VariableScope,
    step_number: int,
    env_headers: Optional[dict] = None,
    *,
    platform_files: Optional[dict[int, str]] = None,
) -> dict:
    """执行单步，返回冻结字段的步骤结果字典（永不抛异常）。"""
    request_spec = step.get("request") or {}
    method = str(request_spec.get("method") or "GET")
    raw_url = str(request_spec.get("url") or "")
    # 步骤级变量（优先级高于用例/环境，低于 runtime）
    scope.step = variables_to_map(step.get("variables"))

    warnings: list[str] = []
    script_errors: list[dict] = []
    try:
        await _run_pre_steps(step.get("pre"), scope, warnings)
    except Exception as exc:  # 防御：前置动作异常不得炸整步
        warnings.append(f"前置步骤执行异常: {exc}")

    # 031（US3）：前置脚本（渲染前执行；写新变量进入本步骤渲染，失败则不发请求）
    pre_script = str(step.get("pre_script") or "")
    if pre_script.strip():
        from core.script_sandbox import run_script

        pre_out = run_script(
            pre_script,
            {"vars": scope.visible_map()},
            timeout_ms=_resolve_script_timeout(step),
        )
        if not pre_out.ok:
            script_errors.append({"phase": "pre", "message": pre_out.error})
            return _failed_step(
                step,
                step_number,
                f"前置脚本失败: {pre_out.error}",
                description=f"{method} {raw_url} → 前置脚本失败: {pre_out.error}",
                script_errors=script_errors,
            )
        for key, value in pre_out.variables.items():
            scope.step[str(key)] = "" if value is None else str(value)

    try:
        req = build_request(step, scope)
        # 环境公共请求头注入（步骤头覆盖同名）
        req.headers = _merge_headers(env_headers or {}, req.headers)
    except Exception as exc:
        return _failed_step(
            step,
            step_number,
            f"请求构造失败: {exc}",
            description=f"{method} {raw_url} → 请求构造失败: {exc}",
        )

    request_dict = _request_dict(req)
    step_started = time.perf_counter()
    try:
        files_arg = _prepare_multipart(req, platform_files)
    except ValueError as exc:
        return _failed_step(
            step,
            step_number,
            f"multipart 文件不可用: {exc}",
            description=f"{method} {req.url} → multipart 文件不可用: {exc}",
            request=request_dict,
        )
    # 031（US5）：重试策略（默认：网络错误/超时 × 1，间隔 300ms；5xx 与断言不重试）
    retry_cfg = step.get("retry") or None
    if retry_cfg:
        max_retry = int(retry_cfg.get("max") or 0)
        delay_ms = int(retry_cfg.get("delay_ms") or 300)
        retry_on = list(retry_cfg.get("on") or ["timeout", "network"])
    else:
        max_retry, delay_ms, retry_on = 1, 300, ["timeout", "network"]

    attempts = 0
    resp = None
    send_error: Optional[str] = None
    while True:
        attempts += 1
        send_error = None
        retry_kind: Optional[str] = None
        try:
            resp = await _send_request(client, req, files=files_arg or None)
        except httpx.HTTPError as exc:
            resp = None
            retry_kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
            # 保留异常详情（客户端桥接的可读原因藏在异常消息里，丢弃会让报告不可读）
            detail = str(exc).strip()
            send_error = (
                f"{_http_error_label(exc)}: {detail}" if detail else _http_error_label(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：任何发送异常不得冒泡
            resp = None
            retry_kind = None
            send_error = f"请求异常 ({type(exc).__name__}): {exc}"
        else:
            if getattr(resp, "status_code", 0) >= 500:
                retry_kind = "5xx"
        if retry_kind is None or attempts > max_retry or retry_kind not in retry_on:
            break
        await asyncio.sleep(max(0, delay_ms) / 1000.0)

    if resp is None:
        label = send_error or "请求失败"
        return _failed_step(
            step,
            step_number,
            f"{label}（已重试 {attempts - 1} 次）" if attempts > 1 else label,
            description=f"{method} {raw_url} → {label}",
            request=request_dict,
            attempts=attempts,
        )
    duration_ms = _elapsed_ms(step_started)
    sent_headers = getattr(getattr(resp, "request", None), "headers", None)
    if sent_headers:
        request_dict["headers"] = mask_headers(dict(sent_headers))

    response_like = ResponseLike(
        status=resp.status_code,
        headers=dict(resp.headers),
        body_text=resp.text,
        duration_ms=duration_ms,
        # 031（US6）：表达式断言可读当前可见变量（vars 上下文）
        variables=scope.visible_map(),
    )
    assertion_results = run_assertions(step.get("assertions") or [], response_like)
    assertion_dicts = [_assertion_dict(a) for a in assertion_results]
    passed_count = sum(1 for a in assertion_dicts if a["passed"])
    total_count = len(assertion_dicts)

    extracted_dicts: list[dict] = []
    extract_error: Optional[str] = None
    try:
        _, extractor_results = run_extractors(step.get("extractors") or [], response_like)
        for e in extractor_results:
            extracted_dicts.append(_extractor_dict(e))
            if not e.ok:
                # 031（US9）：提取器未生效要显式告警（此前静默，下游「未定义变量」很难回溯）
                warnings.append(
                    f"提取器 {e.variable or '(未命名)'} 未生效: {e.error or '未知原因'}"
                )
            if e.ok:
                if e.scope == "environment":
                    scope.set_env(e.variable, e.value)
                else:
                    scope.set_runtime(e.variable, e.value)
    except ExtractionError as exc:
        extract_error = f"提取失败: {exc}"

    try:
        await _run_hook_steps(step.get("post"), scope, warnings, label="后置", write_runtime=True)
    except Exception as exc:
        warnings.append(f"后置步骤执行异常: {exc}")

    response_dict = _response_dict(resp, duration_ms)
    # 031（US3）：后置脚本（可读响应/提取后变量；覆盖写作用域）
    post_script = str(step.get("post_script") or "")
    if post_script.strip():
        from core.script_sandbox import run_script

        post_out = run_script(
            post_script,
            {"vars": scope.visible_map(), **_response_script_context(resp, duration_ms)},
            timeout_ms=_resolve_script_timeout(step),
        )
        if not post_out.ok:
            script_errors.append({"phase": "post", "message": post_out.error})
            return _failed_step(
                step,
                step_number,
                f"后置脚本失败: {post_out.error}",
                description=f"{method} {req.url} → {resp.status_code} ({duration_ms}ms) → 后置脚本失败",
                request=request_dict,
                response=response_dict,
                assertions=assertion_dicts,
                extracted=extracted_dicts,
                script_errors=script_errors,
                attempts=attempts,
            )
        for key, value in post_out.variables.items():
            scope.set_variable(str(key), value)

    base_desc = (
        f"{method} {req.url} → {resp.status_code} ({duration_ms}ms) | "
        f"断言 {passed_count}/{total_count} 通过"
    )
    if extract_error:
        return _failed_step(
            step, step_number, extract_error,
            description=base_desc, request=request_dict, response=response_dict,
            assertions=assertion_dicts, extracted=extracted_dicts, attempts=attempts,
        )
    failed_assertions = [a for a in assertion_dicts if not a["passed"]]
    if failed_assertions:
        detail = failed_assertions[0]["error"] or "断言失败"
        return _failed_step(
            step, step_number, f"断言失败: {detail}",
            description=base_desc, request=request_dict, response=response_dict,
            assertions=assertion_dicts, extracted=extracted_dicts, attempts=attempts,
        )
    return {
        "step_number": step_number,
        "name": str(step.get("name") or ""),
        "description": base_desc,
        "status": "passed",
        "success": True,
        "error": None,
        "request": request_dict,
        "response": response_dict,
        "assertions": assertion_dicts,
        "extracted": extracted_dicts,
        # 031（US3）：后置脚本成功后为空；失败会走 _failed_step 带具体原因
        "script_errors": [],
        # 031（US5）：实际发送次数（含重试）
        "attempts": attempts,
        "warnings": warnings,
    }


def _final_variables(scope: VariableScope) -> dict[str, str]:
    """执行后的变量快照（低 → 高合并，runtime 最高；含 baseUrl）。"""
    out: dict[str, str] = {}
    for layer in (scope.env, scope.dataset, scope.case, scope.step, scope.runtime):
        out.update(layer)
    if scope.base_url:
        out["baseUrl"] = scope.base_url
    return out


async def run_api_case(
    api_spec: dict,
    *,
    environment: Optional[dict] = None,  # {"id","name","base_url","variables":[...],"headers":[...]}
    case_variables: Optional[list[dict]] = None,  # 用例级变量 [{key,value,secret,enable}]
    dataset_row: Optional[dict] = None,  # 单行绑定（优先于 dataset，保持既有语义）
    dataset: Optional[dict] = None,  # 数据集快照 {"id","columns","rows"} → 按行迭代
    client: Optional[httpx.AsyncClient] = None,  # 注入用（测试传 MockTransport；None 时内部自建）
    execution_mode: Optional[str] = None,        # "server"（默认）或 "client:<agent_name>"（T050）
    agent_manager: Any = None,                   # 客户端执行用的 AgentManager（缺省取全局实例）
    step_limit: Optional[int] = None,
    seed_runtime: Optional[dict] = None,
    platform_files: Optional[dict[int, str]] = None,  # 031：platform://<id> → 服务端绝对路径
) -> ApiRunResult:
    """执行一条接口用例，返回完整结果（永不抛异常）。

    绑定 ``dataset`` 时按行迭代（sequential = 逐行；random/loop 本期按 sequential
    执行并在 notes 说明）。所有迭代平铺进一条 ``ApiRunResult.steps``，每步带
    ``iteration``（从 1 起）；多迭代时步骤描述带 ``[迭代 N]`` 前缀。fail_fast 时
    某迭代出现失败步骤后，该迭代后续步骤 skipped 且不再进入后续迭代。
    每个迭代使用独立作用域（runtime 提取不跨迭代泄漏）。
    """
    started = time.perf_counter()
    try:
        spec = normalize_api_spec(api_spec)
    except Exception as exc:
        return ApiRunResult(
            status="failed",
            error=f"api_spec 非法: {exc}",
            duration_ms=_elapsed_ms(started),
        )

    dataset_mode = str(spec.get("dataset_mode") or "sequential")
    notes = dataset_mode_notes(dataset_mode)
    # 031（US9）：迭代计划（sequential / random / loop × loop_count）
    rows = plan_iterations(
        dataset,
        dataset_row,
        mode=dataset_mode,
        loop_count=int(spec.get("loop_count") or 1),
    )
    multi = len(rows) > 1
    fail_policy = str(spec.get("fail_policy") or "fail_fast")
    steps = spec.get("steps") or []

    own_client = client is None
    verify_ssl = _resolve_verify_ssl(spec)
    if not verify_ssl:
        notes.append("已按用例配置关闭 TLS 证书校验（verify_ssl=false）")

    requested_mode = str(execution_mode or spec.get("execution_mode") or "server").strip()
    if client is None and requested_mode.startswith("client:"):
        manager = agent_manager or _default_agent_manager()
        agent_name = requested_mode.split(":", 1)[1].strip()
        if manager is None or not agent_name:
            notes.append("客户端执行不可用（缺少 AgentManager 或 agent 名）→ 回退服务端执行")
        else:
            client = _AgentBridgeClient(manager, agent_name)
            notes.append(f"执行位置：客户端 Agent「{agent_name}」")
    if client is None:
        client = _create_default_client(verify_ssl=verify_ssl)

    results: list[dict] = []
    rendered_requests: list[dict] = []
    failed = False
    final_scope: Optional[VariableScope] = None
    env_extracted: dict[str, str] = {}
    try:
        for iteration, row in enumerate(rows, start=1):
            if failed and fail_policy == "fail_fast":
                # 031（US9）：后续迭代逐步标记 skipped（报告可见消费），不静默丢弃
                for step in steps:
                    results.append(
                        tag_iteration(
                            _skipped_step(step, SKIPPED_REASON_PREVIOUS_ITERATION),
                            iteration,
                            multi,
                        )
                    )
                continue
            scope = _build_scope(spec, environment, case_variables, row, seed_runtime)
            # 环境公共请求头（渲染后注入每步；步骤头覆盖同名）——每迭代独立渲染
            env_headers = _render_header_items((environment or {}).get("headers"), scope)
            iteration_failed = False
            for index, step in enumerate(steps):
                if step_limit is not None and index >= step_limit:
                    step_result = _skipped_step(step, SKIPPED_REASON_STEP_LIMIT)
                elif step.get("enable") is False:
                    step_result = _skipped_step(step, None)
                elif iteration_failed and fail_policy == "fail_fast":
                    step_result = _skipped_step(step, SKIPPED_REASON_PREVIOUS_FAILURE)
                else:
                    step_result = await _run_step(
                        client, step, scope, index + 1, env_headers,
                        platform_files=platform_files,
                    )
                if step_result["status"] == "failed":
                    iteration_failed = True
                    failed = True
                results.append(tag_iteration(step_result, iteration, multi))
                if step_result.get("request") is not None:
                    rendered_requests.append(step_result["request"])
            final_scope = scope
            env_extracted.update(scope.env_extracted)
    finally:
        if own_client:
            await client.aclose()

    return ApiRunResult(
        status="failed" if failed else "passed",
        steps=results,
        error=None,
        duration_ms=_elapsed_ms(started),
        rendered_requests=rendered_requests,
        variables=_final_variables(final_scope) if final_scope is not None else {},
        environment_variables=env_extracted,
        notes=notes,
        runtime=dict(final_scope.runtime) if final_scope is not None else dict(seed_runtime or {}),
    )


__all__ = [
    "ApiRunResult",
    "run_api_case",
    "SKIPPED_REASON_PREVIOUS_FAILURE",
    "SKIPPED_REASON_PREVIOUS_ITERATION",
    "SKIPPED_REASON_STEP_LIMIT",
]