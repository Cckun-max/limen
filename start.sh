#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || [ ! -f frontend/dist/index.html ]; then
  echo '请先执行 ./setup.sh 安装依赖并构建页面。'
  exit 1
fi
exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port "${MARKET_PORT:-8765}" --timeout-graceful-shutdown 5
