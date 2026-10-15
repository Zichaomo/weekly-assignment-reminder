@echo off
rem ============================================================================
rem  Homework reminder console - single-window launcher
rem
rem  Behavior:
rem    - If the service is already listening on 8277, just open the UI and exit.
rem    - Otherwise: start an Edge app window, capture its PID, then start
rem      server.py hidden (run_hidden.vbs) and let it watch that process.
rem    - Closing the browser window stops the service and frees the port.
rem
rem  IMPORTANT: this file is ASCII-only ON PURPOSE.
rem    cmd parses batch files with the console OEM code page (GBK on zh-CN),
rem    so UTF-8 Chinese text inside a .bat can be mis-read as commands and
rem    print errors like:
rem        '...' is not recognized as an internal or external command
rem    Chinese user-facing messages are printed by Python (launcher_msg.py) instead,
rem    which handles UTF-8 correctly. Do not add Chinese text to this file.
rem ============================================================================
setlocal
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SRV=%~dp0data\server.py"
set "VBS=%~dp0data\run_hidden.vbs"
set "MSG=%~dp0data\launcher_msg.py"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" goto no_python
if not exist "%VBS%" goto no_vbs

rem ---- 1) reuse an already running service (never start a 2nd instance)
set "PORTBUSY="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8277); '1' } catch { '0' } finally { $c.Close() }" 2^>nul`) do set "PORTBUSY=%%i"
if "%PORTBUSY%"=="1" goto reuse

rem ---- 2) locate Edge
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"

rem ---- 3) start Edge app window and capture its PID
set "EDGEPID="
if not defined EDGE goto no_edge
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$p = Start-Process -FilePath '%EDGE%' -ArgumentList '--app=%URL%','--no-first-run','--no-default-browser-check' -PassThru; $p.Id" 2^>nul`) do set "EDGEPID=%%i"
if "%EDGEPID%"=="" goto no_pid

rem ---- 4) start the hidden service, watching the browser process
cscript //nologo "%VBS%" "%PY%" "%EDGEPID%" "%SRV%"
"%PY%" "%MSG%" ok
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:reuse
"%PY%" "%MSG%" reuse
start "" "%URL%"
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

:no_edge
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
"%PY%" "%MSG%" no_edge
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:no_pid
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
"%PY%" "%MSG%" no_pid
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:no_python
echo.
echo   [ERROR] Python interpreter not found:
echo           %PY%
echo   Edit the PY variable at the top of console.bat
pause
exit /b 1

:no_vbs
echo.
echo   [ERROR] Missing launcher: %VBS%
pause
exit /b 1
