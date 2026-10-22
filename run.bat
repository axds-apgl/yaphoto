@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" yaphoto.py --help
  pause
  exit /b 0
)
".venv\Scripts\python.exe" yaphoto.py %*
exit /b %errorlevel%
