@echo off
title CaspianX Well Intelligence (local, no upload size limit)
cd /d "%~dp0"
set "PY="
for %%v in (3.13 3.12 3.11) do (
  if not defined PY ( py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v" )
)
if not defined PY ( python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1 && set "PY=python" )
if not defined PY ( echo Python 3.11+ tapilmadi: https://www.python.org/downloads/ & pause & exit /b 1 )
if not exist ".venv_web\Scripts\python.exe" %PY% -m venv .venv_web
echo Paketler qurulur (ilk defe bir nece deqiqe)...
".venv_web\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r web_app\requirements.txt
if errorlevel 1 ( echo Paket qurulmasi alinmadi. & pause & exit /b 1 )
start "" cmd /c "timeout /t 10 >nul & start http://localhost:8000"
".venv_web\Scripts\python.exe" -m uvicorn web_app.main:app --host 127.0.0.1 --port 8000
pause
