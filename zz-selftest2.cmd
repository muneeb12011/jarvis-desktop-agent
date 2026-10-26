@echo off
cd /d "%~dp0backend"
venv\Scripts\python.exe -m app.selftest whisper brain audio > logs\selftest2.log 2>&1
