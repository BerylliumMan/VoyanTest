"""MCP environment setup — pre-download @playwright/mcp and Chromium browser."""

import shlex
import subprocess
import sys


def run(cmd: str, desc: str) -> bool:
    print(f"\n{'='*60}")
    print(f"[{desc}]")
    print(f"Running: {cmd}")
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
            print(f"✓ {desc} succeeded")
            return True
        else:
            print(f"✗ {desc} failed (code={result.returncode})")
            if result.stderr:
                print(result.stderr[:500])
            return False
    except FileNotFoundError:
        print(f"✗ Command not found: {cmd.split()[0]}")
        print("  Please install Node.js: https://nodejs.org")
        return False
    except subprocess.TimeoutExpired:
        print(f"✗ {desc} timed out")
        return False


def main():
    print("VoyanTest Agent — MCP Environment Setup")
    print(f"Python: {sys.version}")

    # Check Node.js
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        print("✓ Node.js is installed")
    except FileNotFoundError:
        print("✗ Node.js is not installed. Download: https://nodejs.org")
        sys.exit(1)

    # 1. Pre-download @playwright/mcp
    run(
        "npx -y @playwright/mcp@latest --version",
        "Download @playwright/mcp",
    )

    # 2. Install chrome-for-testing browser
    run(
        "npx @playwright/mcp install-browser chrome-for-testing",
        "Install chrome-for-testing browser",
    )

    print("\n" + "="*60)
    print("Environment setup complete!")
    print("You can now run the agent:")
    print('  python agent/client.py --server http://SERVER:8002 --name "Agent1"')
    print("="*60)


if __name__ == "__main__":
    main()
