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
import fetch_log

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
        kind = hw.get("kind") or "homework"
        rows.append({
            "source": "平台",
            "kind": kind,
            "course": hw.get("course", ""),
            "title": hw.get("title", ""),
            "deadline": hw.get("deadline", ""),
            "note": hw.get("note", ""),
            "left": _left_text(dt),
            "status": "待发布",
            "manual_id": None,
            "hwtid": hw.get("hwtid"),
            "test_id": hw.get("test_id"),
            "submit_state": hw.get("submit_state"),
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


def run_list(days: int, fetch: bool, with_content: bool = False, sweep: bool = False,
             pool: bool = False, no_tests: bool = False):
    platform_hw = []
    error = None
    warnings = {}
    sweep_meta = None
    elapsed = None
    timing = {}
    if fetch:
        try:
            extra = []
            if no_tests:
                extra.append("--no-tests")
            if with_content:
                extra.append("--with-content")
            if pool:
                # 旧行为：只用首页待提交池
                if sweep:
                    extra.append("--sweep")
            else:
                # 默认按截止日期取数（发给全班，不看提交状态），必须全量扫描
                extra.append("--deadline-only")
            data = weekly.fetch(extra or None)
            platform_hw = list(data.get("homeworks", []))
            for t in (data.get("tests") or []):      # 在线测试同一套规则
                t.setdefault("kind", "test")
                platform_hw.append(t)
            warnings = data.get("warnings", {}) or {}
            sweep_meta = data.get("sweep")
            elapsed = data.get("_elapsed_sec")
            timing = data.get("timing") or {}
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
        "warnings": warnings,
        "sweep": sweep_meta,
        "elapsed_sec": elapsed,
        "elapsed_human": (f"{elapsed / 60:.1f} 分钟" if elapsed and elapsed >= 60
                          else (f"{elapsed:.0f} 秒" if elapsed else None)),
        "timing": timing,
        # 返回原始抓取结果，供 server.py 缓存后给「预览/提前发布」复用，避免重复抓取。
        # 只在本次真的抓了（fetch 且成功）时带出；它是内部字段，前端可忽略。
        "_payload": data if fetch and error is None else None,
        "data_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S") if fetch and error is None else None,
    }
    # 检索台账：记下「这次抓了什么」——多少条、多久、失败几门、是否复用缓存。
    # 只有真的抓了才记（fetch=False 是纯读本地列表，无需记账）。
    if fetch:
        fw = (warnings or {}).get("failed_courses") or []
        fetch_log.log_fetch("hub-list", days, mode=("pool" if pool else "deadline-only"),
                  rows=len(rows), rows_total=len(platform_hw), platform=len(platform_hw),
                  tests=sum(1 for x in platform_hw if x.get("kind") == "test"),
                  manual=len(manual_items), elapsed=elapsed, ok=error is None,
                  error=error or "", failed_courses=fw)
    return out


