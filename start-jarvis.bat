@echo off
rem Starts Jarvis (or restarts it if it's already running). Double-click to run.
rem Logs: backend\logs\console.log (backend) and backend\logs\hud.log (HUD).
setlocal
set ROOT=%~dp0

if not exist "%ROOT%backend\venv\Scripts\python.exe" (
  echo Backend not installed yet. Double-click setup-jarvis.bat first.
  pause
  exit /b 1
)
if not exist "%ROOT%backend\logs" mkdir "%ROOT%backend\logs"

rem Stop a previous Jarvis (backend on port 8000 and this project's HUD) so this acts as a restart.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:8000 .*LISTENING"') do taskkill /PID %%p /F >nul 2>&1
powershell -NoProfile -Command "Get-Process electron -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*screen-agent*' } | Stop-Process -Force" >nul 2>&1

start "Jarvis backend" /min cmd /c "cd /d "%ROOT%backend" && venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log --log-level warning > logs\console.log 2>&1"

rem give the backend a moment to load Whisper
timeout /t 4 /nobreak >nul

cd /d "%ROOT%frontend"
call npm start > "%ROOT%backend\logs\hud.log" 2>&1
