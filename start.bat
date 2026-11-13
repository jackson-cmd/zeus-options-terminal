@echo off
REM ZEUS Terminal - one-click start (Windows)
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
set ZEUS_PYTHON=%cd%\.venv\Scripts\python.exe
set "OPEN_PORT=%PORT%"
if "%OPEN_PORT%"=="" for /f "usebackq delims=" %%p in (`.venv\Scripts\python -c "import sys; sys.path.insert(0, 'zeus'); import zeus_config as c; print(c.get('port') or 8765)"`) do set "OPEN_PORT=%%p"
if "%OPEN_PORT%"=="" set "OPEN_PORT=8765"
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process 'http://localhost:%OPEN_PORT%/'"
.venv\Scripts\python zeus\options_analyzer\optionstrat_server.py
