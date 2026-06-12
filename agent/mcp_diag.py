"""MCP 启动诊断 — 测试 @playwright/mcp 能否正常通信。"""

import asyncio
import json
import sys


async def main():
    print(f"Python: {sys.version}")
    print(f"测试 MCP 子进程启动...\n")

    proc = await asyncio.create_subprocess_exec(
        'npx', '-y', '@playwright/mcp@latest', '--browser=chromium', '--isolated',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors='replace').rstrip()
            if text:
                print(f"  [stderr] {text}")

    async def read_stdout():
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None
            text = line.decode(errors='replace').strip()
            if text:
                print(f"  [stdout] {text}")
                return json.loads(text)
            continue

    asyncio.create_task(read_stderr())

    # 1. 发送 initialize 请求
    req = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-diag", "version": "1.0"},
        },
    }
    data = json.dumps(req) + "\n"
    proc.stdin.write(data.encode())
    await proc.stdin.drain()
    print(f"\n> initialize (protocolVersion=2024-11-05)")

    resp = await asyncio.wait_for(read_stdout(), timeout=30)
    if resp:
        print(f"\n✓ MCP initialize 成功!")
        print(f"  协议版本: {resp.get('result', {}).get('protocolVersion', '?')}")
        print(f"  服务端信息: {resp.get('result', {}).get('serverInfo', {})}")

        # 2. 发送 initialized 通知
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write((json.dumps(notif) + "\n").encode())
        await proc.stdin.drain()
        print(f"\n> notifications/initialized ✓")

        # 3. 测试 browser_snapshot
        req2 = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "browser_snapshot", "arguments": {}},
        }
        proc.stdin.write((json.dumps(req2) + "\n").encode())
        await proc.stdin.drain()
        print(f"\n> tools/call browser_snapshot (等待浏览器启动, 最长 60s)")

        resp2 = await asyncio.wait_for(read_stdout(), timeout=60)
        if resp2:
            result = resp2.get("result", {})
            content = result.get("content", [])
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            is_err = result.get("isError", False)
            if is_err:
                print(f"\n✗ snapshot 返回错误: {text[:300]}")
            else:
                print(f"\n✓ snapshot 成功! 页面内容 ({len(text)} chars)")
                print(text[:500])
        else:
            print(f"\n✗ browser_snapshot 无响应")
    else:
        print(f"\n✗ initialize 无响应")

    proc.kill()
    await proc.wait()
    print("\n诊断完成")


if __name__ == "__main__":
    asyncio.run(main())
