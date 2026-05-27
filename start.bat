@echo off
REM ftre - start backend + frontend

REM 解析 Python 绝对路径：用 py 启动器问当前默认 Python 在哪
REM 这样不依赖 PATH，新装/切换 Python 版本也不用改这个脚本
for /f "delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%i"
if not defined PY (
    REM py 不在也兜底找一次 python
    where python >nul 2>&1 && set "PY=python" || (
        echo [ftre] ERROR: 找不到 Python（py 启动器和 python 命令都不可用）。
        echo        请安装 Python 3.11+ 并勾选 "Add Python to PATH"，
        echo        或者用 `py --version` 验证 Windows Python Launcher 可用。
        pause
        exit /b 1
    )
)

echo [ftre] Starting gateway (port 18790)...
echo        Python: %PY%
set PYTHONPATH=E:\ftre\src
REM /k 让 gateway 闪退时窗口保留，能看到错误信息
start "ftre-gateway" cmd /k ""%PY%" -m ftre.main gateway"

echo [ftre] Waiting for backend...
:wait_backend
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "if (Test-NetConnection 127.0.0.1 -Port 18790 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 goto wait_backend
echo [ftre] Backend ready

echo [ftre] Starting frontend...
cd /d E:\binn\ftre-desktop
start "ftre-frontend" cmd /c "pnpm dev"

echo [ftre] All started
echo   Backend: ws://127.0.0.1:18790/
echo   Frontend: pnpm dev (Electron)
echo.
echo Press any key to stop all...
pause >nul

taskkill /fi "WINDOWTITLE eq ftre-gateway" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq ftre-frontend" /f >nul 2>&1
