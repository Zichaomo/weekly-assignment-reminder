@echo off
setlocal DisableDelayedExpansion
if not defined HOMEWORK_LAUNCH_PYTHON (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0data\launch_with_python.ps1" -Launcher login
    exit /b
)
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set "PY=%HOMEWORK_LAUNCH_PYTHON%"
if not exist "%PY%" (
    echo [ERROR] Python interpreter not found: "%PY%"
    pause
    exit /b 1
)
cd /d "%~dp0data"
"%PY%" login.py --auto
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
