# -*- coding: utf-8 -*-
"""定时任务运行器 —— 计划任务真正调用的入口（2026-10-16）。

它不重复实现任何业务逻辑：抓取/汇总/排版/推送全部交给 weekly.py，
告警交给 alert.py。本文件只负责计划任务特有的那几件事：

  1. **决定往哪个群发**（默认测试群！）—— 这是最容易出人命的地方：
     作业提醒是发给全班同学的，定时任务第一次跑很可能是在无人看管的时候，
     所以默认 must 走测试群，要发正式群必须显式 `--wecom prod`。
  2. **环境变量控制进程，而不是改配置文件** —— 靠 notify.BUCT_WECOM_WEBHOOK。
     为一次测试去改 config.json、然后忘了改回来，是本项目明确记录过的风险。
  3. **落一份任务级台账**（data/task_log.jsonl）：几点跑的、跑多久、退出码、
     抓到多少条、发到哪个群。系统事件日志里查不到这些，出了事必须能自己回答
     「周日那次到底跑了吗、跑成什么样」。
  4. **控制台日志**：weekly.py 的 stdout/stderr 原样进 task_run.log，
     计划任务本身没有窗口，没日志等于事后无法排查。

用法：
    python task_run.py                      # 默认：发测试群，未来 7 天
    python task_run.py --wecom prod         # 发正式班级群
    python task_run.py --dry-run            # 只抓取+生成文案，不发送
    python task_run.py --days 7 --channel wecom

退出码：0 成功；非 0 = 失败（计划任务会记录，alert.py 也会告警）。
"""
import argparse
import json
import os
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

sys.path.insert(0, str(ROOT))
import notify  # noqa: E402  复用 webhook 解析，避免「哪个群」有两套说法

TASK_LOG = ROOT / "task_log.jsonl"
CONSOLE_LOG = ROOT / "task_run.log"


def log_line(*parts) -> None:
    """同时写控制台与 task_run.log（计划任务无窗口，日志是唯一的现场）。"""
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        with CONSOLE_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"WARN: 写 task_run.log 失败 {e}", file=sys.stderr)


def log_task(**rec) -> None:
    rec.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
    rec.setdefault("ts", round(time.time(), 3))
    rec.setdefault("pid", os.getpid())
    try:
        with TASK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: 写任务台账失败 {e}", file=sys.stderr)


def last_fetch_record() -> dict:
    """取检索台账的最后一条（weekly.py 刚写的那条），用于任务台账补充数据。"""
    p = ROOT / "fetch_log.jsonl"
    try:
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    except OSError:
        pass
    return {}


def mask(hook: str) -> str:
    """webhook 的 key 是群发口令，日志里只留尾 8 位。"""
    return ("…" + hook[-8:]) if len(hook) > 8 else "(空)"


def main() -> int:
    ap = argparse.ArgumentParser(description="定时任务运行器（默认发测试群）")
    ap.add_argument("--wecom", choices=["test", "prod", "config"], default="test",
                    help="发到哪个群：test=测试群（默认）/ prod=正式班级群 / config=按 config.json")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--channel", default="wecom",
                    help="推送渠道（默认 wecom；演练可传 none/clipboard）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取并生成文案，不发送")
    ap.add_argument("--kind", default="scheduled",
                    help="触发来源标记，写进任务台账（scheduled / manual / boot-check）")
    args = ap.parse_args()

    t0 = time.time()
    log_line("")
    log_line("=" * 62)
    log_line(f"[task_run] 开始 {time.strftime('%Y-%m-%d %H:%M:%S')} "
             f"kind={args.kind} wecom={args.wecom} days={args.days} "
             f"channel={'none(演练)' if args.dry_run else args.channel}")

    cfg = notify.load_config()

    # ---- 决定目标群（默认测试群，写清楚为什么）----
    target_hook, why = notify.resolve_webhook(
        "test" if args.wecom == "test" else args.wecom, cfg)
    if not args.dry_run and args.channel == "wecom" and not target_hook:
        msg = ("未解析到企业微信 webhook —— 不会发出任何消息。"
               "请在 config.json 里配 wecom_webhook(_test)。")
        log_line("[task_run] ERROR: " + msg)
        log_task(kind=args.kind, ok=False, exit_code=2, elapsed_sec=round(time.time() - t0, 1),
                 wecom=args.wecom, error=msg)
        _alert("task-no-webhook", "作业提醒定时任务：没有可用的企业微信 webhook", msg)
        return 2

    if args.channel == "wecom":
        log_line(f"[task_run] 目标群：{why}  webhook={mask(target_hook)}")
        if args.wecom == "prod":
            log_line("[task_run] ⚠️ 这次会发到【正式班级群】，同学会收到消息")
    os.environ[notify.WECOM_ENV] = target_hook or ""
    os.environ["BUCT_NOTIFY_CHANNEL"] = "none" if args.dry_run else args.channel
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # ---- 交给 weekly.py（业务逻辑一行都不重复）----
    cmd = [sys.executable, str(ROOT / "weekly.py"), "--days", str(args.days)]
    if args.dry_run:
        cmd.append("--no-send")
    log_line("[task_run] 执行：" + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT), env=os.environ.copy(),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for stream in (r.stdout or "", r.stderr or ""):
        for line in stream.splitlines():
            log_line("  | " + line)

    elapsed = time.time() - t0
    fetch = last_fetch_record()
    ok = (r.returncode == 0)
    rec = dict(
        kind=args.kind, ok=ok, exit_code=r.returncode, elapsed_sec=round(elapsed, 1),
        wecom=args.wecom, channel=("none" if args.dry_run else args.channel),
        target=mask(target_hook) if args.channel == "wecom" else "",
        source="task-run",
        platform=fetch.get("platform"), tests=fetch.get("tests"),
        rows=fetch.get("rows"), failed_count=fetch.get("failed_count"),
        error="" if ok else (r.stderr or "").strip()[-300:],
    )
    log_task(**rec)

    if ok:
        log_line(f"[task_run] 完成：退出码 0，用时 {elapsed:.0f} 秒"
                 + ("（演练，未发送）" if args.dry_run else f"（已发 {rec['target']}）"))
    else:
        log_line(f"[task_run] 失败：退出码 {r.returncode}，用时 {elapsed:.0f} 秒")
        # weekly.py 自己已经就各类失败发过告警了；这里只在「连它的告警都没发出去」
        # 这种极端情况下兜一手，避免任务挂了却一片安静。
        _alert("task-failed", "作业提醒定时任务执行失败",
               f"task_run.py 调用 weekly.py 返回 {r.returncode}\n"
               f"用时 {elapsed:.0f} 秒\n"
               f"stderr 尾部：\n{(r.stderr or '').strip()[-600:] or '（空）'}\n\n"
               "排查：data\\task_run.log（本次完整输出）、data\\fetch_log.jsonl、"
               "data\\push_log.jsonl。")
    return r.returncode


def _alert(kind: str, title: str, detail: str) -> None:
    """发告警（旁路，出问题也不该影响任务本身的退出码）。"""
    try:
        import alert
        alert.send(kind, title, detail, level="error")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 告警发送失败（已忽略）{type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
