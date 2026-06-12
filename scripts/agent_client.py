#!/usr/bin/env python3
"""
Client agent script to run on local machines for executing test cases.
"""
import asyncio
import sys
import os

# Add project root to Python path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from agent.client import ClientAgent

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="UI Test Agent")
    parser.add_argument("--server", required=True, help="Server URL (e.g., http://localhost:8000)")
    parser.add_argument("--name", help="Agent name")
    parser.add_argument("--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)")
    
    args = parser.parse_args()
    
    # Configure logging
    import logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create and start agent
    agent = ClientAgent(args.server, args.name)
    
    try:
        await agent.start()
    except KeyboardInterrupt:
        print("Agent stopped by user")

if __name__ == "__main__":
    asyncio.run(main())