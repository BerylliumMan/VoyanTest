"""CLI entry point for VoyanTest Agent."""
import sys
import argparse
import logging
import asyncio
from agent.client_core import AgentClient

logger = logging.getLogger("agent.client")


def main():
    import argparse

    # Check if running as packaged exe with no args → interactive mode
    is_frozen = getattr(sys, 'frozen', False)

    if is_frozen and len(sys.argv) == 1:
        # Packaged with no args → interactive mode
        print("=" * 50)
        print("  VoyanTest Agent Client")
        print("=" * 50)
        print()
        server = input("Server URL (e.g. ws://192.168.1.100:8002): ").strip()
        if not server:
            server = "ws://localhost:8002"
        if not server.startswith("ws://") and not server.startswith("wss://"):
            server = "ws://" + server
        name_input = input("Agent name (leave empty for auto-generated): ").strip()
        name = name_input or None
        headless_input = input("Use headless mode? (y/N): ").strip().lower()
        headless = headless_input in ("y", "yes")
        username_input = input("Username (leave empty to skip auth): ").strip()
        password_input = input("Password (leave empty to skip auth): ").strip()
        print()
        print(f"Server: {server}")
        print(f"Name: {name or '(auto-generated)'}")
        print(f"Headless: {'yes' if headless else 'no'}")
        if username_input:
            print(f"User: {username_input}")
        print("-" * 50)
        print("Connecting...")
        print()
        args = argparse.Namespace(
            server=server,
            name=name,
            headless=headless,
            username=username_input or None,
            password=password_input or None,
        )
    else:
        parser = argparse.ArgumentParser(description="VoyanTest Agent Client")
        parser.add_argument("--server", required=not is_frozen, help="Server URL (e.g. ws://192.168.1.100:8002)")
        parser.add_argument("--name", help="Agent name (default: auto-generated)")
        parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
        parser.add_argument("--username", help="Username for server authentication")
        parser.add_argument("--password", help="Password for server authentication")
        args = parser.parse_args()

        # Unpackaged and no server arg → interactive mode
        if not args.server:
            print("=" * 50)
            print("  VoyanTest Agent Client")
            print("=" * 50)
            print()
            server = input("Server URL (e.g. ws://192.168.1.100:8002): ").strip()
            if not server:
                server = "ws://localhost:8002"
            if not server.startswith("ws://") and not server.startswith("wss://"):
                server = "ws://" + server
            args.server = server
            name_input = input("Agent name (leave empty for auto-generated): ").strip()
            args.name = name_input or None
            headless_input = input("Use headless mode? (y/N): ").strip().lower()
            args.headless = headless_input in ("y", "yes")
            username_input = input("Username (leave empty to skip auth): ").strip()
            args.username = username_input or None
            password_input = input("Password (leave empty to skip auth): ").strip()
            args.password = password_input or None
            print()
            print(f"Server: {args.server}")
            print(f"Name: {args.name or '(auto-generated)'}")
            print(f"Headless: {'yes' if args.headless else 'no'}")
            if args.username:
                print(f"User: {args.username}")
            print("-" * 50)
            print("Connecting...")
            print()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    agent = AgentClient(args.server, args.name, headless=args.headless,
                        username=args.username, password=args.password)
    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")


if __name__ == "__main__":
    main()
