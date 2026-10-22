@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.10+ is required. Install Python and try again.
  pause
  exit /b 1
)
python -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
echo Ready. Run: run.bat check
pause
