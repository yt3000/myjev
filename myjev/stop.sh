#!/usr/bin/env bash
# MyJev 服务停止脚本。用法: ./stop.sh [端口，默认 8090]
cd "$(dirname "$0")"
PORT="${1:-8090}"
if command -v powershell.exe >/dev/null 2>&1; then          # Windows / Git Bash
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$PWD/stop.ps1" 2>/dev/null || echo "$PWD/stop.ps1")" "$PORT"
elif command -v lsof >/dev/null 2>&1; then                  # Linux / macOS
  pids=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN || true)
  if [ -z "$pids" ]; then echo "[stop] port $PORT is free"; exit 0; fi
  for p in $pids; do echo "[stop] killing pid=$p"; kill -9 "$p"; done
  echo "[stop] done"
else
  echo "[stop] 无法识别环境（无 powershell/lsof）"; exit 1
fi
