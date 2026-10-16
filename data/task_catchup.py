# -*- coding: utf-8 -*-
"""第二部分：周日开机后的补推检测（2026-10-16）。

要解决的问题：
    第一部分的定时推送只在**那一刻电脑开着**时才跑得成。周日上午没开机、
    或者开机了但锁屏导致自动登录失败，这一周的提醒就没了 —— 而且没人会发现。
    本脚本负责：开机（登录）后检查「本周日到底推出去了没有」，没推就补推。

运行方式：注册成「**只在周日**触发、错过就补跑」的计划任务
（`task_register.py --catchup`）。脚本自己保证下面这些约束，不依赖任务怎么配：

  1. **只在周日干活**：不是周日直接退出（一次判断，秒退，不占资源）。
     这一条既是安全网，也是触发器语义的兜底 —— Windows 的登录触发器**没有**
     「仅周日」这个条件（schema 里只有 UserId/Delay/Enabled），所以任务用
     「每周日 + 错过就补跑」来表达"只在周日"，脚本再确认一次。
  2. **周日开机后约 15 分钟再动作**：等到「计划时间 + 15 分钟」
     （如 09:00 计划 → 09:15）或「开机后 15 分钟」两者中较早的那个，
     给正常的定时推送留出发车时间，避免抢跑。
     已经是周日、且已过两者时，一秒都不等。
  3. **只在没推的时候补推**：`alert.weekly_due()` 按**日历日**判断
     「今天（周日）有没有成功推过群」，而不是"距今多少小时"。
     已经推过 -> 什么都不做（这就是"检测"本身）。
  4. **不重复补推**：补推成功后再触发本脚本，第 3 条就会拦住它。
  5. **不抢跑**：没到计划推送时间（默认 09:00，与第一部分一致）就退出，
     让定时任务先跑。

用法：
    python task_catchup.py                  # 正常运行（计划任务用这个）
    python task_catchup.py --now            # 立刻判断（不做"周日/等待"检查），用于验证
    python task_catchup.py --check-only     # 只判断并打印，绝不补推
    python task_catchup.py --delay-min 0    # 覆盖等待分钟数
    python task_catchup.py --wecom prod     # 补推发正式班级群（默认测试群）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(ROOT))
import alert      # noqa: E402
import notify     # noqa: E402

LOG_FILE = ROOT / "task_catchup.log"
TASK_LOG = ROOT / "task_log.jsonl"

# 与第一部分保持一致的默认值；改定时时间时这里和任务一起改
# （也可用注册命令覆盖：task_register.py --catchup --target-time HH:MM）
DEFAULT_TARGET = "09:00"
DEFAULT_DELAY_MIN = 15
WEEKDAY_SUNDAY = 7


def log_line(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError as e:
        print(f"WARN: 写 task_catchup.log 失败 {e}", file=sys.stderr)


def log_task(**rec) -> None:
    rec.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    rec.setdefault("ts", round(time.time(), 3))
    rec.setdefault("pid", os.getpid())
    rec.setdefault("source", "task-run")
    try:
        with TASK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: 写任务台账失败 {e}", file=sys.stderr)


def last_fetch_record() -> dict:
    p = ROOT / "fetch_log.jsonl"
    try:
        for line in reversed([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    except OSError:
        pass
    return {}


def still_running(minutes: int = 30) -> bool:
    """最近一条任务台账是不是「还在跑」（30 分钟内，无后续完成记录）。

    防的是这种场面：补推刚启动，用户又注销/登录一次，于是又触发一轮 ——
    两个抓取会抢同一个 storage_state 与平台会话（scrape.py 有文件锁，但要等 120 秒）。
    """
    try:
        lines = [l for l in TASK_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return False
    if not lines:
        return False
    try:
        last = json.loads(lines[-1])
        t = datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return False
    if "ok" in last:                     # 已有结论 = 那一轮结束了
        return False
    return (datetime.now() - t).total_seconds() < minutes * 60


def main() -> int:
    ap = argparse.ArgumentParser(description="周日开机后的补推检测")
    ap.add_argument("--now", action="store_true",
                    help="立刻判断，跳过「只在周日」与「等待 15 分钟」两道闸（验证用）")
    ap.add_argument("--check-only", action="store_true", help="只判断并打印，绝不补推")
    ap.add_argument("--delay-min", type=int, default=DEFAULT_DELAY_MIN,
                    help=f"开机后等待多久再判断（默认 {DEFAULT_DELAY_MIN} 分钟）")
    ap.add_argument("--target-time", default=DEFAULT_TARGET,
                    help=f"计划推送时间，早于它不补推（默认 {DEFAULT_TARGET}）")
    ap.add_argument("--wecom", choices=["test", "prod", "config"], default="test",
                    help="补推发哪个群（默认 test=测试群）")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    now = datetime.now()
    log_line("")
    log_line("=" * 62)
    log_line(f"[补推检测] 开始 {now:%Y-%m-%d %H:%M:%S} 周{'一二三四五六日'[now.isoweekday() - 1]}"
             f"（{'--now 模式' if args.now else '正常模式'}，等待 {args.delay_min} 分钟）")

    # ---- 第一道闸：只在周日干活 ----
    if not args.now and now.isoweekday() != WEEKDAY_SUNDAY:
        log_line("[补推检测] 今天不是周日，退出（不消耗任何等待）。")
        return 0

    # ---- 先判断，再决定要不要等 ----
    status, why, ev = alert.weekly_due(now, target_time=args.target_time)
    log_line(f"[补推检测] 判定：{status} —— {why}")
    log_line("[补推检测] 证据：" + json.dumps(ev, ensure_ascii=False))

    if status == "done":
        log_line("[补推检测] 今天已经推过了，无需补推。退出。")
        return 0
    if status == "too_early":
        log_line("[补推检测] 还没到计划时间，交给定时任务。退出。")
        return 0
    if status == "not_sunday" and not args.now:
        log_line("[补推检测] 非周日，退出。")
        return 0

    if args.check_only:
        log_line("[补推检测] --check-only：只报告，不补推。")
        return 0

    # ---- 需要补推：等到「计划时间之后 + delay 分钟」为止 ----
    # 为什么是"等到某个时刻"而不是"死等 N 分钟"：
    # 触发器改成「仅在周日、错过就补跑」之后，任务可能在周日凌晨开机时就被触发
    # （比如 02:00 开机），那时离计划时间 09:00 还早。死等 15 分钟毫无意义，
    # 而应该是「等到 09:00 过了 15 分钟（09:15）再动作」。
    # 同时把上限设成 delay_min 分钟：开机晚于 09:00 时（如 10:00 开机）
    # 就是「开机后约 15 分钟」执行，与需求一致。
    if not args.now:
        due_at = datetime.now() + timedelta(minutes=max(0, args.delay_min))
        try:
            hh, mm = (int(x) for x in str(args.target_time).split(":")[:2])
            after_target = datetime.now().replace(hour=hh, minute=mm, second=0,
                                                  microsecond=0) + timedelta(minutes=args.delay_min)
            if after_target > due_at:
                due_at = after_target
        except Exception:  # noqa: BLE001
            pass
        wait_sec = (due_at - datetime.now()).total_seconds()
        if wait_sec > 0:
            log_line(f"[补推检测] 需要补推，等到 {due_at:%H:%M:%S} 再动作"
                     f"（约 {wait_sec / 60:.0f} 分钟）…")
            while wait_sec > 0:
                time.sleep(min(wait_sec, 60))
                wait_sec = (due_at - datetime.now()).total_seconds()
            # 复查：等待期间定时任务可能已经跑成功了（这是最好结局）
            status2, why2, _ = alert.weekly_due(datetime.now(), target_time=args.target_time)
            log_line(f"[补推检测] 等待结束，复查：{status2} —— {why2}")
            if status2 == "done":
                log_line("[补推检测] 等待期间已经推送成功，无需补推。退出。")
                return 0
            if status2 == "not_sunday":
                log_line("[补推检测] 已经跨天了（不再是周日），退出。")
                return 0

    if still_running():
        log_line("[补推检测] 检测到另一个抓取刚开始（任务台账有待完成的记录），"
                 "本次跳过，避免两个抓取互相干扰。")
        log_task(kind="catchup", ok=False, exit_code=0, skipped=True,
                 note="另一个任务正在运行，跳过", wecom=args.wecom)
        return 0

    # ---- 补推：复用第一部分的运行器，业务逻辑一行不重复 ----
    t0 = time.time()
    cmd = [sys.executable, str(ROOT / "task_run.py"),
           "--wecom", args.wecom, "--days", str(args.days),
           "--kind", "catchup"]
    log_line("[补推检测] 开始补推 → " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), env=os.environ.copy(),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for stream in (r.stdout or "", r.stderr or ""):
        for line in stream.splitlines():
            log_line("  | " + line)

    elapsed = time.time() - t0
    ok = (r.returncode == 0)
    fetch = last_fetch_record()
    log_task(kind="catchup", ok=ok, exit_code=r.returncode, elapsed_sec=round(elapsed, 1),
             wecom=args.wecom, rows=fetch.get("rows"), platform=fetch.get("platform"),
             reason=why[:200], trigger=("now-mode" if args.now else "logon"),
             error="" if ok else (r.stderr or "").strip()[-300:])

    if ok:
        log_line(f"[补推检测] 补推完成：退出码 0，用时 {elapsed:.0f} 秒（已发 {args.wecom}）")
    else:
        log_line(f"[补推检测] 补推失败：退出码 {r.returncode}，用时 {elapsed:.0f} 秒")
        # task_run.py 失败时自己会告警；这里只在它没发出去时兜一手
        try:
            alert.send("catchup-failed", "作业提醒：周日补推失败",
                       f"补推失败（task_run.py 退出码 {r.returncode}）。\n"
                       f"原因：{why}\n用时 {elapsed:.0f} 秒\n"
                       f"stderr 尾部：\n{(r.stderr or '').strip()[-500:] or '（空）'}\n\n"
                       "排查：data\\task_catchup.log、data\\task_run.log、data\\push_log.jsonl。")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: 告警发送失败（已忽略）{e}", file=sys.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
