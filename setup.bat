@echo off
setlocal DisableDelayedExpansion
if not defined HOMEWORK_LAUNCH_PYTHON (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0data\launch_with_python.ps1" -Launcher setup
    exit /b
)
chcp 936 >nul 2>&1
set "PY=%HOMEWORK_LAUNCH_PYTHON%"
if not exist "%PY%" (
    echo [ERROR] Install Python 3.10 or newer.
    echo         https://www.python.org/downloads/windows/
    echo         Expected: "%PY%"
    pause
    exit /b 1
)
echo Installing Playwright and Pillow for this project...
"%PY%" -m pip install playwright Pillow
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check the network and retry setup.bat.
    pause
    exit /b 1
)
echo Opening the console. Follow Management - Setup Guide to finish configuration.
call "%~dp0console.bat"
exit /b %ERRORLEVEL%
