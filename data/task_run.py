# -*- coding: utf-8 -*-
"""定时任务运行器 —— 计划任务真正调用的入口（2026-10-16）。

它不重复实现任何业务逻辑：抓取/汇总/排版/推送全部交给 weekly.py，
告警交给 alert.py。本文件只负责计划任务特有的那几件事：

  1. **决定往哪个群发**（默认 `--wecom config`）—— 跟随 `config.json` 的
     `wecom_webhook`：你在那里放测试群就发测试群，换成正式班级群就发正式群。
     测试时想临时只发测试群，用显式 `--wecom test`（读 `wecom_webhook_test`）。
     这个选择是刻意的：**群的选择权只在一个地方（config.json）**，避免
     "换了 key 但某个任务还钉在旧群"这种最难发现的错。
  2. **环境变量控制进程，而不是改配置文件** —— 靠 notify.BUCT_WECOM_WEBHOOK。
     为一次测试去改 config.json、然后忘了改回来，是本项目明确记录过的风险。
  3. **落一份任务级台账**（data/task_log.jsonl）：几点跑的、跑多久、退出码、
     抓到多少条、发到哪个群。系统事件日志里查不到这些，出了事必须能自己回答
     「周日那次到底跑了吗、跑成什么样」。
  4. **控制台日志**：weekly.py 的 stdout/stderr 原样进 task_run.log，
     计划任务本身没有窗口，没日志等于事后无法排查。

用法：
    python task_run.py                      # 默认：跟随 config.json 的 wecom_webhook
    python task_run.py --wecom test         # 强制只发测试群（演练用）
    python task_run.py --wecom prod         # 强制发正式班级群
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
from datetime import datetime
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


def push_status_today(cfg: dict) -> dict:
    """今天两条消息各自推成功没有。

    audience=class    → 班级群（全部课程 − 选修课）
    audience=personal → 个人小群（完整清单，含选修课）

    为什么要分开查：两条是两件事。班级群发成功了不代表个人那条也发成功；
    只看一条会出现「一条失败被另一条掩盖」—— 重试被幂等闸挡住，
    缺的那条就永远补不上了。
    """
    st = {"class": False, "personal": False, "personal_enabled": False}
    try:
        import alert
        import notify as _n
        st["personal_enabled"] = _n.personal_ready(cfg)[0]
        recs = alert._push_records()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 幂等闸判定失败（{e}），本次照常执行", file=sys.stderr)
        return st
    today = datetime.now().date()
    for r in recs:
        dt = alert._parse_time(r.get("time"))
        if not dt or dt.date() != today:
            continue
        aud = (r.get("audience") or "class").lower()
        if aud in st:
            st[aud] = True
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description="定时任务运行器（默认跟随 config）")
    ap.add_argument("--wecom", choices=["config", "test", "prod"], default="config",
                    help="发到哪个群：config=跟随 config.json 的 wecom_webhook（默认，"
                         "你换成哪个群就发哪个群）/ test=强制测试群 / prod=强制班级群")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--channel", default="wecom",
                    help="推送渠道（默认 wecom；演练可传 none/clipboard）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取并生成文案，不发送")
    ap.add_argument("--kind", default="scheduled",
                    help="触发来源标记，写进任务台账（scheduled / manual / boot-check）")
    ap.add_argument("--target-time", default="09:00",
                    help="计划推送时间 HH:MM（默认 09:00）；用于判断「今天推过没有」")
    ap.add_argument("--force", action="store_true",
                    help="跳过「今天已推过就跳过」的幂等闸（确实要重发时用）")
    args = ap.parse_args()

    t0 = time.time()
    log_line("")
    log_line("=" * 62)
    log_line(f"[task_run] 开始 {time.strftime('%Y-%m-%d %H:%M:%S')} "
             f"kind={args.kind} wecom={args.wecom} days={args.days} "
             f"channel={'none(演练)' if args.dry_run else args.channel}")

    cfg = notify.load_config()

    # ---- 幂等闸：今天两条都发过了，才秒退 ----
    # 为什么必须有（2026-10-18 的实际缺口）：
    #   09:00 那一刻电脑可能刚开机、还停在登录界面，交互式任务起不来，
    #   Windows 记成「错过一次」且**不会**在登录后自动补跑 → 整周静默。
    #   对策是把 Weekly 触发器改成「09:00 起每 30 分钟重试，持续 2 小时」，
    #   代价是同一天会被触发多次。所以第一件事必须是问一句：
    #   「今天已经发出去了吗？」是 -> 秒退，绝不重复打扰群里。
    #
    # 2026-10-18 之后多了一条消息（个人小群），所以判定分两半：
    #   · 班级群已发 + 个人已发（或没启用个人通道）→ 秒退
    #   · 只缺其中一条 → 照常跑，但让 weekly 跳过已发的那条（--skip-class / --skip-personal）
    weekly_flags = []
    if not args.dry_run and args.channel == "wecom" and not args.force:
        st = push_status_today(cfg)
        personal_ok = st["personal"] or not st["personal_enabled"]
        if st["class"] and personal_ok:
            log_line("[task_run] 今天两条都已成功推送 —— 本次跳过，不重复打扰群。")
            log_line(f"[task_run] 证据：班级群已发={st['class']}，"
                     f"个人小群已发={st['personal']}（启用={st['personal_enabled']}）")
            log_task(kind=args.kind, ok=True, skipped=True, exit_code=0,
                     elapsed_sec=round(time.time() - t0, 1), wecom=args.wecom,
                     reason="already-pushed-today")
            return 0
        if st["class"]:
            log_line("[task_run] 班级群今天已发过；这次只补发「个人小群（含选修课）」那条。")
            weekly_flags.append("--skip-class")
        if st["personal"]:
            log_line("[task_run] 个人小群今天已发过；这次只补发班级群那条。")
            weekly_flags.append("--skip-personal")

    # ---- 决定目标群 ----
    # config（默认）：跟随 config.json 的 wecom_webhook —— 你在那里放哪个群就发哪个群，
    #   换了 key 不需要重新注册任务，也不会出现"任务钉在旧群"的错。
    # test：强制测试群，且 strict=True —— 测试群没配就报错停止，绝不退回 wecom_webhook，
    #   因为"我要发测试群"和"实际发到主用群（可能是班级群）"是完全两回事。
    strict = (args.wecom == "test")
    target_hook, why = notify.resolve_webhook(args.wecom, cfg, strict=strict)
    if not args.dry_run and args.channel == "wecom" and not target_hook:
        msg = ("未解析到企业微信 webhook —— 不会发出任何消息。\n"
               "原因：" + why + "\n"
               "请在 config.json 里配好对应的 key：跟随 config 时是 wecom_webhook，"
               "强制测试群时是 wecom_webhook_test。")
        log_line("[task_run] ERROR: " + msg.replace("\n", " "))
        log_task(kind=args.kind, ok=False, exit_code=2, elapsed_sec=round(time.time() - t0, 1),
                 wecom=args.wecom, error=msg)
        _alert("task-no-webhook", "作业提醒定时任务：没有可用的企业微信 webhook", msg)
        return 2

    if args.channel == "wecom":
        log_line(f"[task_run] 目标群：{why}  webhook={mask(target_hook)}")
        if args.wecom == "prod":
            log_line("[task_run] ⚠️ 这次会发到【正式班级群】，同学会收到消息")
        elif args.wecom == "config":
            log_line("[task_run] 说明：跟随 config.json 的 wecom_webhook，"
                     "换了 key 下次就会发到新群（不用重新注册任务）")
    os.environ[notify.WECOM_ENV] = target_hook or ""
    os.environ["BUCT_NOTIFY_CHANNEL"] = "none" if args.dry_run else args.channel
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # ---- 交给 weekly.py（业务逻辑一行都不重复）----
    cmd = [sys.executable, str(ROOT / "weekly.py"), "--days", str(args.days)]
    if args.dry_run:
        cmd.append("--no-send")
    cmd += weekly_flags
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
        if args.dry_run:
            log_line("[task_run] 演练模式：失败告警未发送。")
            return r.returncode
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
