@echo off
rem ============================================================================
rem  Homework reminder console - single-window launcher
rem
rem  Behavior:
rem    - 1st run  : open an Edge app window, start the service hidden, exit.
rem    - later run: the service is already listening -> bring the EXISTING
rem                 window to the front. Never open a second window.
rem    - Closing the browser window stops the service and frees the port.
rem
rem  This file is ASCII-only ON PURPOSE, and it prints ASCII-only messages.
rem    Reason: cmd parses batch files with the console OEM code page, and the
rem    console code page can disagree with what Python reports
rem    (GetConsoleOutputCP returned 65001 while chcp said 936 on this machine).
rem    Any non-ASCII text here or in the child process output rendered as
rem    mojibake in a real console window. ASCII cannot be mangled, so the
rem    launcher speaks English only. The web UI is fully Chinese.
rem ============================================================================
setlocal
rem Keep the console code page at the system ANSI page (936 on zh-CN) so that
rem helper tools behave consistently. No non-ASCII is printed either way.
chcp 936 >nul 2>&1
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SRV=%~dp0data\server.py"
set "VBS=%~dp0data\run_hidden.vbs"
set "FOCUS=%~dp0data\focus_console.py"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" goto no_python
if not exist "%VBS%" goto no_vbs

rem ---- 1) is a service already listening?
set "PORTBUSY="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8277); '1' } catch { '0' } finally { $c.Close() }" 2^>nul`) do set "PORTBUSY=%%i"

rem ---- 2) ALREADY RUNNING: bring the existing window to the front (no new window)
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
echo.
echo   Console started: %URL%
echo   Closing the browser window stops the service. This window will close.
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem  Service already running: focus the existing window instead of opening a new one.
:reuse
"%PY%" "%FOCUS%" >nul 2>&1
if errorlevel 1 goto reuse_nofocus
echo.
echo   Console is already running - brought the existing window to the front.
echo   (no new window was opened)
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

:reuse_nofocus
echo.
echo   Console service is already running, but its window was not found.
echo   Please open manually: %URL%
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

rem  Edge not found: the only case where the default browser is used.
:no_edge
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
echo.
echo   Edge not found - opening with the default browser instead: %URL%
echo   The service will keep running; stop it from the page footer.
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem  Edge exists but we could not get its PID: do NOT open a second browser.
:no_pid
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
echo.
echo   Could not get the Edge process id, so no new window was opened.
echo   The service is running - open manually: %URL%
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
