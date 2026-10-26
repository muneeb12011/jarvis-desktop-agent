@echo off
rem Demo for Jarvis heads-up: an error appears in this window after a few seconds.
echo Running a script that is missing a package...
timeout /t 10 /nobreak >nul
"%~dp0backend\venv\Scripts\python.exe" -c "import requests_toolbelt_missing_demo"
echo.
pause
