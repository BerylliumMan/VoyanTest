#!/bin/bash
docker exec voyantest grep -RhoE 'nl_goal|browser_use|legacy_hybrid|compiled_script|智能 OTA|唯一' /app/app/static/assets 2>/dev/null | sort | uniq -c
