# -*- coding: utf-8 -*-
"""把「每周定时推送」注册成 Windows 计划任务（2026-10-16）。

第一部分：每周日固定时间跑一次，把作业提醒推到企业微信（**默认测试群**）。
第二部分（周日开机补推）不在这里，见 task_catchup.py。

设计要点（都是踩过的坑）：
  · **必须用装了 playwright 的那个解释器**：系统 python 没装，抓取会直接失败。
    这里自动探测 %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\default 下的 python，
    找不到就报错，绝不静默用一个跑不起来的解释器去注册任务。
  · **只跑一次、不重复**：schtasks 的 /SC WEEKLY /D SUN 就是「每周日」，
    不需要自己在脚本里判断星期几。
  · **错过就补**（StartWhenAvailable）：周日那一刻电脑没开机/休眠，开机后补跑一次。
    这与第二部分「开机后检查」是两件事：这里是系统级的补跑，第二部分是
    业务级的「这周到底发出去没有」。
  · **任务名固定**，重名就覆盖（/F），避免注册出两个任务各发一次。
  · **默认发测试群**：命令里带上 --wecom test。要发正式群必须自己改命令，
    并且 README 里写了怎么改。

用法：
    python task_register.py --time 09:00                  # 注册（每周日 09:00，测试群）
    python task_register.py --time 09:00 --dry-run        # 只打印将要执行的命令
    python task_register.py --time 09:00 --wecom prod     # 注册成发正式班级群
    python task_register.py --query                       # 查看当前任务状态
    python task_register.py --unregister                  # 删除任务
    python task_register.py --run-now                     # 立刻触发一次（验证用）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

TASK_NAME = "BUCT-Homework-Weekly"
TASK_CATCHUP_NAME = "BUCT-Homework-Catchup"
TASK_RUN = ROOT / "task_run.py"
TASK_CATCHUP = ROOT / "task_catchup.py"


def find_python() -> str:
    """找装了 playwright 的解释器（就是本项目一直用的那个）。"""
    cands = [
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return sys.executable


def run_schtasks(args, capture=True):
    """调 schtasks。注意要传 list，别把带空格的路径拼成字符串。"""
    cmd = ["schtasks"] + args
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r


def powershell_available() -> bool:
    return bool(shutil.which("powershell") or shutil.which("pwsh"))


def apply_extra_settings(task_name: str) -> tuple:
    """补上 schtasks 命令行给不了的设置（错过就补跑、唤醒、失败重试）。

    用 PowerShell 的 ScheduledTasks 模块；拿不到就跳过并说明，
    不因为「锦上添花」的设置失败而把整件事判成失败。
    """
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        return False, "找不到 PowerShell，跳过附加设置"
    script = (
        f"$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop\n"
        "$s = $t.Settings\n"
        "$s.StartWhenAvailable = $true\n"      # 错过时间点（没开机）-> 尽快补跑
        "$s.DisallowStartIfOnBatteries = $false\n"
        "$s.StopIfGoingOnBatteries = $false\n"
        "$s.ExecutionTimeLimit = 'PT30M'\n"    # 抓取 1-2 分钟，30 分钟够且不会挂死
        "$s.MultipleInstances = 'IgnoreNew'\n" # 上一次没跑完就不再叠加
        "Set-ScheduledTask -TaskName '" + task_name + "' -Settings $s | Out-Null\n"
        "Write-Output 'SETTINGS-OK'\n"
    )
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = "SETTINGS-OK" in (r.stdout or "")
    return ok, ((r.stdout or "") + (r.stderr or "")).strip()[-300:]


def task_command(python: str, wecom: str, days: int) -> str:
    """计划任务要执行的命令行。

    ⚠️ 这里**不能**写 `>> log 2>&1`：计划任务的「操作」不做 shell 解释，
    重定向符会被当成 python 的参数传进去，argparse 直接报错退出 2
    （实测：Last Result=2、日志文件根本没被创建，任务看起来"跑了但什么都没有"）。
    日志改由 task_run.py 自己写 data/task_run.log，更可靠也更可控。
    """
    return f'"{python}" "{TASK_RUN}" --wecom {wecom} --days {days}'


def build_create_args(name: str, time_str: str, day: str, command: str,
                      once: bool = False, date_str: str = None) -> list:
    """组 schtasks 参数。

    once=True 时注册「只跑一次」的任务（/SC ONCE /SD 日期）——
    用来当场验证「到点会不会自己触发」，不必等到周日。
    """
    common = ["/Create", "/TN", name, "/TR", command, "/ST", time_str,
              "/F",          # 重名直接覆盖，避免注册出两个任务各发一次
              "/RL", "LIMITED"]
    if once:
        return common + ["/SC", "ONCE", "/SD", date_str or time.strftime("%Y/%m/%d")]
    return common + ["/SC", "WEEKLY", "/D", day]


def register_catchup(args) -> int:
    """注册第二部分：**仅周日**触发、错过就补跑的「补推检测」。

    为什么不用「登录时触发」：
      Windows 的登录触发器 schema 里只有 UserId / Delay / Enabled，
      **没有 DayOfWeek** —— 原生表达不出「仅周日登录时」。用登录触发器就必须
      每次登录都空跑一次（非周日也无害，但字面上不是"只在周日"）。

    改用「每周日 00:05 + StartWhenAvailable（错过就补跑）」：
      · 只在周日存在（周一~周六根本没有这个触发点）；
      · 周日那一刻电脑开着 -> 到点就跑（脚本自己等到"计划时间+15 分钟"再动作）；
      · 周日那一刻没开机 -> StartWhenAvailable 在**下次可用时**补跑，
        用户登录后的正常会话里就会执行 —— 这正是我们要的"开机后约 15 分钟"。

    ⚠️ 解释器用 pythonw.exe（无窗口）：python.exe 每次运行会闪一个黑框，
    而这个任务在周日可能被触发多次，闪窗很烦。pythonw 一样能跑，
    子进程输出照样被 task_run.py 捕获并写进日志。
    """
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        print("ERROR: 找不到 PowerShell，无法注册补推任务", file=sys.stderr)
        return 1
    python = find_python()
    if not Path(python).exists():
        print(f"ERROR: 解释器不存在：{python}", file=sys.stderr)
        return 1
    # 优先换成无窗口解释器（同目录的 pythonw.exe）
    pyw = Path(python).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else python
    if not TASK_CATCHUP.exists():
        print(f"ERROR: 找不到 {TASK_CATCHUP}", file=sys.stderr)
        return 1

    script = f"""
