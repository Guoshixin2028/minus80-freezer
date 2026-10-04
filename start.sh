#!/usr/bin/env bash
# -80C 冰箱菌种管理台 · Linux/macOS 启动脚本
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[初始化] 正在创建 Python 虚拟环境..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt --quiet

# 云平台一般通过 PORT 环境变量指定监听端口，本地默认 8000
PORT="${PORT:-8000}"
echo "========================================"
echo " -80C 冰箱菌种管理台已启动，监听端口: $PORT"
echo " 本机访问: http://127.0.0.1:$PORT"
echo "========================================"

exec python -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
