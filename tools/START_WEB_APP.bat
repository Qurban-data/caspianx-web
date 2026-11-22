@echo off
title CaspianX Well Intelligence (web app)
cd /d "%~dp0"
echo [1/4] Preparing folder CaspianX_v5 ...
if not exist "CaspianX_v5\web_app\main.py" powershell -NoProfile -Command "Expand-Archive -Force 'CaspianX_v5_repo.zip' 'CaspianX_v5'"
cd /d "%~dp0CaspianX_v5"
echo [2/4] Looking for Python 3.11+ ...
set "PY="
for %%v in (3.13 3.12 3.11) do (
  if not defined PY ( py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v" )
)
if not defined PY ( python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1 && set "PY=python" )
if not defined PY (
  echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ and run this file again.
  pause
  exit /b 1
)
echo Using %PY%
if not exist ".venv_web\Scripts\python.exe" %PY% -m venv .venv_web
echo [3/4] Installing packages (first run takes a few minutes) ...
".venv_web\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r web_app\requirements.txt
if errorlevel 1 (
  echo Package installation failed. See the messages above.
  pause
  exit /b 1
)
for /f %%p in ('".venv_web\Scripts\python.exe" -c "import socket;print(next(p for p in range(8000,8020) if socket.socket().connect_ex(('127.0.0.1',p))!=0))"') do set "PORT=%%p"
if not defined PORT set "PORT=8000"
echo [4/4] Starting the app on http://localhost:%PORT%  (close this window to stop it)
start "" cmd /c "timeout /t 12 >nul & start http://localhost:%PORT%"
".venv_web\Scripts\python.exe" -m uvicorn web_app.main:app --host 127.0.0.1 --port %PORT%
pause
