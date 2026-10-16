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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

TASK_NAME = "BUCT-Homework-Weekly"
TASK_RUN = ROOT / "task_run.py"


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


def build_create_args(name: str, time_str: str, day: str, command: str) -> list:
    return ["/Create", "/TN", name, "/TR", command,
            "/SC", "WEEKLY", "/D", day, "/ST", time_str,
            "/F",          # 重名直接覆盖，避免注册出两个任务各发一次
            "/RL", "LIMITED"]


def register(args) -> int:
    python = find_python()
    command = task_command(python, args.wecom, args.days)
    cmd_args = build_create_args(args.name, args.time, args.day, command)

    print(f"计划任务名：{args.name}")
    print(f"执行时间：每周{'日一二三四五六'[int(args.day)] if args.day.isdigit() else args.day} {args.time}")
    print(f"解释器　：{python}")
    print(f"目标群　：{'测试群（--wecom test）' if args.wecom == 'test' else args.wecom}")
    print(f"完整命令：{command}")
    print(f"schtasks ：schtasks {' '.join(cmd_args)}")

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
    print(f"\n✅ 已注册：每周 {args.day} {args.time} 自动推送（{'测试群' if args.wecom == 'test' else args.wecom}）")
    print(f"   任务台账：{ROOT / 'task_log.jsonl'}")
    print(f"   运行日志：{ROOT / 'task_run.log'}（由 task_run.py 自己写，不靠 shell 重定向）")
    print(f"   立刻验证：python data\\task_register.py --run-now")
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
    ap = argparse.ArgumentParser(description="注册/管理「每周定时推送」计划任务")
    ap.add_argument("--name", default=TASK_NAME)
    ap.add_argument("--time", default="09:00", help="HH:MM（默认 09:00）")
    ap.add_argument("--day", default="SUN", help="SUN/MON/... 或 1-7（默认 SUN）")
    ap.add_argument("--wecom", choices=["test", "prod", "config"], default="test",
                    help="发到哪个群（默认 test=测试群）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="只打印，不注册")
    ap.add_argument("--query", action="store_true", help="查看任务状态")
    ap.add_argument("--unregister", action="store_true", help="删除任务")
    ap.add_argument("--run-now", action="store_true", help="立刻触发一次（验证用）")
    args = ap.parse_args()

    if args.query:
        return query(args.name)
    if args.unregister:
        return unregister(args.name)
    if args.run_now:
        return run_now(args.name)
    return register(args)


if __name__ == "__main__":
    sys.exit(main())