$ErrorActionPreference = 'Stop'
$name = '{args.name}'
$exe  = '{exe}'
$arg  = '"{TASK_CATCHUP}" --wecom {args.wecom} --days {args.days} --delay-min {args.delay_min} --target-time {args.target_time}'

$action = New-ScheduledTaskAction -Execute $exe -Argument $arg
# 仅周日：每周日 00:05 触发；错过（当时没开机）由 StartWhenAvailable 补跑
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 00:05
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {{
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'BUCT homework: Sunday-only catch-up check (missed run starts when available)' | Out-Null
$t = Get-ScheduledTask -TaskName $name
$i = $t | Get-ScheduledTaskInfo
Write-Output 'CATCHUP-OK'
Write-Output ('Trigger : ' + ($t.Triggers | ForEach-Object {{ $_.CimClass.CimClassName }}))
Write-Output ('Days    : ' + (($t.Triggers | ForEach-Object {{ $_.DaysOfWeek }})))
Write-Output ('At      : ' + (($t.Triggers | ForEach-Object {{ $_.StartBoundary }})))
Write-Output ('LogonType: ' + $t.Principal.LogonType)
Write-Output ('Execute : ' + $t.Actions[0].Execute)
Write-Output ('Argument: ' + $t.Actions[0].Arguments)
Write-Output ('StartWhenAvailable: ' + $t.Settings.StartWhenAvailable)
Write-Output ('NextRun : ' + $i.NextRunTime)
Write-Output ('State   : ' + $t.State)
"""

    print(f"计划任务名：{args.name}（**仅周日**触发）")
    print(f"触发条件　：每周日 00:05 + 错过就补跑（StartWhenAvailable）")
    print(f"执行脚本　：{TASK_CATCHUP}")
    print(f"参数　　　：--wecom {args.wecom} --days {args.days} "
          f"--delay-min {args.delay_min} --target-time {args.target_time}")
    print(f"解释器　　：{exe}" + ("（pythonw.exe，无窗口）" if exe != python else "（python.exe）"))
    print(f"判定逻辑　：仅周日 + 已过计划时间 {args.target_time} + 今天还没推成功 时，"
          f"等到 {args.target_time} 之后 {args.delay_min} 分钟（或开机后 {args.delay_min} 分钟）补推；"
          f"否则秒退")

    if args.dry_run:
        print("\n[dry-run] 未实际注册。")
        return 0

    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print("\nPowerShell 输出：\n" + "\n".join("  " + l for l in out.splitlines()))
    if "CATCHUP-OK" not in out:
        print("ERROR: 注册失败", file=sys.stderr)
        if "拒绝访问" in out or "Access is denied" in out:
            print("提示：注册计划任务可能需要管理员权限。", file=sys.stderr)
        return 1

    print(f"\n✅ 已注册补推检测：仅周日触发，错过（没开机）则下次可用时补跑")
    print(f"   补推日志：{ROOT / 'task_catchup.log'}")
    print(f"   任务台账：{ROOT / 'task_log.jsonl'}（kind=catchup）")
    print(f"   立刻验证：python data\\task_catchup.py --now --check-only")
    return 0


def register(args) -> int:
    python = find_python()
    command = task_command(python, args.wecom, args.days)
    cmd_args = build_create_args(args.name, args.time, args.day, command,
                                once=args.once, date_str=args.date)

    when = (f"一次性 {args.date or time.strftime('%Y/%m/%d')} {args.time}"
            if args.once else f"每周{args.day} {args.time}")
    print(f"计划任务名：{args.name}")
    print(f"执行时间　：{when}")
    print(f"解释器　　：{python}")
    print(f"目标群　　：{'测试群（--wecom test）' if args.wecom == 'test' else args.wecom}")
    print(f"完整命令　：{command}")
    print(f"schtasks 　：schtasks {' '.join(cmd_args)}")

    if not Path(python).exists():
        print(f"ERROR: 解释器不存在：{python}", file=sys.stderr)
        return 1
    if not TASK_RUN.exists():
        print(f"ERROR: 找不到 {TASK_RUN}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n[dry-run] 未实际注册。去掉 --dry-run 即生效。")
        return 0

    r = run_schtasks(cmd_args)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print("\nschtasks 输出：" + (out or "（空）"))
    if r.returncode != 0:
        print(f"ERROR: 注册失败（退出码 {r.returncode}）", file=sys.stderr)
        if "拒绝访问" in out or "Access is denied" in out:
            print("提示：注册计划任务可能需要管理员权限，"
                  "请用「以管理员身份运行」的终端重跑本命令。", file=sys.stderr)
        return 1

    ok, why = apply_extra_settings(args.name)
    print(f"附加设置（错过补跑/超时上限/不叠加）：{'已应用' if ok else '未应用'}（{why}）")
    print(f"\n✅ 已注册：{when} 自动推送（{'测试群' if args.wecom == 'test' else args.wecom}）")
    print(f"   任务台账：{ROOT / 'task_log.jsonl'}")
    print(f"   运行日志：{ROOT / 'task_run.log'}（由 task_run.py 自己写，不靠 shell 重定向）")
    print(f"   查看状态：python data\\task_register.py --query --name {args.name}")
    if args.once:
        print("   ⏳ 到点会自动触发，**不需要你做任何事**；跑完看上面的台账与日志。")
        print(f"   验证完记得删掉：python data\\task_register.py --unregister --name {args.name}")
    return 0


def query(name: str) -> int:
    r = run_schtasks(["/Query", "/TN", name, "/V", "/FO", "LIST"])
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        print(f"任务 {name} 不存在或查询失败：{out}")
        return 1
    keep = ("TaskName", "Next Run Time", "Status", "Last Run Time", "Last Result",
            "Schedule Type", "Start Time", "Days", "Task To Run", "Scheduled Task State")
    for line in out.splitlines():
        if any(k.lower() in line.lower() for k in keep):
            print("  " + line.strip())
    return 0


def unregister(name: str) -> int:
    r = run_schtasks(["/Delete", "/TN", name, "/F"])
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(out or "（无输出）")
    return 0 if r.returncode == 0 else 1


def run_now(name: str) -> int:
    r = run_schtasks(["/Run", "/TN", name])
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(out or "（无输出）")
    if r.returncode == 0:
        print("已触发。计划任务在后台跑，抓取约 1-2 分钟；"
              f"结束后看 {ROOT / 'task_log.jsonl'}（任务台账）与 {ROOT / 'task_run.log'}（完整输出）")
    return 0 if r.returncode == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="注册/管理定时任务（每周推送 + 周日补推检测）")
    ap.add_argument("--name", help=f"任务名（默认：每周={TASK_NAME}，补推={TASK_CATCHUP_NAME}）")
    ap.add_argument("--time", default="09:00", help="HH:MM（默认 09:00）")
    ap.add_argument("--day", default="SUN", help="SUN/MON/... 或 1-7（默认 SUN）")
    ap.add_argument("--wecom", choices=["test", "prod", "config"], default="test",
                    help="发到哪个群（默认 test=测试群）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--once", action="store_true",
                    help="注册「只跑一次」的任务（配合 --time/--date），用于当场验证到点是否自动触发")
    ap.add_argument("--date", help="配合 --once：YYYY/MM/DD（默认今天）")
    ap.add_argument("--catchup", action="store_true",
                    help="注册第二部分：登录时触发的「周日开机补推检测」")
    ap.add_argument("--delay-min", type=int, default=15,
                    help="补推前等待分钟数（默认 15，即「开机后约 15 分钟」）")
    ap.add_argument("--target-time", default="09:00",
                    help="计划推送时间 HH:MM（默认 09:00，与第一部分保持一致）；"
                         "早于它不补推，避免抢在定时任务前面")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不注册")
    ap.add_argument("--query", action="store_true", help="查看任务状态")
    ap.add_argument("--unregister", action="store_true", help="删除任务")
    ap.add_argument("--run-now", action="store_true", help="立刻触发一次（验证用）")
    args = ap.parse_args()

    if not args.name:
        args.name = TASK_CATCHUP_NAME if args.catchup else TASK_NAME

    if args.query:
        return query(args.name)
    if args.unregister:
        return unregister(args.name)
    if args.run_now:
        return run_now(args.name)
    if args.catchup:
        return register_catchup(args)
    return register(args)


if __name__ == "__main__":
    sys.exit(main())
