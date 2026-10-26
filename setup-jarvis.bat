@echo off
rem One-time setup: installs everything Jarvis needs. Output goes to setup.log.
cd /d "%~dp0"
echo === Jarvis setup %date% %time% === > setup.log
echo Setting up Jarvis... this takes a few minutes. Details in setup.log
echo [1/5] Python packages
backend\venv\Scripts\python.exe -m pip install --upgrade pip >> setup.log 2>&1
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt >> setup.log 2>&1
echo PIP_MAIN_EXIT %errorlevel% >> setup.log
echo [2/5] Voice recognition (Whisper)
backend\venv\Scripts\python.exe -m pip install -r backend\requirements-voice.txt >> setup.log 2>&1
echo PIP_VOICE_EXIT %errorlevel% >> setup.log
echo [3/5] Downloading the Whisper model
backend\venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8'); print('WHISPER_OK')" >> setup.log 2>&1
echo [4/5] HUD packages
pushd frontend
call npm install >> ..\setup.log 2>&1
echo NPM_EXIT %errorlevel% >> ..\setup.log
call npm run build >> ..\setup.log 2>&1
echo BUILD_EXIT %errorlevel% >> ..\setup.log
popd
echo [5/5] Checking
backend\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); from app import tools; print('TOOLS_OK', len(tools.load_all()))" >> setup.log 2>&1
backend\venv\Scripts\python.exe -c "import pywinauto, psutil, edge_tts, pycaw; print('WIN_DEPS_OK')" >> setup.log 2>&1
where ollama >> setup.log 2>&1 && ollama list >> setup.log 2>&1
echo SETUP_DONE >> setup.log
echo Done. You can close this window.
timeout /t 5 >nul
