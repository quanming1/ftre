@echo off
REM ftre - start backend + frontend

echo [ftre] Starting gateway (port 18790)...
set PYTHONPATH=E:\ftre\src
start "ftre-gateway" cmd /c "python -m ftre.main gateway"

echo [ftre] Waiting for backend...
:wait_backend
timeout /t 1 /nobreak >nul
powershell -Command "try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 18790); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
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
