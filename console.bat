@echo off
rem ============================================================================
rem  作业提醒控制台 —— 单窗口入口
rem
rem  双击后只看到一个 Edge 应用窗口（本窗口会在启动后自动关闭）。
rem  关闭那个浏览器窗口，服务自动退出并释放端口。
rem
rem  要点：
rem    1) 若服务已在跑，直接复用并打开界面，绝不起第二个进程
rem    2) PowerShell 以 --app 起 Edge 并取得 PID
rem    3) VBScript 无窗口启动 server.py，并让它守护上面那个 PID
rem
rem  注：本文件必须使用 CRLF 换行；括号块内只放 ASCII 提示，
rem      中文提示一律放在括号块之外（cmd 解析块内中文易出错）。
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0data"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SRV=%~dp0data\server.py"
set "VBS=%~dp0data\run_hidden.vbs"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" goto no_python
if not exist "%VBS%" goto no_vbs

rem ---- 1) service already listening? reuse it (never start a 2nd instance)
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

rem ---- 4) start hidden service, watching the browser process
cscript //nologo "%VBS%" "%PY%" "%EDGEPID%" "%SRV%"
echo.
echo   控制台已启动：%URL%
echo   关闭浏览器窗口后服务会自动停止，本窗口即将自动关闭。
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:reuse
echo.
echo   控制台服务已在运行，直接打开界面：%URL%
start "" "%URL%"
ping -n 2 127.0.0.1 >nul
endlocal
exit /b 0

:no_edge
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
echo.
echo   未找到 Edge，已用默认浏览器打开：%URL%
echo   服务将常驻运行；可点页面底部「停止控制台服务」停止。
start "" "%URL%"
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:no_pid
cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
echo.
echo   未能取得浏览器进程号，服务将常驻运行：%URL%
echo   可点页面底部「停止控制台服务」停止。
ping -n 3 127.0.0.1 >nul
endlocal
exit /b 0

:no_python
echo.
echo   [错误] 找不到 Python 解释器：
echo          %PY%
echo   请修改 console.bat 顶部的 PY 变量。
pause
exit /b 1

:no_vbs
echo.
echo   [错误] 缺少启动器：%VBS%
pause
exit /b 1
