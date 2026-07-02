"""MCP 环境安装脚本 — 预下载 @playwright/mcp 和 Chromium 浏览器。"""

import shlex
import subprocess
import sys


def run(cmd: str, desc: str) -> bool:
    print(f"\n{'='*60}")
    print(f"[{desc}]")
    print(f"运行: {cmd}")
    print('='*60)
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if result.returncode == 0:
            out = (result.stdout or result.stderr or "").strip()
            if out:
                print(out[:500])
            print(f"✓ {desc} 成功")
            return True
        else:
            print(f"✗ {desc} 失败 (code={result.returncode})")
            if result.stderr:
                print(result.stderr[:500])
            return False
    except FileNotFoundError:
        print(f"✗ 未找到命令: {cmd.split()[0]}")
        print("  请先安装 Node.js: https://nodejs.org")
        return False
    except subprocess.TimeoutExpired:
        print(f"✗ {desc} 超时")
        return False


def main():
    print("VoyanTest Agent — MCP 环境安装")
    print(f"Python: {sys.version}")

    # 检查 Node.js
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        print("✓ Node.js 已安装")
    except FileNotFoundError:
        print("✗ Node.js 未安装，请先下载安装: https://nodejs.org")
        sys.exit(1)

    # 1. 预下载 @playwright/mcp
    run(
        "npx -y @playwright/mcp@latest --version",
        "下载 @playwright/mcp",
    )

    # 2. 安装 chrome-for-testing 浏览器（@playwright/mcp 依赖）
    run(
        "npx @playwright/mcp install-browser chrome-for-testing",
        "安装 chrome-for-testing 浏览器",
    )

    print("\n" + "="*60)
    print("环境安装完成！")
    print("现在可以运行 agent:")
    print('  python agent/client.py --server http://SERVER:8002 --name "Agent1"')
    print("="*60)


if __name__ == "__main__":
    main()
