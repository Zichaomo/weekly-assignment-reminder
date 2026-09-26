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
    """
    cmd = [PY, str(ROOT / "scrape.py")] + list(extra_args or [])
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=UTF8_ENV,
        cwd=str(ROOT),
    )
    if out.returncode != 0:
        raise RuntimeError(f"抓取失败:\n{out.stdout}\n{out.stderr}")
    if not out.stdout:
        raise RuntimeError(f"抓取无输出:\n{out.stderr}")
    return json.loads(out.stdout)


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
    """合并平台抓取与手动录入，标出截止时间，返回时间窗口内的条目。"""
    all_hw = list(data.get("homeworks", []))
    for m in manual:
        m.setdefault("source", "manual")
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
    now = datetime.now()
    end = now + timedelta(days=days)
    lines = [
        f"📚 作业提醒（{now:%Y-%m-%d} 生成）",
        f"覆盖未来 {days} 天，至 {end:%m-%d}",
        "",
    ]
    if not items:
        lines.append("✅ 这段时间没有待交作业，安心休息。")
        return "\n".join(lines)

    items = sorted(items, key=lambda x: (x.get("_deadline_dt") or datetime.max))
    for it in items:
        dl = it.get("deadline") or "未标注"
        left = ""
        dt = it.get("_deadline_dt")
        if dt:
            delta = dt - now
            d = delta.days
            h = delta.seconds // 3600
            left = f"（还剩 {d} 天 {h} 小时）" if d >= 0 else "（已逾期）"
        src = it.get("source") or "platform"
        mark = " 📌手动" if src == "manual" else ""
        lines.append(f"• {it.get('course', '未命名课程')}{mark}")
        lines.append(f"   {it.get('title', '作业')}")
        lines.append(f"   ⏰ 截止 {dl} {left}".rstrip())
        if it.get("note"):
            lines.append(f"   备注：{it['note']}")
        lines.append("")
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
    args = ap.parse_args()

    print("抓取中...")
    if not ensure_login():
        alert = ("⚠️ 北化在线作业提醒：平台登录态已失效，且自动登录未成功"
                 "（可能平台要求验证码，或 Edge 里存的密码已过期）。"
                 "请双击 D:\\项目\\每周作业提醒\\login.bat 手动登录一次后，再运行作业提醒。")
        print(alert)
        try:
            ok = send_alert(alert)
            print("已发送登录失效告警。" if ok else "告警发送失败（见 stderr）。", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print("告警发送异常:", e, file=sys.stderr)
        return 1

    data = fetch()
    platform = data.get("homeworks", [])
    print(f"平台抓到 {len(platform)} 条作业记录")

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

    if args.no_send:
        print("演练模式：未发送。")
        return 0

    cmd = [PY, str(ROOT / "notify.py"), "--file", str(msg_file)]
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
