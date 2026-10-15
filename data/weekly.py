# -*- coding: utf-8 -*-
"""
端到端入口：抓取北化在线作业 -> 合并手动录入 -> 汇总 -> 推送。

用法:
    python weekly.py                       # 抓取 + 汇总 + 推送（未来 7 天）
    python weekly.py --days 3              # 只看未来 3 天
    python weekly.py --no-send             # 演练：只打印/落盘，不推送
    python weekly.py --channel pushplus    # 临时指定推送渠道（覆盖 config.json）

手动补录（老师发在微信群/其他企业微信群、平台抓不到的）：
    见 manual.py —— python manual.py add
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# 子进程统一按 utf-8 输出，避免 Windows 下 GBK 中文把父进程解码搞崩。
UTF8_ENV = os.environ.copy()
UTF8_ENV["PYTHONIOENCODING"] = "utf-8"


def fetch(extra_args: list = None) -> dict:
    """调用 scrape.py 抓取，返回结构化 dict。

    extra_args: 追加给 scrape.py 的命令行参数，例如 ["--with-content"] 表示连作业正文一起抓。
    抓取耗时写进返回值的 _elapsed_sec，方便上层显示「本次抓取用时」。
    """
    cmd = [PY, str(ROOT / "scrape.py")] + list(extra_args or [])
    t0 = time.time()
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=UTF8_ENV,
        cwd=str(ROOT),
    )
    elapsed = time.time() - t0
    if out.returncode != 0:
        raise RuntimeError(f"抓取失败（耗时 {elapsed:.0f} 秒）:\n{out.stdout}\n{out.stderr}")
    if not out.stdout:
        raise RuntimeError(f"抓取无输出（耗时 {elapsed:.0f} 秒）:\n{out.stderr}")
    data = json.loads(out.stdout)
    data["_elapsed_sec"] = round(elapsed, 1)
    return data


def load_manual() -> list:
    """读取手动录入的作业（老师发在微信群等平台抓不到的）。"""
    path = ROOT / "manual.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        if it.get("done"):
            continue  # 已完成的不再提醒
        out.append({
            "course": it.get("course", "未命名课程"),
            "title": it.get("title", "作业"),
            "deadline": it.get("deadline", ""),
            "note": it.get("note", ""),
            "source": "manual",
        })
    return out


def parse_deadline(s: str):
    """解析截止时间字符串，返回 datetime；解析不了返回 None。

    支持：2026-09-17 23:59 / 2026-09-17 / 2026/9/17 23:59 / 9-17 23:59 / 9/17 / 9.10 / 9月10日
    只给日期时默认当天 23:59。
    """
    s = (s or "").strip()
    if not s:
        return None
    # 短格式（仅月日）转成 %m-%d 再走标准解析，避免重复写一堆月日格式
    short = s.replace("日", " ").strip()
    short = re.sub(r"(\d{1,2})月(\d{1,2})", r"\1-\2", short).strip()
    short = re.sub(r"^(\d{1,2})\.(\d{1,2})(\s|$)", r"\1-\2\3", short)
    if short != s:
        return parse_deadline(short)
    fmts = (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        "%Y-%m-%d", "%Y/%m/%d",
        "%m-%d %H:%M", "%m/%d %H:%M",
        "%m-%d", "%m/%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
            dt = dt.replace(hour=23, minute=59)
        if fmt in ("%m-%d %H:%M", "%m/%d %H:%M", "%m-%d", "%m/%d"):
            now = datetime.now()
            dt = dt.replace(year=now.year)
            if dt < now - timedelta(days=180):
                dt = dt.replace(year=now.year + 1)
        return dt
    return None


def merge_and_filter(data: dict, manual: list, days: int):
    """合并平台抓取（作业 + 在线测试）与手动录入，返回截止时间窗口内的条目。"""
    all_hw = list(data.get("homeworks", []))
    for t in data.get("tests", []) or []:      # 在线测试与作业同一套截止规则
        t.setdefault("kind", "test")
        all_hw.append(t)
    for m in manual:
        m.setdefault("source", "manual")
        m.setdefault("kind", "manual")
        all_hw.append(m)

    now = datetime.now()
    end = now + timedelta(days=days)
    picked = []
    for hw in all_hw:
        dt = parse_deadline(hw.get("deadline", ""))
        hw["_deadline_dt"] = dt
        if dt is None:
            # 截止时间无法解析：手动条目保留（标「未标注」），平台条目丢弃
            if hw.get("source") == "manual":
                picked.append(hw)
            continue
        # 平台作业和手动作业一视同仁：只保留时间窗口内的，过期自动掉出
        if now <= dt <= end:
            picked.append(hw)
    return picked


def format_message(items: list, days: int) -> str:
    """推送文案（B 方案：紧迫感分级）。

    - 头部一行：覆盖窗口，不带「生成时间」（对同学是噪音）
    - 按「近两天 / 本周稍后」分组，组为空则整组消失
    - 倒计时精度：今天/明天直接说「今天/明天 HH:mm 截止」；
      2 天内带小时，超过 2 天只报天数（没有「0 小时」「5 天 10 小时」这类精度表演）
    """
    now = datetime.now()
    end = now + timedelta(days=days)
    lines = [f"📚 作业提醒 · 未来 {days} 天（至 {end:%m-%d}）", ""]
    if not items:
        lines.append("✅ 这段时间没有待交作业，安心休息。")
        return "\n".join(lines)

    def mark_of(it) -> str:
        src = it.get("source") or "platform"
        kind = it.get("kind") or ("manual" if src == "manual" else "homework")
        if src == "manual":
            return " 📌手动"
        return " 📝在线测试" if kind == "test" else ""

    def deadline_line(it) -> str:
        dt = it.get("_deadline_dt")
        if not dt:
            return f"   ⏰ 截止 {it.get('deadline') or '未标注'}"
        delta = dt - now
        if delta.total_seconds() < 0:
            return f"   ⏰ {dt:%m-%d %H:%M} 截止（已逾期）"
        if dt.date() == now.date():
            return f"   ⏰ 今天 {dt:%H:%M} 截止"
        if dt.date() == (now + timedelta(days=1)).date():
            return f"   ⏰ 明天 {dt:%H:%M} 截止"
        d, h = delta.days, delta.seconds // 3600
        left = f"还剩 {d} 天" if (d > 2 or h == 0) else f"还剩 {d} 天 {h} 小时"
        return f"   ⏰ {dt:%m-%d %H:%M} 截止（{left}）"

    def block(it) -> list:
        blk = [f"• {it.get('course', '未命名课程')}{mark_of(it)}｜{it.get('title', '作业')}",
               deadline_line(it)]
        if it.get("note"):
            blk.append(f"   备注：{it['note']}")
        return blk

    picked = sorted(items, key=lambda x: (x.get("_deadline_dt") or datetime.max))
    near, later = [], []
    for it in picked:
        dt = it.get("_deadline_dt")
        (near if (dt is not None and (dt - now).days <= 2) else later).append(it)

    if near:
        lines.append("⚠️ 近两天截止")
        for it in near:
            lines.extend(block(it))
        lines.append("")
    if later:
        lines.append("🗓 本周稍后")
        for it in later:
            lines.extend(block(it))
        lines.append("")
    lines.append(f"—— 共 {len(picked)} 项，详情以「北化在线」为准")
    return "\n".join(lines).rstrip()


def ensure_login() -> bool:
    """抓取前保证有有效登录态。

    1. 先快速检查 storage_state；
    2. 失效 -> 调 `login.py --no-wait` 自动登录（Edge 自动填充 + 自动点登录，最多等 45 秒，
       不转人工，免得后台挂死）。实测本机 IP 不弹滑块，所以这一步通常能自愈；
    3. 仍失败 -> 返回 False，由调用方发告警让人工双击 login.bat。
    """
    r = subprocess.run(
        [PY, str(ROOT / "login.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV, cwd=str(ROOT),
    )
    if "LOGGED_IN" in (r.stdout or ""):
        return True

    print("登录态失效，尝试自动登录（最多 45 秒）...", file=sys.stderr)
    r2 = subprocess.run(
        [PY, str(ROOT / "login.py"), "--no-wait"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV, cwd=str(ROOT),
    )
    for line in (r2.stdout or "").splitlines():
        print("  " + line, file=sys.stderr)
    if r2.returncode == 0:
        print("自动登录成功，继续抓取。", file=sys.stderr)
        return True

    print("自动登录未成功，需人工双击 login.bat 重新登录。", file=sys.stderr)
    return False


def send_alert(text: str) -> bool:
    """把告警文本通过 notify.py 发出（默认 wecom 渠道）。"""
    f = ROOT / "_login_alert.txt"
    f.write_text(text, encoding="utf-8")
    r = subprocess.run(
        [PY, str(ROOT / "notify.py"), "--file", str(f)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV, cwd=str(ROOT),
    )
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="统计未来 N 天（默认 7）")
    ap.add_argument("--no-send", action="store_true", help="不推送，只打印")
    ap.add_argument("--channel", help="覆盖 config.json 的推送渠道")
    ap.add_argument("--pool", action="store_true",
                    help="旧行为：只用首页「待提交作业」视图的课程（快，但谁交了哪门课，"
                         "全班就收不到那门课的提醒）。默认关闭")
    args = ap.parse_args()

    # 默认按「截止日期」取作业（全量扫描所有课程，不看是否已提交）——
    # 这份提醒是发给全班同学的，谁交没交不该决定某门课要不要提醒。
    extra = [] if args.pool else ["--deadline-only"]

    print("抓取中..." + ("（全量扫描所有课程，约 1-3 分钟）" if not args.pool else ""))
    if not ensure_login():
        alert = ("⚠️ 北化在线作业提醒：平台登录态已失效，且自动登录未成功"
                 "（可能平台要求验证码，或 Edge 里存的密码已过期）。"
                 f"请双击 {ROOT.parent / 'login.bat'} 手动登录一次后，再运行作业提醒。")
        print(alert)
        try:
            ok = send_alert(alert)
            print("已发送登录失效告警。" if ok else "告警发送失败（见 stderr）。", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print("告警发送异常:", e, file=sys.stderr)
        return 1

    data = fetch(extra or None)
    elapsed = data.get("_elapsed_sec")
    platform = data.get("homeworks", [])
    tests_n = len(data.get("tests", []) or [])
    print(f"平台抓到 {len(platform)} 条作业记录"
          + (f" + {tests_n} 条在线测试" if tests_n else "")
          + ("（按截止日期，含我已提交的）" if not args.pool else "（首页待提交池）"))
    if elapsed is not None:
        mins = elapsed / 60
        print(f"⏱ 本次抓取用时 {mins:.1f} 分钟（{elapsed:.0f} 秒）"
              f"（其中含在线测试扫描）")
    warn = data.get("warnings") or {}
    if warn.get("failed_courses"):
        print(f"⚠️ {len(warn['failed_courses'])} 门课抓取失败，覆盖不完整："
              f"{', '.join(warn['failed_courses'][:5])}", file=sys.stderr)

    manual = load_manual()
    if manual:
        print(f"手动录入 {len(manual)} 条待办")

    picked = merge_and_filter(data, manual, args.days)
    print(f"未来 {args.days} 天内待交：{len(picked)} 条")

    msg = format_message(picked, args.days)
    print("-" * 40)
    print(msg)
    print("-" * 40)

    msg_file = ROOT / "last_message.txt"
    msg_file.write_text(msg, encoding="utf-8")
    print("已落盘:", msg_file)

    # 检索台账：定时任务（本脚本）也要记账，与控制台的 hub-list/publish 口径一致。
    # 这样「这周的推送到底是抓到了什么、有没有失败课程、复用了哪份数据」都有据可查。
    try:
        import fetch_log
        fetch_log.log_fetch(
            "weekly", args.days, mode=("pool" if args.pool else "deadline-only"),
            rows=len(picked), rows_total=len(platform),
            platform=len(platform), tests=tests_n, manual=len(manual),
            elapsed=elapsed, failed_courses=warn.get("failed_courses") or [],
            notes=("演练模式（未发送）" if args.no_send else ""),
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写检索台账失败 {e}", file=sys.stderr)

    if args.no_send:
        print("演练模式：未发送。")
        return 0

    cmd = [PY, str(ROOT / "notify.py"), "--file", str(msg_file), "--source", "weekly"]
    if args.channel:
        cmd += ["--channel", args.channel]
    r = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=UTF8_ENV,
    )
    print(r.stdout or "", end="")
    if r.returncode != 0:
        print("推送失败:", r.stderr or "", file=sys.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
