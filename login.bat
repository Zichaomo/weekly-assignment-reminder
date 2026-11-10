@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not exist "%PY%" (
    echo [ERROR] Python 3.14 interpreter not found: %PY%
    pause
    exit /b 1
)
cd /d "%~dp0data"
"%PY%" login.py --auto
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
