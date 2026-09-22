#!/bin/bash
set -e
docker exec voyantest sh -c 'ls -lt /app/reports | head -15'
echo '---'
docker exec voyantest sh -c 'ls -la /app/reports/agent_147_* 2>/dev/null; ls -la /app/reports/agent_147_*/screenshots 2>/dev/null || echo no-147-dir'
echo '--- logs ---'
docker logs --since 5m voyantest 2>&1 | grep -E 'run=147|run #147|AgentRun #147|nav_fail|failure_final|navigating to https://voyantest' | tail -40
