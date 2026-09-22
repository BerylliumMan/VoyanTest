#!/usr/bin/env python3
"""一次 batch-run-client：Sauce Demo 成功 + 坏域名失败，同一批次保留报告。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
USER = os.environ.get("VOYAN_USER", "admin")
PASS = os.environ.get("VOYAN_PASS", "Abc@12345")
AGENT = os.environ.get("VOYAN_AGENT", "lzl")

SUCCESS_URL = "https://www.saucedemo.com/"
FAIL_URL = "https://voyantest-ota-fail-never-resolves.invalid/"

jar = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


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
    with opener.open(r, timeout=180) as resp:
        raw = resp.read()
        return json.loads(raw.decode() or "null") if raw else None


def main() -> int:
    print(f"BASE={BASE} agent={AGENT}")
    req("POST", "/api/auth/login", {"username": USER, "password": PASS})
    agents = req("GET", "/api/agents") or []
    online = [a for a in agents if a.get("name") == AGENT and a.get("status") == "online"]
    if not online:
        print(f"[FAIL] agent {AGENT} offline")
        return 1
    print(f"[PASS] agent online — {AGENT}")

    proj = req(
        "POST",
        "/api/projects/",
        {
            "name": f"OTA-BATCH-MIX-{int(time.time())}",
            "description": "one batch: success+failure",
            "base_url": SUCCESS_URL,
        },
    )
    pid = proj["id"]
    print(f"[PASS] project {pid}")

    ok_case = req(
        "POST",
        "/api/testcases/",
        {
            "project_id": pid,
            "name": "batch OK — Sauce Demo login",
            "case_kind": "ui",
            "steps": [
                {"step_order": 1, "description": f"打开 {SUCCESS_URL}"},
                {"step_order": 2, "description": "在 Username 输入框输入 standard_user"},
                {"step_order": 3, "description": "在 Password 输入框输入 secret_sauce"},
                {"step_order": 4, "description": "点击 Login 按钮"},
                {"step_order": 5, "description": "等待页面出现文本 Products"},
            ],
        },
    )
    fail_case = req(
        "POST",
        "/api/testcases/",
        {
            "project_id": pid,
            "name": "batch FAIL — bad host",
            "case_kind": "ui",
            "steps": [
                {"step_order": 1, "description": f"打开 {FAIL_URL}"},
                {
                    "step_order": 2,
                    "description": "等待页面出现文本 VOYAN_OTA_FAIL_MARKER_NEVER_EXISTS_999",
                },
            ],
        },
    )
    ok_id, fail_id = ok_case["id"], fail_case["id"]
    print(f"[PASS] cases ok={ok_id} fail={fail_id}")

    # 同一批次：成功 + 失败
    payload = {
        "case_ids": [ok_id, fail_id],
        "agent_name": AGENT,
        "init_policy": "before_each",
    }
    resp = req("POST", "/api/testcases/batch-run-client", payload)
    print(f"[PASS] batch-run-client → {resp}")
    batch_id = resp.get("batch_id")
    if not batch_id:
        print("[FAIL] no batch_id in response")
        return 1

    # 轮询批次直到终态
    deadline = time.time() + 600
    i = 0
    detail = None
    while time.time() < deadline:
        detail = req("GET", f"/api/reports/batches/{batch_id}")
        st = (detail or {}).get("status")
        print(
            f"  batch#{batch_id} poll#{i} status={st} "
            f"passed={detail.get('passed')} failed={detail.get('failed')} "
            f"total={detail.get('total_cases')}"
        )
        if st in ("passed", "failed", "partial", "error", "cancelled", "completed"):
            # 等两用例都有终态
            runs = detail.get("runs") or []
            if len(runs) >= 2 and all(
                (r.get("status") or "") not in ("running", "pending", "")
                for r in runs
            ):
                break
            if st in ("passed", "failed", "partial") and len(runs) >= 2:
                break
        time.sleep(5)
        i += 1

    detail = req("GET", f"/api/reports/batches/{batch_id}")
    print("--- batch detail ---")
    print(
        f"batch#{batch_id} status={detail.get('status')} "
        f"name={detail.get('name')!r} project={detail.get('project_id')} "
        f"passed={detail.get('passed')} failed={detail.get('failed')}"
    )
    ok_shot = fail_shot = False
    for r in detail.get("runs") or []:
        print(
            f"  run#{r.get('run_id')} case={r.get('case_name')!r} "
            f"status={r.get('status')} steps={len(r.get('steps') or [])}"
        )
        for s in r.get("steps") or []:
            sp = s.get("screenshot_path")
            msg = (s.get("description") or "")[:90].replace("\n", " ")
            print(f"    step#{s.get('step_number')} ok={s.get('success')} shot={sp} | {msg}")
            if sp and not s.get("success"):
                fail_shot = True
            if "Sauce" in (r.get("case_name") or "") and r.get("status") == "passed":
                ok_shot = True  # success case may not show shots by design

    # 成功用例是否固化
    compiled = req("GET", f"/api/testcases/{ok_id}/compiled-script") or {}
    has_script = bool(compiled.get("has_script")) and bool(
        (compiled.get("compiled_script") or "").strip()
    )
    print(
        f"success solidified={has_script} bytes={len(compiled.get('compiled_script') or '')}"
    )
    compiled_fail = req("GET", f"/api/testcases/{fail_id}/compiled-script") or {}
    fail_has = bool(compiled_fail.get("has_script")) and bool(
        (compiled_fail.get("compiled_script") or "").strip()
    )
    print(f"failure solidified={fail_has} (expect False)")

    runs = detail.get("runs") or []
    statuses = {r.get("case_name"): r.get("status") for r in runs}
    print("--- verdict ---")
    print(f"UI: 执行报告 → batch #{batch_id}（project_id={pid}）")
    print(f"statuses={statuses}")

    passed_ok = any(
        r.get("status") == "passed" and "Sauce" in (r.get("case_name") or "")
        for r in runs
    )
    failed_ok = any(
        r.get("status") == "failed" and "FAIL" in (r.get("case_name") or "")
        for r in runs
    )
    if not passed_ok:
        print("[FAIL] success case not passed")
        return 1
    if not failed_ok:
        print("[FAIL] failure case not failed")
        return 1
    if not has_script:
        print("[FAIL] success case not solidified")
        return 1
    if fail_has:
        print("[FAIL] failure case should not solidify")
        return 1
    if not fail_shot:
        print("[WARN] failure case has no screenshot on error step")
    print("[PASS] mixed batch: success passed+solidified, failure failed+no script")
    print(f"KEEP project_id={pid} batch_id={batch_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("[FAIL]", type(exc).__name__, exc)
        raise
