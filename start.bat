@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "BACKEND_DIR=%ROOT%backend"
set "FRONTEND_DIR=%ROOT%frontend"
set "BACKEND_PY=%BACKEND_DIR%\.venv\Scripts\python.exe"
set "APP_URL=http://127.0.0.1:5173/"
set "API_URL=http://127.0.0.1:8000/docs"

title Lingmo Writer Launcher
echo ============================================
echo   Lingmo Writer - One-click Launcher
echo ============================================
echo.

if not exist "%BACKEND_PY%" (
  echo [ERROR] Backend virtual environment not found:
  echo         %BACKEND_PY%
  echo         Create it and install backend requirements first.
  goto :failed
)

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm.cmd was not found. Install Node.js and reopen this launcher.
  goto :failed
)

if not exist "%FRONTEND_DIR%\node_modules" (
  echo [ERROR] Frontend dependencies are missing:
  echo         %FRONTEND_DIR%\node_modules
  echo         Run npm install in the frontend directory first.
  goto :failed
)

if not exist "%BACKEND_DIR%\.env" (
  echo [WARN] backend\.env was not found. The app will use default settings.
  echo        LLM features require a configured API key.
  echo.
)

if /i "%~1"=="--check" (
  echo [OK] Required runtime files are available.
  exit /b 0
)

call :port_in_use 8000
if errorlevel 1 (
  echo [1/2] Backend already appears to be running on port 8000.
) else (
  echo [1/2] Starting backend on port 8000...
  start "Lingmo Backend - close to stop" /D "%BACKEND_DIR%" "%ComSpec%" /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
)

call :port_in_use 5173
if errorlevel 1 (
  echo [2/2] Frontend already appears to be running on port 5173.
) else (
  echo [2/2] Starting frontend on port 5173...
  start "Lingmo Frontend - close to stop" /D "%FRONTEND_DIR%" "%ComSpec%" /k "npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort"
)

echo.
echo Waiting for services to become ready...
set /a ATTEMPTS=0

:wait_for_services
call :url_ready "%API_URL%"
if errorlevel 1 goto :wait_again
call :url_ready "%APP_URL%"
if errorlevel 1 goto :wait_again
goto :ready

:wait_again
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 30 goto :timeout
timeout /t 1 /nobreak >nul
goto :wait_for_services

:ready
echo [OK] Backend and frontend are ready.
echo      Backend: %API_URL%
echo      Frontend: %APP_URL%
echo.
echo Opening Lingmo Writer in your default browser...
start "" "%APP_URL%"
echo Close the Backend and Frontend windows to stop the services.
timeout /t 3 /nobreak >nul
exit /b 0

:timeout
echo.
echo [WARN] Services did not both become ready within 30 seconds.
echo        Check the Backend and Frontend windows for error messages.
echo        Backend: %API_URL%
echo        Frontend: %APP_URL%
goto :failed

:port_in_use
powershell.exe -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort %~1 -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
exit /b %errorlevel%

:url_ready
powershell.exe -NoProfile -Command "try { $response = Invoke-WebRequest -UseBasicParsing -Uri '%~1' -TimeoutSec 1; if ($response.StatusCode -lt 500) { exit 0 } } catch {}; exit 1"
exit /b %errorlevel%

:failed
echo.
echo Press any key to close this launcher.
pause >nul
exit /b 1
