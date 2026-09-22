#!/usr/bin/env python3
"""Client Agent OTA smoke on a complex page: success→compiled_script, failure→no script.

Success target: Sauce Demo (login form + inventory) — fill/click/assert, not example.com.
Failure target: unreachable host — must fail before solidify.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
USER = os.environ.get("VOYAN_USER", "admin")
PASS = os.environ.get("VOYAN_PASS", "Abc@12345")
AGENT = os.environ.get("VOYAN_AGENT", "lzl")
# 默认保留项目/报告便于在「执行报告」查看；设 VOYAN_CLEANUP=1 才删除
CLEANUP = os.environ.get("VOYAN_CLEANUP", "0").strip() in ("1", "true", "yes")

# 比 example.com 更接近真实业务：登录表单 + 跳转商品列表
SUCCESS_URL = "https://www.saucedemo.com/"
SUCCESS_STEPS = [
    {"step_order": 1, "description": f"打开 {SUCCESS_URL}"},
    {
        "step_order": 2,
        "description": "在 Username 输入框输入 standard_user",
    },
    {
        "step_order": 3,
        "description": "在 Password 输入框输入 secret_sauce",
    },
    {"step_order": 4, "description": "点击 Login 按钮"},
    {
        "step_order": 5,
        "description": "等待页面出现文本 Products",
    },
]

jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def csrf() -> str:
    for c in jar:
        if c.name == "csrf_token":
            return c.value
    return ""


def req(method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if method not in ("GET", "HEAD") and csrf():
        headers["X-CSRF-Token"] = csrf()
    r = urllib.request.Request(
        BASE.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with opener.open(r, timeout=180) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode() or "null") if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw or "null")
        except Exception:
            payload = raw
        raise AssertionError(f"{method} {path} -> {e.code}: {payload}") from e


def poll_agent_run(run_id: int, timeout_s: int = 600) -> dict:
    deadline = time.time() + timeout_s
    last = None
    i = 0
    while time.time() < deadline:
        _, last = req("GET", f"/api/agent-runs/{run_id}")
        st = (last or {}).get("status")
        print(f"  agent_run#{run_id} poll#{i} status={st}")
        if st in ("completed", "passed", "failed", "error", "cancelled"):
            return last
        time.sleep(5)
        i += 1
    raise TimeoutError(f"agent_run {run_id} still {(last or {}).get('status')}")


def get_compiled(case_id: int) -> dict:
    _, data = req("GET", f"/api/testcases/{case_id}/compiled-script")
    return data or {}


def wait_compiled(case_id: int, timeout_s: int = 120) -> dict:
    """Status 可能在固化 commit 前就变成 completed — 轮询等到脚本落库。"""
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = get_compiled(case_id)
        if last.get("has_script") and (last.get("compiled_script") or "").strip():
            return last
        time.sleep(3)
    return last


def main() -> int:
    print(f"BASE={BASE} agent={AGENT}")
    print(f"SUCCESS_URL={SUCCESS_URL} steps={len(SUCCESS_STEPS)}")
    req("POST", "/api/auth/login", {"username": USER, "password": PASS})
    rec("login", True)

    agents = req("GET", "/api/agents")[1]
    online = [a for a in agents if a.get("name") == AGENT and a.get("status") == "online"]
    rec("agent online", bool(online), AGENT)
    if not online:
        return 1

    _, proj = req(
        "POST",
        "/api/projects/",
        {
            "name": f"OTA-CLIENT-COMPLEX-{int(time.time())}",
            "description": "client ota saucedemo success+fail",
            "base_url": SUCCESS_URL,
        },
    )
    pid = proj["id"]
    rec("create project", True, str(pid))

    # ── SUCCESS: Sauce Demo 登录进商品页 ──
    _, ok_case = req(
        "POST",
        "/api/testcases/",
        {
            "project_id": pid,
            "name": "client OTA success Sauce Demo login",
            "case_kind": "ui",
            "steps": SUCCESS_STEPS,
        },
    )
    ok_id = ok_case["id"]
    rec("create success case", True, str(ok_id))

    q = urllib.parse.urlencode({"agent_name": AGENT})
    _, run_resp = req("POST", f"/api/testcases/{ok_id}/run-client?{q}")
    ar_id = run_resp.get("agent_run_id")
    rec("start success run-client", ar_id is not None, str(run_resp))
    ar = poll_agent_run(int(ar_id), timeout_s=600)
    ok_status = ar.get("status")
    rec(
        "success OTA finished",
        ok_status in ("completed", "passed"),
        f"status={ok_status} error={(ar.get('error') or '')[:200]}",
    )

    compiled = wait_compiled(ok_id, timeout_s=120)
    has_script = bool(compiled.get("has_script")) and bool(
        (compiled.get("compiled_script") or "").strip()
    )
    script_len = len(compiled.get("compiled_script") or "")
    rec(
        "success case solidified compiled_script",
        has_script,
        f"bytes={script_len} hash={compiled.get('compiled_script_hash')}",
    )
    if has_script:
        snippet = (compiled.get("compiled_script") or "")[:280].replace("\n", " ")
        print(f"  script snippet: {snippet}")

    # ── FAILURE: 不可达 URL → 初始导航失败，不得固化 ──
    _, fail_case = req(
        "POST",
        "/api/testcases/",
        {
            "project_id": pid,
            "name": "client OTA failure bad-host",
            "case_kind": "ui",
            "steps": [
                {
                    "step_order": 1,
                    "description": (
                        "打开 https://voyantest-ota-fail-never-resolves.invalid/"
                    ),
                },
                {
                    "step_order": 2,
                    "description": "等待页面出现文本 VOYAN_OTA_FAIL_MARKER_NEVER_EXISTS_999",
                },
            ],
        },
    )
    fail_id = fail_case["id"]
    rec("create failure case", True, str(fail_id))

    _, run_resp2 = req("POST", f"/api/testcases/{fail_id}/run-client?{q}")
    ar_id2 = run_resp2.get("agent_run_id")
    rec("start failure run-client", ar_id2 is not None, str(run_resp2))
    ar2 = poll_agent_run(int(ar_id2), timeout_s=180)
    fail_status = ar2.get("status")
    rec(
        "failure OTA finished as failed/error",
        fail_status in ("failed", "error"),
        f"status={fail_status} error={(ar2.get('error') or '')[:200]}",
    )

    time.sleep(3)
    compiled_fail = get_compiled(fail_id)
    fail_has = bool(compiled_fail.get("has_script")) and bool(
        (compiled_fail.get("compiled_script") or "").strip()
    )
    rec(
        "failure case NOT solidified",
        not fail_has,
        f"has_script={compiled_fail.get('has_script')}",
    )

    import subprocess

    logs = subprocess.check_output(
        [
            "wsl",
            "-e",
            "bash",
            "-lc",
            "docker logs voyantest 2>&1 | grep -E "
            "'Bridge: synthesized|skip synthesis|premature done|导航失败|"
            f"case={ok_id}|case={fail_id}|saucedemo|Products' | tail -50",
        ],
        text=True,
        errors="replace",
    )
    print("--- container markers ---")
    print(logs or "(none)")
    rec(
        "bridge synthesize log for success",
        f"case={ok_id}" in logs and "synthesized compiled_script" in logs,
        "see markers above",
    )

    # 查出刚写入的批次，方便在「执行报告」打开
    try:
        batches = req("GET", f"/api/reports/batches?project_id={pid}&page=1&size=10")[1]
        items = (batches or {}).get("items") or []
        print("--- report batches (keep these) ---")
        for it in items:
            print(
                f"  batch#{it.get('id')} status={it.get('status')} "
                f"name={it.get('name')!r} triggered_by={it.get('triggered_by')} "
                f"passed={it.get('passed')} failed={it.get('failed')}"
            )
        print(
            f"UI: 执行报告 → 打开上方 batch；"
            f"或 Agent Runs → #{ar_id} / #{ar_id2}；"
            f"project_id={pid} case_success={ok_id} case_fail={fail_id}"
        )
    except Exception as e:
        print(f"(batch lookup failed: {e})")

    if CLEANUP:
        try:
            req("DELETE", f"/api/projects/{pid}")
            rec("cleanup project", True, str(pid))
        except Exception as e:
            rec("cleanup project", False, str(e))
    else:
        rec("keep project for UI report", True, f"project_id={pid}")

    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n=== {len(results) - failed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("[FAIL]", type(exc).__name__, exc)
        raise
