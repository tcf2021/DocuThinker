#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! docker info >/dev/null 2>&1; then echo '请先打开 Docker Desktop，并在 Settings → Resources → WSL Integration 启用 Ubuntu。'; exit 1; fi
docker compose version >/dev/null
if [[ ! -s settings.json || ! -s .env ]]; then python3 scripts/configure.py; fi
docker compose up -d --build --wait --wait-timeout 150
port=$(sed -n 's/^AGENT_PORT=//p' .env)
printf '工作台已启动：http://localhost:%s\n第一次使用：docker compose exec agent python -m app.users cf\n' "${port:-8020}"
