@echo off
chcp 65001 >nul
set "ROOT=%~dp0"

echo ============================================
echo   灵墨 Lingmo 服务启动器
echo ============================================
echo.

echo [1/2] 启动后端 (8000)...
start "lingmo-backend" /min cmd /c "cd /d %ROOT%backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo [2/2] 启动前端 (5173)...
start "lingmo-frontend" /min cmd /c "cd /d %ROOT%frontend && npm run dev"

echo.
echo 启动完成，稍等几秒后访问：
echo   后端  http://127.0.0.1:8000
echo   前端  http://localhost:5173/
echo.
echo 提示：关闭任务栏里最小化的两个窗口即可停止服务。
timeout /t 8
