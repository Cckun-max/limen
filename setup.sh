#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  market_python="${MARKET_PYTHON:-python3}"
  "$market_python" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+；可用 MARKET_PYTHON 指定路径"'
  "$market_python" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
(cd frontend && npm ci && npm run build)
echo '完成。运行 ./start.sh，然后打开 http://127.0.0.1:8765'
