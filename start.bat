@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist .venv (
  echo [初始化] 正在创建 Python 虚拟环境...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo [安装依赖] 首次运行需要联网...
pip install -r requirements.txt --quiet

echo.
echo ================================================
echo  -80C 冰箱菌种管理台已启动
echo  本机访问:  http://127.0.0.1:8000
echo  手机访问:  http://<本机局域网IP>:8000
echo  (IP 可用 ipconfig 查看 IPv4 地址)
echo ================================================
echo.

python -m uvicorn app:app --host 0.0.0.0 --port 8000
