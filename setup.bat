@echo off
setlocal
chcp 936 >nul 2>&1
set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not exist "%PY%" (
    echo [ERROR] Install Python 3.14 for the current user with the default path.
    echo         https://www.python.org/downloads/windows/
    echo         Expected: %PY%
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
