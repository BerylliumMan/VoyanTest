"""
VoyanTest Agent Client - backward-compatible wrapper.
AgentClient has been moved to agent/client_core.py.
CLI entry point has been moved to agent/cli_entry.py.
"""
from agent.client_core import AgentClient
from agent.cli_entry import main

__all__ = ["AgentClient", "main"]

if __name__ == "__main__":
    main()
