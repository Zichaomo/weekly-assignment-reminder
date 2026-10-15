@echo off
rem ============================================================================
rem  Homework reminder console - single-window launcher
rem
rem  Behavior:
rem    - ALWAYS open the UI as an Edge app window (no address bar).
rem      If the service is already listening, reuse it; otherwise start it hidden.
rem    - Only when Edge cannot be found at all does it fall back to the
rem      default browser, and it says so explicitly.
rem    - Closing the browser window stops the service and frees the port.
rem
rem  IMPORTANT: this file is ASCII-only ON PURPOSE.
rem    cmd parses batch files with the console OEM code page (GBK on zh-CN),
rem    so UTF-8 Chinese text inside a .bat can be mis-read as commands and
rem    print errors like:
rem        '...' is not recognized as an internal or external command
rem    Chinese user-facing messages are printed by Python (launcher_msg.py)
rem    instead, which handles UTF-8 correctly. Do not add Chinese to this file.
rem ============================================================================
setlocal
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SRV=%~dp0data\server.py"
set "VBS=%~dp0data\run_hidden.vbs"
set "MSG=%~dp0data\launcher_msg.py"
set "FOCUS=%~dp0data\focus_console.py"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" goto no_python
if not exist "%VBS%" goto no_vbs

rem ---- 1) is a service already listening?
set "PORTBUSY="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8277); '1' } catch { '0' } finally { $c.Close() }" 2^>nul`) do set "PORTBUSY=%%i"

rem ---- 2) ALREADY RUNNING: bring the existing window to the front.
rem         Do NOT open a second window (that is what the user asked for).
if "%PORTBUSY%"=="1" goto reuse

rem ---- 3) locate Edge (only needed when we must open a window)
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE goto no_edge

rem ---- 4) open the Edge app window and capture its PID
set "EDGEPID="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$p = Start-Process -FilePath '%EDGE%' -ArgumentList '--app=%URL%','--no-first-run','--no-default-browser-check' -PassThru; $p.Id" 2^>nul`) do set "EDGEPID=%%i"
if "%EDGEPID%"=="" goto no_pid

rem ---- 5) start the hidden service, watching the browser process
cscript //nologo "%VBS%" "%PY%" "%EDGEPID%" "%SRV%"
"%PY%" "%MSG%" ok
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem  Service already running: focus the existing window instead of opening a new one.
:reuse
"%PY%" "%FOCUS%" >nul 2>&1
if errorlevel 1 goto reuse_nofocus
"%PY%" "%MSG%" reuse
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

:reuse_nofocus
"%PY%" "%MSG%" reuse_nofocus
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

rem  Edge not found: the only case where the default browser is used.
:no_edge
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
"%PY%" "%MSG%" no_edge
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem  Edge exists but we could not get its PID: do NOT open a second browser.
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
