#!/bin/bash
# Start VoyanTest Agent Client persistently
export PYTHONPATH=/home/lzl/git-repos/uitest-work
cd /home/lzl/git-repos/uitest-work
exec python3 agent/cli_entry.py --server ws://localhost:8002 --headless --name e2e-agent --username admin --password admin123
