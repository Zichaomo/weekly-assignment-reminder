@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0data"
set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe

rem Start the local web console server (it exits by itself if one is already running)
start "HomeworkConsole" /min "%PY%" "%~dp0data\server.py"

rem Give the server a moment, then open the UI (ping works even without console stdin)
ping -n 3 127.0.0.1 >nul

rem Prefer an Edge app window (no address bar); fall back to the default browser
set EDGE1=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
set EDGE2=C:\Program Files\Microsoft\Edge\Application\msedge.exe
if exist "%EDGE1%" (start "" "%EDGE1%" --app=http://127.0.0.1:8277 & goto :eof)
if exist "%EDGE2%" (start "" "%EDGE2%" --app=http://127.0.0.1:8277 & goto :eof)
start "" http://127.0.0.1:8277
