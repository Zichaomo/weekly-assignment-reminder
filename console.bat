@echo off
rem ============================================================================
rem  作业提醒控制台 —— 单窗口入口
rem
rem  双击后【只看到一个 Edge 应用窗口】：本窗口会在几秒内自动关闭。
rem  关闭那个浏览器窗口 -> 服务自动退出（释放端口与内存），不留任何残余进程。
rem
rem  实现：
rem    1) PowerShell 以 --app 方式起 Edge，并取得它的 PID
rem    2) VBScript 把 server.py 以【无窗口】方式启动，并告知它守护上面那个 PID
rem    3) 服务端每 3 秒检查该进程；浏览器窗口一关，整棵进程树消失 -> 服务自我退出
rem    （已实测：Edge --app 实例关闭后，其 14 个子进程全部消失）
rem
rem  为什么不在本窗口里等待：等待会让控制台窗口一直留着 —— 那正是要消除的
rem  "第二个窗口"。让服务自己守护浏览器，本窗口就能立刻退出。
rem
rem  兜底：万一没拿到 Edge PID，服务会常驻（与旧版行为一致，不会更差）：
rem        此时可点页面底部「停止控制台服务」，或手动结束 python 进程。
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0data"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SRV=%~dp0data\server.py"
set "VBS=%~dp0data\run_hidden.vbs"
set "URL=http://127.0.0.1:8277"

if not exist "%PY%" (
  echo [错误] 找不到 Python 解释器：
  echo        %PY%
  echo        请修改 console.bat 里的 PY 变量。
  pause
  exit /b 1
)
if not exist "%VBS%" (
  echo [错误] 缺少启动器：%VBS%
  pause
  exit /b 1
)

rem ------------------------------------------------------------- 1) 定位 Edge
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"

rem ------------------------------------------- 2) 起 Edge 应用窗口并取 PID
set "EDGEPID="
if defined EDGE (
  for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$p = Start-Process -FilePath '%EDGE%' -ArgumentList '--app=%URL%','--no-first-run','--no-default-browser-check' -PassThru; $p.Id" 2^>nul`) do set "EDGEPID=%%i"
)

rem --------------------------------- 3) 后台无窗口启动服务（守护浏览器进程）
if defined EDGEPID (
  cscript //nologo "%VBS%" "%PY%" "%EDGEPID%" "%SRV%"
  echo 控制台已启动：%URL%
  echo 关闭浏览器窗口后服务会自动停止。本窗口即将自动关闭...
) else (
  cscript //nologo "%VBS%" "%PY%" "" "%SRV%"
  echo 控制台已启动：%URL%
  echo （未取得浏览器进程号，服务将常驻；可点页面底部「停止控制台服务」停止）
  start "" "%URL%"
)

rem 给服务 2 秒起来；若浏览器没起来（没找到 Edge），用默认浏览器兜底
ping -n 3 127.0.0.1 >nul
if not defined EDGE start "" "%URL%"

rem 本窗口直接退出：不再留第二个窗口。服务由它自己守护浏览器进程。
endlocal
exit /b 0
