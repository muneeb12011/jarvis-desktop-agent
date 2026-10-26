@echo off
rem Builds the one-click installer: frontend\release\Jarvis-Setup-<version>.exe  (log: build-installer.log)
cd /d "%~dp0frontend"
rem The running HUD locks Electron's files, so close it first (start-jarvis.bat brings it back).
powershell -NoProfile -Command "Get-Process electron -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*screen-agent*' } | Stop-Process -Force" >nul 2>&1
timeout /t 2 /nobreak >nul
echo Building the Jarvis installer... this takes a few minutes.
echo === build %date% %time% === > "%~dp0build-installer.log"
call npm install --no-audit --no-fund >> "%~dp0build-installer.log" 2>&1
echo NPM_EXIT %errorlevel% >> "%~dp0build-installer.log"
call npm run dist >> "%~dp0build-installer.log" 2>&1
echo DIST_EXIT %errorlevel% >> "%~dp0build-installer.log"
dir /b release\*.exe >> "%~dp0build-installer.log" 2>&1
echo Done. The installer is in frontend\release
timeout /t 5 >nul
