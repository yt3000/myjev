#!/usr/bin/env bash
# MyJev 启动脚本（双进程拓扑，docs/04）。用法: ./start.sh <命令> [端口]
cd "$(dirname "$0")"
PY=".venv/Scripts/python.exe"; [[ -f "$PY" ]] || PY=".venv/bin/python"
export PYTHONIOENCODING=utf-8 PYTHONPATH=.
ensure() { [[ -f "$PY" ]] || { python -m venv .venv && "$PY" -m pip install -r requirements.txt; }; }

case "${1:-help}" in
  setup) ensure; "$PY" -m pip install -r requirements.txt ;;
  serve-all) ensure; "$PY" -m myjev.serve serve-all --daemon ;;
  serve|serve-admin) ensure; "$PY" -m myjev.serve --role admin ;;
  serve-runtime) ensure; "$PY" -m myjev.serve --role runtime ;;
  stop-all) ensure; "$PY" -m myjev.serve stop-all || ./stop.sh "${2:-8090}" ;;
  status) ensure; "$PY" -m myjev.serve status ;;
  demo) ensure; "$PY" -m myjev.run_demo ;;
  test) ensure; "$PY" -m unittest discover -s tests && "$PY" -m unittest discover -s scripts/tests ;;
  check) ensure; "$PY" --version; "$PY" -c "import importlib;[print(' ',m,getattr(importlib.import_module(m),'__version__','?')) for m in ['numpy','scipy','sklearn','lightgbm','fastapi','uvicorn']]" ;;
  *) cat <<'EOF'
MyJev 启动脚本（A 服务后台 :8091 / B 管理后台 :8090 / C 测试App 打开 myjev/app/index.html）
  ./start.sh setup            安装环境
  ./start.sh serve-all        守护启动两进程（推荐）
  ./start.sh stop-all         停止（pid+端口双路，幂等）
  ./start.sh status           查看状态
  ./start.sh serve-admin      前台跑管理后台（调试）
  ./start.sh serve-runtime    前台跑服务后台（调试）
  ./start.sh demo|test|check  旧单机演示 / 全测试 / 依赖自检
EOF
esac
