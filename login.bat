@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0data"
"%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" login.py --auto
echo.
pause
