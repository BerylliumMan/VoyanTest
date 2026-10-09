"""US6 场景级公共配置（pre_script 每用例执行一次）——033-api-test-maturity。

TDD：先于实现编写（预期红：run_api_case 尚无 scenario_pre_script）。
"""

from __future__ import annotations

import asyncio

import httpx


def _spec(url: str, steps: int = 1) -> dict:
    return {
        "schema_version": 1, "variables": [], "fail_policy": "fail_fast",
        "steps": [
            {
                "order": i + 1, "name": f"步骤{i + 1}", "enable": True,
                "request": {"method": "GET", "url": url, "headers": [], "query": [],
                             "body": {"type": "none", "content": ""}, "auth": {"type": "none"}},
                "assertions": [{"type": "status_code", "condition": "equals", "expected": 200, "enable": True}],
                "extractors": [],
            }
            for i in range(steps)
        ],
    }


def _run(spec, transport, **kw):
    from core.api_runner.runner import run_api_case

    async def _go():
        client = httpx.AsyncClient(transport=transport, timeout=15)
        try:
            return await run_api_case(spec, environment={}, client=client, **kw)
        finally:
            await client.aclose()

    return asyncio.run(_go())


def test_scenario_pre_script_variables_visible_to_step(echo_server):
    spec = _spec("http://testserver/ok?stamp={{stamp}}")
    result = _run(spec, echo_server.transport, scenario_pre_script='stamp = "s6"')
    assert result.status == "passed", result.error
    req = echo_server.requests[0]
    assert "stamp=s6" in str(req.url)


def test_scenario_pre_script_runs_per_iteration(echo_server):
    spec = _spec("http://testserver/ok?tag={{tag}}")
    dataset = {"columns": ["n"], "rows": [["1"], ["2"]]}
    result = _run(
        spec,
        echo_server.transport,
        scenario_pre_script='tag = "v" + str(vars.get("n"))',
        dataset=dataset,
    )
    assert result.status == "passed", result.error
    urls = [str(r.url) for r in echo_server.requests]
    assert len(urls) == 2
    assert any("tag=v1" in u for u in urls) and any("tag=v2" in u for u in urls)


def test_scenario_pre_script_failure_is_readable_and_skips_rest(echo_server):
    spec = _spec("http://testserver/ok", steps=2)
    result = _run(spec, echo_server.transport, scenario_pre_script="x = 1 / 0")
    assert result.status == "failed"
    first = result.steps[0]
    assert "场景前置脚本失败" in (first.get("error") or "")
    assert first.get("status") == "failed"
    assert result.steps[1].get("status") == "skipped"
    assert not echo_server.requests  # 未发出任何请求


def test_no_scenario_pre_script_unchanged(echo_server):
    spec = _spec("http://testserver/ok")
    result = _run(spec, echo_server.transport)
    assert result.status == "passed"
