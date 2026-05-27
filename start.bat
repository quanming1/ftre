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

REM 解析 pnpm 绝对路径：where 找不到时再去常见的 npm global / pnpm 安装位置兜底
set "PNPM="
for /f "delims=" %%i in ('where pnpm 2^>nul') do (
    if not defined PNPM set "PNPM=%%i"
)
if not defined PNPM (
    if exist "%APPDATA%\npm\pnpm.cmd" set "PNPM=%APPDATA%\npm\pnpm.cmd"
)
if not defined PNPM (
    if exist "%LOCALAPPDATA%\pnpm\pnpm.cmd" set "PNPM=%LOCALAPPDATA%\pnpm\pnpm.cmd"
)
if not defined PNPM (
    echo [ftre] ERROR: 找不到 pnpm。请确认安装并把 npm global 或 pnpm 目录加进 PATH。
    pause
    exit /b 1
)
echo        pnpm: %PNPM%
REM 把 pnpm 所在目录注入子窗口 PATH。pnpm dev 内部用 concurrently 派生
REM 多个子进程跑 `pnpm --filter ...`，它们只看 PATH 不看父进程的 %PNPM%
REM 变量，所以必须把目录加到 PATH 里
for %%P in ("%PNPM%") do set "PNPM_DIR=%%~dpP"
REM /k 让前端闪退也保留窗口看错误
start "ftre-frontend" cmd /k "set "PATH=%PNPM_DIR%;%PATH%" && "%PNPM%" dev"

echo [ftre] All started
echo   Backend: ws://127.0.0.1:18790/
echo   Frontend: pnpm dev (Electron)
echo.
echo Press any key to stop all...
pause >nul

taskkill /fi "WINDOWTITLE eq ftre-gateway" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq ftre-frontend" /f >nul 2>&1
