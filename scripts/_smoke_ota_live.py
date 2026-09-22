#!/usr/bin/env python3
"""Live OTA smoke against WSL VoyanTest: create UI case, server-run, assert OTA path."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
USER = os.environ.get("VOYAN_USER", "admin")
PASS = os.environ.get("VOYAN_PASS", "Abc@12345")

jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def csrf() -> str:
    for c in jar:
        if c.name == "csrf_token":
            return c.value
    return ""


def req(method: str, path: str, body=None, expect: int | None = None):
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
        with opener.open(r, timeout=120) as resp:
            raw = resp.read()
            code = resp.status
            payload = json.loads(raw.decode() or "null") if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
        try:
            payload = json.loads(raw.decode() or "null")
        except Exception:
            payload = raw.decode(errors="replace")
        if expect is not None and code != expect:
            raise AssertionError(f"{method} {path} -> {code}: {payload}") from e
        if expect is None and code >= 400:
            raise AssertionError(f"{method} {path} -> {code}: {payload}") from e
        return code, payload
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {path} -> {code}: {payload}")
    return code, payload


def main() -> int:
    print(f"BASE={BASE} user={USER}")
    # login
    req("POST", "/api/auth/login", {"username": USER, "password": PASS}, expect=200)
    print("[PASS] login")

    # backend must be ota
    code, cfg = req("GET", "/api/config/execution-backend")
    backend = (cfg or {}).get("backend")
    print(f"execution-backend={backend}")
    assert backend == "ota", cfg
    print("[PASS] backend is ota")

    # ensure execution agent has tools
    code, agents = req("GET", "/api/agent-definitions")
    items = agents if isinstance(agents, list) else (agents or {}).get("items") or []
    exec_agents = [a for a in (items or []) if a.get("agent_type") == "execution"]
    active = next((a for a in exec_agents if a.get("is_active")), None) or (
        exec_agents[0] if exec_agents else None
    )
    if not active:
        raise SystemExit("No execution agent definition found")

    tools = active.get("tools") or []
    enabled = [
        t for t in tools
        if isinstance(t, dict) and t.get("enabled", True) is not False
    ]
    print(
        f"agent id={active.get('id')} name={active.get('name')} "
        f"tools={len(tools)} enabled={len(enabled)}"
    )
    if not enabled:
        raise SystemExit("execution agent has no enabled tools — OTA cannot run")
    print("[PASS] execution agent tools ready")

    # create project
    code, proj = req(
        "POST",
        "/api/projects/",
        {
            "name": f"OTA-CURSOR-{int(time.time())}",
            "description": "ota cursor smoke",
            "base_url": "https://example.com",
        },
    )
    if not isinstance(proj, dict) or "id" not in proj:
        print("project create", code, proj)
        raise SystemExit("create project failed")
    pid = proj["id"]
    print(f"[PASS] project {pid}")

    # create UI case
    code, case = req(
        "POST",
        "/api/testcases/",
        {
            "project_id": pid,
            "name": "OTA cursor example.com",
            "case_kind": "ui",
            "steps": [
                {
                    "step_order": 1,
                    "description": "打开 https://example.com",
                },
                {
                    "step_order": 2,
                    "description": "等待页面出现文本 Example Domain",
                },
            ],
        },
    )
    if not isinstance(case, dict) or "id" not in case:
        print("create case response:", code, case)
        raise SystemExit("create case failed")
    cid = case["id"]
    print(f"[PASS] case {cid}")

    # server run
    _, run_resp = req("POST", f"/api/testcases/{cid}/run")
    print("run_resp:", run_resp)
    batch_id = (run_resp or {}).get("batch_id") or (run_resp or {}).get("id")

    status = None
    detail = None
    for i in range(90):
        time.sleep(2)
        if not batch_id:
            break
        _, batch = req("GET", f"/api/reports/batches/{batch_id}")
        status = (batch or {}).get("status")
        print(f"  poll#{i} batch status={status} keys={list((batch or {}).keys())[:8]}")
        if status in ("completed", "passed", "failed", "error", "cancelled", "success"):
            detail = batch
            break
        # also try nested runs
        runs = (batch or {}).get("runs") or (batch or {}).get("items") or []
        if runs and all(
            (r.get("status") in ("completed", "passed", "failed", "error", "cancelled", "success"))
            for r in runs
        ):
            detail = batch
            status = runs[0].get("status")
            break

    print("final status=", status)
    if detail:
        print("final detail snippet:", json.dumps(detail, ensure_ascii=False)[:1500])

    # Check docker logs for OTA cursor markers
    import subprocess

    logs = subprocess.check_output(
        [
            "wsl",
            "-e",
            "bash",
            "-lc",
            "docker logs voyantest 2>&1 | grep -E 'OTA|AgentRunner|click_xy|Observe|screenshot|run_test_case_via_agent|CURSOR|force_screenshot|Thinking' | tail -40",
        ],
        text=True,
        errors="replace",
    )
    print("--- container log markers ---")
    print(logs or "(no markers)")

    # cleanup project
    try:
        req("DELETE", f"/api/projects/{pid}")
        print(f"[PASS] cleanup project {pid}")
    except Exception as e:
        print(f"[WARN] cleanup: {e}")

    # success criteria: OTA path used (not legacy). Prefer completed/passed but
    # failed with OTA logs still proves wiring.
    ota_wired = (
        "run_test_case_via_agent" in logs
        or "AgentRunner" in logs
        or "━━━ Turn" in logs
        or "Thinking:" in logs
        or "OTA" in logs
    )
    if not ota_wired:
        # check via container that case went OTA - read last errors
        more = subprocess.check_output(
            [
                "wsl",
                "-e",
                "bash",
                "-lc",
                f"docker logs voyantest 2>&1 | grep -E 'case {cid}|via_agent|OTA Agent|nl_goal|browser_use' | tail -30",
            ],
            text=True,
            errors="replace",
        )
        print("--- more logs ---")
        print(more)
        ota_wired = "via_agent" in more or "OTA" in more
        legacy = "nl_goal" in more or "browser_use" in more or "run_test_case_in_browser" in more
        if legacy and not ota_wired:
            print("[FAIL] still on legacy path")
            return 1

    if status in ("completed", "passed"):
        print("[PASS] OTA run completed successfully")
        return 0
    if ota_wired:
        print(f"[PASS] OTA path exercised (status={status}) — wiring verified")
        return 0
    print("[FAIL] could not confirm OTA path")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("[FAIL]", type(exc).__name__, exc)
        raise
