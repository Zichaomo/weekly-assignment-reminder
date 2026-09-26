# -*- coding: utf-8 -*-
"""
统一控制台后端：给 PowerShell 图形界面（console.ps1）提供数据（列表）与动作（发布）。

复用 weekly.py 的抓取 / 合并 / 筛选 / 排版逻辑，以及 notify.py 的推送逻辑，
避免在界面里重复实现同一套规则。

子命令:
    python hub.py list [--days 7] [--fetch] [--all] [--with-content]
        -> 输出 JSON 对象 {ok, error, rows:[...], platform_count, manual_count}
        rows 每项: source(平台/手动), course, title, deadline, note, left, status, manual_id
        --with-content: 连作业正文一起抓（写进 content 字段）。
            注意：列表里显示的是「标题」，不是内容；正文常常是图片，
            所以界面暂不展示 content 列，此参数留给以后的「内容展示」功能。

    python hub.py publish [--days 7] [--no-send] [--channel wecom]
        -> 抓取 + 合并 + 排版 + (推送)，输出 JSON 对象
        {ok, sent, message, count, channel, error?}

    python hub.py content --hwtid <id>
        -> 登录北化在线，抓取某条平台作业的详情正文，输出 JSON 对象
        {ok, hwtid, url, content_text, content_html, error?}
        content_text 是可读多行文本；content_url/submit_url/result_url 由 list 的行带上。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 强制本进程以 utf-8 输出，保证被 PowerShell 以 UTF8 读回时一致
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import weekly
import notify

MANUAL_FILE = ROOT / "manual.json"


def load_manual_raw() -> list:
    """读取 manual.json 全部条目（含 done / id），供列表展示与管理用。"""
    if not MANUAL_FILE.exists():
        return []
    try:
        data = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def _left_text(dt: datetime) -> str:
    if dt is None:
        return "未标注"
    delta = dt - datetime.now()
    if delta.total_seconds() < 0:
        return "已逾期"
    d, h = delta.days, delta.seconds // 3600
    return f"还剩 {d} 天 {h} 小时"


def build_rows(platform_hw: list, manual_items: list, days: int) -> list:
    now = datetime.now()
    end = now + timedelta(days=days)
    rows = []

    for hw in platform_hw:
        dt = weekly.parse_deadline(hw.get("deadline", ""))
        if dt is None or not (now <= dt <= end):
            continue
        rows.append({
            "source": "平台",
            "course": hw.get("course", ""),
            "title": hw.get("title", ""),
            "deadline": hw.get("deadline", ""),
            "note": hw.get("note", ""),
            "left": _left_text(dt),
            "status": "待发布",
            "manual_id": None,
            "hwtid": hw.get("hwtid"),
            "content_url": hw.get("content_url"),
            "submit_url": hw.get("submit_url"),
            "result_url": hw.get("result_url"),
            "_dt": dt,
        })

    for it in manual_items:
        if it.get("done"):
            continue
        dt = weekly.parse_deadline(it.get("deadline", ""))
        in_window = dt is not None and (now <= dt <= end)
        if dt is None:
            status = "待办"
        elif dt < now:
            status = "逾期"
        elif in_window:
            status = "待发布"
        else:
            status = "待办"
        rows.append({
            "source": "手动",
            "course": it.get("course", ""),
            "title": it.get("title", ""),
            "deadline": it.get("deadline", ""),
            "note": it.get("note", ""),
            "content": it.get("note", ""),         # 手动条目没有平台正文，备注即补充内容
            "left": _left_text(dt),
            "status": status,
            "manual_id": it.get("id"),
            "_dt": dt,
        })

    rows.sort(key=lambda r: (r["_dt"] is None, r["_dt"] or datetime.max))
    for r in rows:
        r.pop("_dt", None)
    return rows


def run_list(days: int, fetch: bool, with_content: bool = False):
    platform_hw = []
    error = None
    if fetch:
        try:
            data = weekly.fetch(["--with-content"] if with_content else None)
            platform_hw = data.get("homeworks", [])
        except Exception as e:  # noqa: BLE001
            error = f"北化在线抓取失败（会话可能过期，请运行 login.py）：{e}"
    manual_items = load_manual_raw()
    rows = build_rows(platform_hw, manual_items, days)
    out = {
        "ok": error is None,
        "error": error,
        "rows": rows,
        "platform_count": len(platform_hw),
        "manual_count": len(manual_items),
    }
    print(json.dumps(out, ensure_ascii=False))


def run_publish(days: int, no_send: bool, channel: str):
    try:
        data = weekly.fetch()
        platform_hw = data.get("homeworks", [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"抓取失败：{e}"}

    manual = weekly.load_manual()
    picked = weekly.merge_and_filter(data, manual, days)
    msg = weekly.format_message(picked, days)
    (ROOT / "last_message.txt").write_text(msg, encoding="utf-8")

    if no_send:
        return {
            "ok": True, "sent": False, "message": msg,
            "count": len(picked), "channel": channel or "默认",
        }

    cfg = notify.load_config()
    ch = (channel or cfg.get("channel", "clipboard")).lower()
    try:
        sent = notify.dispatch(msg, ch, cfg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"推送异常：{e}", "message": msg}
    return {
        "ok": sent, "sent": sent, "message": msg,
        "count": len(picked), "channel": ch,
    }


def run_content(hwtid: str):
    """登录北化在线并抓取某条作业的正文，返回 JSON。"""
    import scrape
    with scrape.sync_playwright() as p:
        ctx = scrape.make_context(p, headless=True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        page.goto(scrape.SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        if not scrape.logged_in(page, ctx):
            return {"ok": False, "error": "未登录或会话已过期，请运行 login.py"}
        res = scrape.get_hw_content(ctx, hwtid)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="输出合并后的作业列表 JSON")
    p_list.add_argument("--days", type=int, default=7)
    p_list.add_argument("--all", action="store_true", help="显示全部平台作业（含已过期），忽略天数窗口")
    p_list.add_argument("--fetch", action="store_true", help="同时抓取北化在线平台作业")
    p_list.add_argument("--with-content", action="store_true",
                        help="检索时连作业正文一起抓（每条多约 2-3 秒）")

    p_pub = sub.add_parser("publish", help="抓取+合并+排版+(推送)")
    p_pub.add_argument("--days", type=int, default=7)
    p_pub.add_argument("--no-send", action="store_true", help="只生成预览，不推送")
    p_pub.add_argument("--channel", help="覆盖 config.json 的 channel")

    p_ct = sub.add_parser("content", help="抓取某条平台作业的详情正文")
    p_ct.add_argument("--hwtid", required=True, help="作业 id（列表里 platform 行的 hwtid）")

    args = ap.parse_args()
    if args.cmd == "list":
        run_list(9999 if args.all else args.days, args.fetch, args.with_content)
    elif args.cmd == "publish":
        print(json.dumps(
            run_publish(args.days, args.no_send, args.channel),
            ensure_ascii=False,
        ))
    elif args.cmd == "content":
        print(json.dumps(run_content(args.hwtid), ensure_ascii=False))


if __name__ == "__main__":
    main()