def run_refresh_courses():
    """刷新本地课程索引（我的课程总表 -> courses.json）。"""
    import scrape
    with scrape.sync_playwright() as p:
        ctx = scrape.make_context(p, headless=True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        page.goto(scrape.SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        if not scrape.logged_in(page, ctx):
            return {"ok": False, "error": "未登录或会话已过期，请运行 login.py"}
        try:
            index = scrape.collect_all_courses(page)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"拉取课程总表失败：{e}"}
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    if not index:
        return {"ok": False, "error": "课程总表解析为空（平台结构可能变了）"}
    # 保留已有的每课元数据（最近作业时间 / 失败次数 / 空课轮数）。
    # 曾经这里整体覆盖，导致点一次「刷新课程表」就把黑名单候选的证据清空，
    # 界面上候选与「暂无作业」全部变 0（用户实测遇到）。
    old_meta = scrape.load_index_meta()
    scrape.COURSES_FILE.write_text(
        json.dumps({
            "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": scrape.ALL_COURSES_URL,
            "courses": index,
            "meta": old_meta,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "count": len(index), "file": str(scrape.COURSES_FILE)}


def _data_source_fields(source: str, age_sec):
    """给返回值标注「这次用的数据是刚抓的还是复用的」。"""
    if source == "cache":
        age = float(age_sec or 0)
        human = f"{age / 60:.1f} 分钟" if age >= 60 else f"{age:.0f} 秒"
        return {
            "data_source": "cache",
            "data_age_sec": round(age, 1),
            "data_note": f"复用了 {human}前的检索结果，未重复抓取",
        }
    return {"data_source": "fresh", "data_age_sec": 0.0,
            "data_note": "本次重新抓取了平台数据"}


def run_publish(days: int, no_send: bool, channel: str, sweep: bool = False,
                pool: bool = False, prefetched: dict = None,
                prefetched_age_sec: float = None):
    try:
        if prefetched:
            # 复用最近一次抓取的结果：不再重复全量扫描（省 1-2 分钟）
            data = prefetched
            data_source = "cache"
            _reuse_payload = None
        else:
            if pool:
                data = weekly.fetch(["--sweep"] if sweep else None)
            else:
                data = weekly.fetch(["--deadline-only"])
            data_source = "fresh"
            _reuse_payload = data      # 交给上层写缓存，让随后的发布/预览能复用
        platform_hw = list(data.get("homeworks", []))
        for t in (data.get("tests") or []):          # 在线测试同一套规则
            t.setdefault("kind", "test")
            platform_hw.append(t)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"抓取失败：{e}"}

    manual = weekly.load_manual()
    picked = weekly.merge_and_filter(data, manual, days)
    msg = weekly.format_message(picked, days)
    (ROOT / "last_message.txt").write_text(msg, encoding="utf-8")

    # 检索台账：预览/发布走的是同一段抓取，也要记账（区分是预览还是发布）
    _src = ("hub-preview" if no_send else "hub-publish")
    _fw = (data.get("warnings") or {}).get("failed_courses") or []
    _el = data.get("_elapsed_sec")
    _cached = (data_source == "cache")
    fetch_log.log_fetch(_src, days, mode=("pool" if pool else "deadline-only"),
              rows=len(picked), rows_total=len(platform_hw), platform=len(platform_hw),
              tests=sum(1 for x in platform_hw if x.get("kind") == "test"),
              manual=len(manual), elapsed=_el, cached=_cached,
              data_age=(prefetched_age_sec if _cached else None),
              failed_courses=_fw,
              notes=("复用缓存" if _cached else "重新抓取"))

    if no_send:
        out = {
            "ok": True, "sent": False, "message": msg,
            "count": len(picked), "channel": channel or "默认",
        }
        out.update(_data_source_fields(data_source, prefetched_age_sec))
        # 带上本次抓取的原始数据（若为复用则为 None），供 server 写缓存
        out["_payload"] = _reuse_payload
        out["data_fetched_at"] = (
            time.strftime("%Y-%m-%d %H:%M:%S") if _reuse_payload else None)
        return out

    cfg = notify.load_config()
    ch = (channel or cfg.get("channel", "clipboard")).lower()

    # 去重：同一目标上、窗口内推过完全一样的内容就跳过
    # （防止手滑连点两次「提前发布」重复打扰；按目标区分，测试群发过不影响班级群）
    window = int(cfg.get("dedup_window_sec", 300) or 0)
    target = notify.dedup_target(ch, cfg)
    dup = notify.recent_duplicate(msg, window, target)
    if dup:
        note = (f"{window} 秒内已向同一目标推送过完全相同的内容"
                f"（{dup.get('time')}，来源 {dup.get('source')}），本次已跳过，未重复发送。")
        notify.log_push("hub", ch, msg, True, note="duplicate-skipped", target=target)
        return {
            "ok": True, "sent": False, "deduped": True, "note": note,
            "message": msg, "count": len(picked), "channel": ch,
        }

    try:
        sent = notify.dispatch(msg, ch, cfg)
    except Exception as e:  # noqa: BLE001
        notify.log_push("hub", ch, msg, False,
                        note=f"exception:{type(e).__name__}", target=target)
        return {"ok": False, "error": f"推送异常：{e}", "message": msg}
    notify.log_push("hub", ch, msg, sent,
                    note="" if sent else "send-failed", target=target)
    out = {
        "ok": sent, "sent": sent, "message": msg,
        "count": len(picked), "channel": ch,
    }
    out.update(_data_source_fields(data_source, prefetched_age_sec))
    out["_payload"] = _reuse_payload
    out["data_fetched_at"] = (
        time.strftime("%Y-%m-%d %H:%M:%S") if _reuse_payload else None)
    return out


def run_blacklist_list():
    """列出黑名单与建议候选（只读）。"""
    import scrape
    info = scrape.build_blacklist_candidates()
    return {
        "ok": True,
        "semester_start": info["semester_start"],
        "total_courses": info["total_courses"],
        "candidates": info["candidates"],
        "blacklist": [
            {"lid": lid, "name": v.get("name"), "reason": v.get("reason"),
             "detail": v.get("detail"), "confirmed_at": v.get("confirmed_at")}
            for lid, v in (info["blacklist"] or {}).items()
        ],
    }


def run_blacklist_apply(lids, reason="manual"):
    """把指定课程写入黑名单（由人工在界面上确认后调用）。"""
    import scrape
    index = scrape.load_course_index()
    data = scrape.load_blacklist()
    added, unknown = [], []
    for lid in lids:
        lid = str(lid)
        if lid not in index:
            unknown.append(lid)
            continue
        if lid in data["courses"]:
            continue
        cand = next((c for c in scrape.build_blacklist_candidates()["candidates"]
                     if c["lid"] == lid), None)
        data["courses"][lid] = {
            "name": index[lid],
            "reason": (cand or {}).get("reason", reason),
            "detail": (cand or {}).get("detail", "人工确认拉黑"),
            "last_homework": (cand or {}).get("last_homework"),
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": "human",
        }
        added.append({"lid": lid, "name": index[lid]})
    scrape.save_blacklist(data)
    return {"ok": True, "added": added, "unknown": unknown,
            "total": len(data["courses"])}


def run_blacklist_elective(tokens):
    """按「选修课」拉黑：每人选的课不同，这门课的作业对全班没意义。

    与候选确认的区别：候选是程序按证据推导的（陈旧 / 无入口 / 暂无作业），
    而选修课是**用户主观声明**「这门课只有我（少数人）上」，跟作业有没有、早不早无关。
    所以单独一个入口，reason 记 elective，界面上也能分开看。

    tokens 可以是 lid，也可以是课程名的一部分（唯一匹配时才生效）。
    """
    import scrape
    index = scrape.load_course_index()
    data = scrape.load_blacklist()
    added, unknown = [], []
    for token in tokens:
        lid = scrape.resolve_lid(str(token), index)
        if not lid:
            unknown.append(str(token))
            continue
        if lid in data["courses"]:
            continue
        data["courses"][lid] = {
            "name": index[lid],
            "reason": "elective",
            "detail": "选修课（每人选课不同，作业不影响全班）",
            "last_homework": None,
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": "human",
        }
        added.append({"lid": lid, "name": index[lid]})
    scrape.save_blacklist(data)
    return {"ok": True, "added": added, "unknown": unknown,
            "total": len(data["courses"])}


def run_blacklist_remove(lids):
    import scrape
    data = scrape.load_blacklist()
    removed = []
    for lid in lids:
        lid = str(lid)
        if lid in data["courses"]:
            removed.append({"lid": lid, "name": data["courses"][lid].get("name")})
            data["courses"].pop(lid)
    scrape.save_blacklist(data)
    return {"ok": True, "removed": removed, "total": len(data["courses"])}


# ---------------------------------------------------------------- 测试黑名单
def run_test_blacklist_list():
    """列出「只屏蔽在线测试」的课（作业照常抓）。"""
    import scrape
    index = scrape.load_course_index()
    data = scrape.load_test_blacklist().get("courses", {})
    return {
        "ok": True,
        "total": len(data),
        "blacklist": [
            {"lid": lid, "name": v.get("name", index.get(lid, "?")),
             "note": v.get("note", ""), "confirmed_at": v.get("confirmed_at")}
            for lid, v in data.items()
        ],
    }


def run_test_blacklist_apply(lids):
    import scrape
    index = scrape.load_course_index()
    data = scrape.load_test_blacklist()
    added, unknown = [], []
    for lid in lids:
        lid = str(lid)
        if lid not in index:
            unknown.append(lid)
            continue
        if lid in data["courses"]:
            continue
        data["courses"][lid] = {
            "name": index[lid],
            "note": "只屏蔽在线测试，作业照常搜",
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": "human",
        }
        added.append({"lid": lid, "name": index[lid]})
    scrape.save_test_blacklist(data)
    return {"ok": True, "added": added, "unknown": unknown, "total": len(data["courses"])}


def run_test_blacklist_remove(lids):
    import scrape
    data = scrape.load_test_blacklist()
    removed = []
    for lid in lids:
        lid = str(lid)
        if lid in data["courses"]:
            removed.append({"lid": lid, "name": data["courses"][lid].get("name")})
            data["courses"].pop(lid)
    scrape.save_test_blacklist(data)
    return {"ok": True, "removed": removed, "total": len(data["courses"])}


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
    p_list.add_argument("--pool", action="store_true",
                        help="旧行为：只用首页「待提交作业」视图（快，但会漏掉我已交、同学还没交的课）")
    p_list.add_argument("--sweep", action="store_true",
                        help="配合 --pool 使用：在全量里再算一遍首页漏报的课")
    p_list.add_argument("--no-tests", action="store_true",
                        help="不抓在线测试（省一半时间）；透传给 scrape.py，旧 console.ps1 就在传这个参数")

    p_pub = sub.add_parser("publish", help="抓取+合并+排版+(推送)")
    p_pub.add_argument("--days", type=int, default=7)
    p_pub.add_argument("--no-send", action="store_true", help="只生成预览，不推送")
    p_pub.add_argument("--channel", help="覆盖 config.json 的 channel")
    p_pub.add_argument("--pool", action="store_true", help="旧行为：只用首页待提交池")
    p_pub.add_argument("--sweep", action="store_true", help="配合 --pool：额外做全量扫描")

    p_ref = sub.add_parser("refresh-courses", help="刷新本地课程索引 courses.json")

    p_bl = sub.add_parser("blacklist-list", help="列出课程黑名单与建议候选（只读）")
    p_bla = sub.add_parser("blacklist-apply", help="把课程写入黑名单（人工确认后调用）")
    p_bla.add_argument("--lids", nargs="+", required=True)
    p_blr = sub.add_parser("blacklist-remove", help="把课程移出黑名单")
    p_blr.add_argument("--lids", nargs="+", required=True)

    sub.add_parser("test-blacklist-list", help="列出「只屏蔽在线测试」的课（作业照抓）")
    p_tbl = sub.add_parser("test-blacklist-apply", help="把课程加入测试黑名单（人工确认后调用）")
    p_tbl.add_argument("--lids", nargs="+", required=True)
    p_tblr = sub.add_parser("test-blacklist-remove", help="把课程移出测试黑名单")
    p_tblr.add_argument("--lids", nargs="+", required=True)

    p_ct = sub.add_parser("content", help="抓取某条平台作业的详情正文")
    p_ct.add_argument("--hwtid", required=True, help="作业 id（列表里 platform 行的 hwtid）")

    args = ap.parse_args()
    if args.cmd == "list":
        run_list(9999 if args.all else args.days, args.fetch, args.with_content,
                 args.sweep, args.pool, no_tests=args.no_tests)
    elif args.cmd == "publish":
        print(json.dumps(
            run_publish(args.days, args.no_send, args.channel, args.sweep, args.pool),
            ensure_ascii=False,
        ))
    elif args.cmd == "refresh-courses":
        print(json.dumps(run_refresh_courses(), ensure_ascii=False))
    elif args.cmd == "blacklist-list":
        print(json.dumps(run_blacklist_list(), ensure_ascii=False))
    elif args.cmd == "blacklist-apply":
        print(json.dumps(run_blacklist_apply(args.lids), ensure_ascii=False))
    elif args.cmd == "blacklist-remove":
        print(json.dumps(run_blacklist_remove(args.lids), ensure_ascii=False))
    elif args.cmd == "test-blacklist-list":
        print(json.dumps(run_test_blacklist_list(), ensure_ascii=False))
    elif args.cmd == "test-blacklist-apply":
        print(json.dumps(run_test_blacklist_apply(args.lids), ensure_ascii=False))
    elif args.cmd == "test-blacklist-remove":
        print(json.dumps(run_test_blacklist_remove(args.lids), ensure_ascii=False))
    elif args.cmd == "content":
        print(json.dumps(run_content(args.hwtid), ensure_ascii=False))


if __name__ == "__main__":
    main()
