@echo off
rem Checks screen reading, audio devices, the speech model and the AI key. Report: backend\logs\selftest.log
cd /d "%~dp0backend"
if not exist logs mkdir logs
echo Checking Jarvis... speak and play something with sound during the audio check.
venv\Scripts\python.exe -m app.selftest > logs\selftest.log 2>&1
type logs\selftest.log
pause
