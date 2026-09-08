@echo off
set "ROOT=%~dp0"

echo ============================================
echo   Lingmo Writer - Service Launcher
echo ============================================
echo.

echo [1/2] Starting backend (port 8000)...
start "lingmo-backend" /min cmd /c "cd /d %ROOT%backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo [2/2] Starting frontend (port 5173)...
start "lingmo-frontend" /min cmd /c "cd /d %ROOT%frontend && npm run dev"

echo.
echo Services starting, wait a few seconds:
echo   Backend  http://127.0.0.1:8000
echo   Frontend http://localhost:5173/
echo.
echo Close the two minimized windows to stop.
timeout /t 8
