@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Emoji Tool Launcher

if not exist "venv\Scripts\python.exe" (
    echo.
    echo [!] 还没建虚拟环境，先在这个目录执行：
    echo     py -3 -m venv venv
    echo     venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem 结束占用端口 5178 的旧进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":5178"') do taskkill /F /PID %%a >nul 2>nul

set "PYTHONIOENCODING=utf-8"
"venv\Scripts\python.exe" "app.py" --open
pause
