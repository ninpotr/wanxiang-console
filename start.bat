@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   万相3.0 视频创作台
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 没找到 python 命令。请先安装 Python 3.10 或更高版本。
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] Python 版本太低，请使用 3.10 或更高版本。
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, requests, multipart, dashscope" >nul 2>&1
if errorlevel 1 (
    echo 首次运行，正在安装依赖……
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败。
        pause
        exit /b 1
    )
)

echo 正在启动，稍后浏览器会自动打开 http://127.0.0.1:8000
echo 关闭这个窗口即可停止服务。
echo.

start "" http://127.0.0.1:8000
python app.py

pause
