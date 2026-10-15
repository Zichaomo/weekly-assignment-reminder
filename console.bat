@echo off
rem ============================================================================
rem  Homework reminder console - single-window launcher
rem
rem  Behavior:
rem    - 1st run  : start the service, WAIT until it answers HTTP, then open
rem                 the Edge app window. Never before: opening the browser first
rem                 made Edge show ERR_CONNECTION_REFUSED and the user had to
rem                 refresh by hand.
rem    - later run: if a real console window exists -> bring it to the front.
rem                 If the port is busy but no usable window exists (the window
rem                 was just closed and the service has not exited yet) -> open
rem                 a NEW window. This command must always end with a usable
rem                 console, never with "please open manually".
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
set "READY=%~dp0data\wait_ready.py"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" goto no_python
if not exist "%VBS%" goto no_vbs

rem ---- is another instance already listening on 8277?
rem  wait_ready.py --probe looks at LISTEN state, not at "can I connect":
rem  a closed socket left in TIME_WAIT also accepts connect and used to make a
rem  dead service look alive.
"%PY%" "%READY%" --probe >nul 2>&1
if not errorlevel 1 goto reuse

rem ===========================================================================
rem  Clean start: service first, browser last.
rem ===========================================================================
:start_fresh
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"

rem  Wait until the service really answers HTTP (up to 20s).
rem  A listening port is not enough - the browser must not arrive before the
rem  HTTP stack can serve the page, or it shows ERR_CONNECTION_REFUSED.
"%PY%" "%READY%" --timeout 20
if errorlevel 1 goto start_failed

call :find_edge
if not defined EDGE goto no_edge
start "" "%EDGE%" --app=%URL% --no-first-run --no-default-browser-check
echo.
echo   Console started: %URL%
echo   Closing the browser window stops the service. This window will close.
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem ===========================================================================
rem  Port busy: there is a service. Is there also a window to raise?
rem  focus_console only reports success when it truly raised a console window;
rem  a just-closed window (heartbeat not yet expired) is exactly the case where
rem  the port is busy but nothing can be raised -> open a new window instead.
rem ===========================================================================
:reuse
"%PY%" "%FOCUS%" >nul 2>&1
if not errorlevel 1 goto reuse_ok

echo.
echo   A console service is already running, but no existing window could be
echo   raised (the previous window was probably closed moments ago).
echo   Opening a new window...
call :find_edge
if not defined EDGE goto reuse_noedge
start "" "%EDGE%" --app=%URL% --no-first-run --no-default-browser-check
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:reuse_noedge
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:reuse_ok
echo.
echo   Console is already running - brought the existing window to the front.
echo   (no new window was opened)
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

rem  Edge not found: the only case where the default browser is used.
:no_edge
echo.
echo   Edge not found - opening with the default browser instead: %URL%
echo   The service is running; stop it from the page footer.
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

rem  The service never answered HTTP: do NOT open a window onto a dead port,
rem  that is precisely what used to show "connection refused".
:start_failed
echo.
echo   [ERROR] The console service did not start within 20 seconds.
echo           Nothing was opened: a window onto a dead port only shows
echo           "connection refused".
echo   Check the log: %~dp0data\server_console.log
pause
endlocal
exit /b 1

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

rem ===========================================================================
rem  Subroutines
rem ===========================================================================

rem  Locate Edge (only needed when a window must actually be opened).
:find_edge
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
goto :eof
