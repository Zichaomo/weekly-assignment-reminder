' ============================================================================
'  run_hidden.vbs —— 以【无窗口】方式启动控制台服务（console.bat 调用）
'
'  为什么要它：cmd 的 `start /min` 仍会在任务栏留一个控制台窗口，
'  而双击 console.bat 的目标是"只有一个浏览器窗口"。
'  用 WScript.Shell.Run 且窗口样式设为 0，可以真正隐藏控制台。
'
'  参数： 0 = Python 解释器路径
'         1 = 要守护的 Edge 进程号（空 = 不守护，服务常驻）
'         2 = server.py 路径
'
'  服务输出写入 data/server_console.log。服务是无窗口的，出问题时
'  没有别的地方能看到它的打印，所以日志是必须的（且 Python 要加 -u，
'  否则块缓冲会让日志迟迟不落盘）。
' ============================================================================
Option Explicit

Dim sh, py, watchPid, srv, logFile, cmd, fso, root, inner
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count < 3 Then
  WScript.Echo "用法: cscript //nologo run_hidden.vbs <python.exe> <watchPid|空> <server.py>"
  WScript.Quit 2
End If

py = WScript.Arguments(0)
watchPid = WScript.Arguments(1)
srv = WScript.Arguments(2)

root = fso.GetParentFolderName(srv)
logFile = root & "\server_console.log"

' -u 关闭 Python 的块缓冲：无终端运行时默认按 4KB 缓冲，日志迟迟不落盘，
'    出问题时看不到任何输出（实测日志一直为空就是这个问题）。
'
' 引号要点：cmd /c 会剥掉【最外层】一对引号，所以整条命令要再包一层。
'    内层程序与参数各自成对，重定向放在里层 → cmd 会把整串当一条命令执行。
'    （少包这层会报 "--watch-pid 不是内部或外部命令"，实测踩过）
inner = """" & py & """ -u """ & srv & """"
If Len(watchPid) > 0 Then
  inner = inner & " --watch-pid " & watchPid
End If
inner = inner & " >> """ & logFile & """ 2>&1"
cmd = "cmd /c """ & inner & """"

' 0 = 隐藏窗口, False = 不等待（服务在后台跑，由 --watch-pid 自行退出）
sh.Run cmd, 0, False
