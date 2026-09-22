#!/bin/bash
echo '=== container frontend snippets ==='
docker exec voyantest sh -c 'grep -RhoE "nl_goal|browser_use|legacy_hybrid|智能 OTA|唯一|Select\.Option" /app/frontend/dist/assets 2>/dev/null | sort | uniq -c | head -40'
echo '=== config API ==='
docker exec voyantest sh -c 'python -c "from app.runtime_config import execution_backend_config, BackendName; print(execution_backend_config.model_dump()); print(BackendName)"'
echo '=== has ExecutionBackend in image? ==='
docker exec voyantest sh -c 'ls /app/frontend/dist/assets 2>/dev/null | head -5; find /app -name "ExecutionBackend*" 2>/dev/null | head'
