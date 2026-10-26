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
import cache_store
import content_pipeline
from platform_lock import FetchLock, LockTimeout

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


def build_rows(platform_hw: list, manual_items: list, days: int,
               include_expired: bool = False) -> list:
    now = datetime.now()
    end = now + timedelta(days=days)
    rows = []

    for hw in platform_hw:
        dt = weekly.parse_deadline(hw.get("deadline", ""))
        if dt is None or dt > end or (dt < now and not include_expired):
            continue
        kind = hw.get("kind") or "homework"
        rows.append({
            "source": "平台",
            "kind": kind,
            "lid": hw.get("lid"),
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
            "item_key": hw.get("item_key"),
            "platform_id": hw.get("platform_id"),
            "content_status": hw.get("content_status"),
            "content_ready": bool(hw.get("content_ready")),
            "content_stale": bool(hw.get("content_stale")),
            "content_checked_at": hw.get("content_checked_at"),
            "content_error": hw.get("content_error"),
            "content_text": hw.get("content_text") or "",
            "content_blocks": hw.get("content_blocks") or [],
            "asset_ids": hw.get("asset_ids") or [],
            "content_version": hw.get("source_version"),
            "source_version": hw.get("source_version"),
            "content_revision": hw.get("content_revision"),
            "view": hw.get("view"),
            "stale_view": hw.get("stale_view"),
            "_dt": dt,
        })

    for it in manual_items:
        if it.get("done"):
            continue
        dt = weekly.parse_deadline(it.get("deadline", ""))
        in_window = dt is not None and dt <= end and (include_expired or dt >= now)
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
            "item_key": f"manual:{it.get('id')}",
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
             pool: bool = False, no_tests: bool = False,
             include_expired: bool = False, lids=None, scan_id: str | None = None):
    t_start = time.perf_counter()
    platform_hw = []
    error = None
    warnings = {}
    sweep_meta = None
    elapsed = None
    timing = {}
    data = None
    scope = cache_store.account_scope()
    if fetch:
        try:
            extra = []
            if no_tests:
                extra.append("--no-tests")
            if with_content:
                extra.append("--with-content")
            if scan_id:
                extra.extend(["--scan-id", str(scan_id)])
            if pool:
                # 旧行为：只用首页待提交池
                if sweep:
                    extra.append("--sweep")
            else:
                # 默认按截止日期取数（发给全班，不看提交状态），必须全量扫描
                extra.append("--deadline-only")
                if not with_content:
                    extra.append("--content-after-scan")
                if lids:
                    extra.extend(["--lids", *[str(x) for x in lids]])
            data = weekly.fetch(extra or None)
            warnings = data.get("warnings", {}) or {}
            sweep_meta = data.get("sweep")
            elapsed = data.get("_elapsed_sec")
            timing = data.get("timing") or {}
        except Exception as e:  # noqa: BLE001
            error = f"北化在线抓取失败（会话可能过期，请运行 login.py）：{e}"
            lowered = str(e).lower()
            if any(x in str(e) for x in ("未登录", "登录", "认证", "session")) or \
                    any(x in lowered for x in ("login", "auth")):
                cache_store.invalidate_scope_scans(
                    "抓取在开始记录扫描前认证失败；旧成功快照禁止冒充最新结果")
    # 列表始终优先读取 SQLite：扫描过程中每门课事务写入，本地读取无需平台。
    t_local = time.perf_counter()
    try:
        import scrape
        blocked_now = scrape.blacklisted_lids()
        tests_blocked_now = scrape.test_blacklisted_lids()
    except Exception:  # noqa: BLE001
        blocked_now, tests_blocked_now = set(), set()
    platform_hw = cache_store.load_items(
        scope, exclude_lids=blocked_now, exclude_test_lids=tests_blocked_now)
    if fetch and error is None and not platform_hw:
        platform_hw = list((data or {}).get("homeworks", []))
        for t in ((data or {}).get("tests") or []):
            t.setdefault("kind", "test")
            platform_hw.append(t)
    manual_items = load_manual_raw()
    rows = build_rows(platform_hw, manual_items, days, include_expired=include_expired)
    first_screen_ms = round((time.perf_counter() - t_local) * 1000, 1)
    # 给每行打上「是不是选修课」：界面据此显示徽标，也据此知道
    # 这行不会出现在班级群那条提醒里（只出现在个人小群那条）。
    try:
        import scrape
        _elec = scrape.elective_lids()
    except Exception:  # noqa: BLE001
        _elec = set()
    for r in rows:
        r["elective"] = bool(r.get("lid") and str(r["lid"]) in _elec)
    try:
        import scrape
        index = scrape.load_course_index()
        expected = [lid for lid in index if lid not in scrape.blacklisted_lids()]
        tests_blocked = scrape.test_blacklisted_lids()
        index_version = cache_store.version_hash(index) if index else ""
        latest = cache_store.latest_state(scope).get("scan") or {}
        tests_requested = bool(latest.get("tests_requested", not no_tests))
        cov = cache_store.coverage(
            scope, expected_lids=expected, tests_requested=tests_requested,
            tests_blocked_lids=tests_blocked,
            current_index_version=index_version,
            max_age_sec=cache_store.cache_settings()["publish_fresh_sec"])
    except Exception as exc:  # noqa: BLE001
        cov = {"ok": False, "publishable": False,
               "publish_block_reasons": [f"读取缓存覆盖失败：{exc}"]}
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
        "cache": cov,
        "scan_id": (cov.get("scan") or {}).get("scan_id") if isinstance(cov, dict) else None,
        "data_source": (("fresh" if (cov or {}).get("publishable") else "fresh-partial")
                        if fetch and error is None else "cache"),
        "data_fetched_at": cov.get("updated_at"),
        "cache_age_sec": cov.get("age_sec"),
        "content_stats": (data or {}).get("content_stats"),
    }
    # 检索台账：记下「这次抓了什么」——多少条、多久、失败几门、是否复用缓存。
    # 只有真的抓了才记（fetch=False 是纯读本地列表，无需记账）。
    if fetch:
        fw = (warnings or {}).get("failed_courses") or []
        fetch_log.log_fetch("hub-list", days, mode=("pool" if pool else "deadline-only"),
                  rows=len(rows), rows_total=len(platform_hw), platform=len(platform_hw),
                  tests=sum(1 for x in platform_hw if x.get("kind") == "test"),
                  manual=len(manual_items), elapsed=elapsed, ok=error is None,
                  cached=False, cache_hit=False, first_screen_ms=first_screen_ms,
                  content_requested=((data or {}).get("content_stats") or {}).get("requested"),
                  content_ok=((data or {}).get("content_stats") or {}).get("ok"),
                  content_failed=((data or {}).get("content_stats") or {}).get("failed"),
                  error=error or "", failed_courses=fw)
    out["first_screen_ms"] = first_screen_ms
    out["local_read_ms"] = first_screen_ms
    out["request_total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    return out


def run_refresh_courses():
    """刷新本地课程索引（我的课程总表 -> courses.json）。"""
    import scrape
    try:
        with FetchLock(wait_sec=120):
            with scrape.sync_playwright() as p:
                ctx = scrape.make_context(p, headless=True)
                page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                try:
                    page.goto(scrape.SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
                    if not scrape.wait_authenticated(page, ctx, timeout=12):
                        return {"ok": False, "error": "未登录或会话已过期，请运行 login.py"}
                    try:
                        scrape.bind_page_identity(page)
                    except cache_store.AccountIdentityConflict as exc:
                        cache_store.invalidate_scope_scans(str(exc))
                        return {"ok": False, "error": str(exc)}
                    try:
                        index = scrape.collect_all_courses(page)
                    except Exception as e:  # noqa: BLE001
                        return {"ok": False, "error": f"拉取课程总表失败：{e}"}
                finally:
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
    except LockTimeout as exc:
        return {"ok": False, "error": str(exc)}
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
    if source == "stale-cache":
        age = float(age_sec or 0)
        human = f"{age / 60:.1f} 分钟" if age >= 60 else f"{age:.0f} 秒"
        return {
            "data_source": "stale-cache",
            "data_age_sec": round(age, 1),
            "data_note": f"本地缓存已 {human}未更新或覆盖不完整；本次仅供预览，未访问平台",
        }
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
    coverage = None
    try:
        if pool:
            # Legacy pool mode is intentionally kept for the old CLI.  It is
            # not allowed to claim the complete-cache publication guarantee.
            data = weekly.fetch(["--sweep"] if sweep else None)
            data_source = "fresh"
        else:
            data, coverage = weekly.load_fresh_publish_cache()
            if data:
                data_source = "cache"
                prefetched_age_sec = coverage.get("age_sec")
            elif not no_send:
                reasons = (coverage or {}).get("publish_block_reasons") or ["缓存不存在"]
                return {
                    "ok": False, "sent": False, "cache_blocked": True,
                    "error": "列表缓存未通过完整性与 5 分钟新鲜度校验，已拒绝发布："
                             + "；".join(reasons),
                    "coverage": coverage, "count": 0, "count_personal": 0,
                }
            else:
                # Preview is allowed to show the last local snapshot, but every
                # field makes clear that it is stale/incomplete and no platform
                # request took place.
                import scrape
                items = cache_store.load_items(
                    exclude_lids=scrape.blacklisted_lids(),
                    exclude_test_lids=scrape.test_blacklisted_lids())
                data = {
                    "ok": True,
                    "homeworks": [x for x in items if x.get("kind") != "test"],
                    "tests": [x for x in items if x.get("kind") == "test"],
                    "warnings": {"failed_courses": []},
                    "_cache_blocked": True,
                }
                data_source = "stale-cache"
                prefetched_age_sec = (coverage or {}).get("age_sec") or 0
        platform_hw = list(data.get("homeworks", []))
        for t in (data.get("tests") or []):          # 在线测试同一套规则
            t.setdefault("kind", "test")
            platform_hw.append(t)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"抓取失败：{e}", "coverage": coverage}

    manual = weekly.load_manual()
    picked = weekly.merge_and_filter(data, manual, days)

    # 两份文案：班级群（去选修课）/ 个人小群（完整清单）。
    # 与定时任务走的是同一段逻辑（weekly.split_items / format_message），
    # 绝不在这里另写一套筛选规则。
    elec = weekly._elective_lids_from_disk(data)
    class_items, personal_items = weekly.split_items(picked, elec)
    msg = weekly.format_message(class_items, days)
    msg_personal = weekly.format_message(personal_items, days,
                                         title="📚 我的作业提醒（含选修课）",
                                         elective_lids=elec)
    (ROOT / "last_message.txt").write_text(msg, encoding="utf-8")
    (ROOT / "last_message_personal.txt").write_text(msg_personal, encoding="utf-8")

    # 检索台账：预览/发布走的是同一段抓取，也要记账（区分是预览还是发布）
    _src = ("hub-preview" if no_send else "hub-publish")
    _fw = (data.get("warnings") or {}).get("failed_courses") or []
    _el = data.get("_elapsed_sec")
    _cached = data_source in ("cache", "stale-cache")
    fetch_log.log_fetch(_src, days, mode=("pool" if pool else "deadline-only"),
              rows=len(picked), rows_total=len(platform_hw), platform=len(platform_hw),
              tests=sum(1 for x in platform_hw if x.get("kind") == "test"),
              manual=len(manual), elapsed=_el, cached=_cached,
              data_age=(prefetched_age_sec if _cached else None),
              cache_hit=_cached,
              failed_courses=_fw,
              notes=(("复用" + ("旧" if data_source == "stale-cache" else "完整") + "缓存"
                      if _cached else "重新抓取")
                     + (f"；选修课 {len(elec)} 门只发个人" if elec else "")))

    if no_send:
        out = {
            "ok": True, "sent": False,
            "message": msg,                    # 班级群那条（已去选修课）
            "message_personal": msg_personal,  # 个人小群那条（完整清单）
            "count": len(class_items), "count_personal": len(personal_items),
            "elective_count": len(elec),
            "channel": channel or "默认",
            "coverage": coverage,
            "cache_blocked": bool((coverage or {}).get("publish_block_reasons")),
        }
        out.update(_data_source_fields(data_source, prefetched_age_sec))
        out["data_fetched_at"] = ((coverage or {}).get("updated_at")
                                  if data_source == "cache" else None)
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
            "message": msg, "message_personal": msg_personal,
            "count": len(class_items), "count_personal": len(personal_items),
            "channel": ch,
        }

    try:
        sent = notify.dispatch(msg, ch, cfg)
    except Exception as e:  # noqa: BLE001
        notify.log_push("hub", ch, msg, False,
                        note=f"exception:{type(e).__name__}", target=target)
        return {"ok": False, "error": f"推送异常：{e}", "message": msg}
    notify.log_push("hub", ch, msg, sent,
                    note="" if sent else "send-failed", target=target)

    # 个人小群那条：可选通道，失败不影响班级群那条（两者各自记账）
    personal = {"ok": True, "skipped": True, "note": "未启用"}
    pready, pwhy = notify.personal_ready(cfg)
    if pready:
        p_ch = notify.personal_cfg(cfg)["channel"]
        p_target = notify.dedup_target(p_ch, cfg, "personal")
        p_dup = notify.recent_duplicate(msg_personal, window, p_target)
        if p_dup:
            personal = {"ok": True, "sent": False, "deduped": True,
                        "note": f"{window} 秒内已向个人小群推过相同内容，已跳过"}
        else:
            try:
                p_sent = notify.dispatch(msg_personal, p_ch, cfg, audience="personal")
                notify.log_push("hub", p_ch, msg_personal, p_sent,
                                note="" if p_sent else "send-failed",
                                target=p_target, audience="personal")
                personal = {"ok": p_sent, "sent": p_sent, "channel": p_ch,
                            "count": len(personal_items),
                            "note": "" if p_sent else "推送失败"}
            except Exception as e:  # noqa: BLE001
                notify.log_push("hub", p_ch, msg_personal, False,
                                note=f"exception:{type(e).__name__}",
                                target=p_target, audience="personal")
                personal = {"ok": False, "sent": False, "error": f"推送异常：{e}"}
    else:
        personal = {"ok": True, "skipped": True, "note": pwhy}

    out = {
        "ok": sent, "sent": sent, "message": msg,
        "message_personal": msg_personal,
        "count": len(class_items), "count_personal": len(personal_items),
        "elective_count": len(elec),
        "channel": ch, "personal": personal,
        "coverage": coverage,
    }
    out.update(_data_source_fields(data_source, prefetched_age_sec))
    out["data_fetched_at"] = ((coverage or {}).get("updated_at")
                              if data_source == "cache" else None)
    return out


def run_courses():
    """课程目录：全部课程 + 三个名单状态 + 本学期有无作业。

    用途（2026-10-18）：界面上「临时想不起这门课叫什么」时的唯一数据源 ——
      选修课弹窗的输入联想（datalist）与「全部课程」清单，
      黑名单/测试黑名单弹窗的左栏候选，都用它。

    为什么必须带 lid：课程名会重名（「形势与政策（I）」有 3 个 lid、
    「C语言程序设计」有 2 个），只显示名字必然选错课。
    """
    import scrape
    idx = scrape.load_course_index()
    bl = set((scrape.load_blacklist().get("courses") or {}).keys())
    elec = set((scrape.load_electives().get("courses") or {}).keys())
    tbl = set((scrape.load_test_blacklist().get("courses") or {}).keys())
    meta = scrape.load_index_meta()
    try:
        sem_start = scrape._semester_start().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        sem_start = ""

    rows = []
    for lid, name in idx.items():
        m = meta.get(lid) or {}
        lh = m.get("last_homework")
        current = bool(lh and sem_start and lh[:10] >= sem_start)
        rows.append({
            "lid": str(lid),
            "name": name,
            "last_homework": lh,
            "current": current,          # 本学期有没有作业记录
            "blacklisted": str(lid) in bl,
            "elective": str(lid) in elec,
            "test_blacklisted": str(lid) in tbl,
            # 排序用：本学期有作业在前，其次有历史作业，最后是从没抓到过的
            "_r": 0 if current else (1 if lh else 2),
        })
    rows.sort(key=lambda r: (r["_r"], "" if not r["last_homework"] else
                             "".join(chr(0xFFFF - ord(c)) for c in r["last_homework"]),
                             r["name"]))
    for r in rows:
        r.pop("_r", None)
    return {
        "ok": True,
        "semester_start": sem_start,
        "total": len(rows),
        "courses": rows,
    }


def run_blacklist_list():
    """列出黑名单、选修课与建议候选（只读）。

    两件事要分开看，否则会互相误导：
      blacklist —— 不抓（整门课都不再扫描）
      electives —— 照抓，只是不进班级群那条提醒（发个人小群）
    """
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
        "electives": [
            {"lid": lid, "name": v.get("name"), "detail": v.get("detail"),
             "confirmed_at": v.get("confirmed_at")}
            for lid, v in (scrape.load_electives().get("courses") or {}).items()
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
    """把课程标记为「选修课」：照常抓取，但**不进班级群提醒**，只发个人小群。

    与黑名单的区别（2026-10-18 改写）：
      黑名单 = 完全不抓（这门课跟我无关 / 已结课）
      选修课 = 照抓，只是受众不同（班里多数人没上这门课）
    所以它写 electives.json，而不是 course_blacklist.json。

    tokens 可以是 lid，也可以是课程名的一部分（唯一匹配时才生效）。
    """
    import scrape
    index = scrape.load_course_index()
    data = scrape.load_electives()
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
            "detail": "选修课（照常抓取，只发给个人小群，不进班级群提醒）",
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": "human",
        }
        added.append({"lid": lid, "name": index[lid]})
    scrape.save_electives(data)
    return {"ok": True, "added": added, "unknown": unknown,
            "total": len(data["courses"])}


def run_elective_list():
    """列出「选修课」（照抓，只发个人小群）。

    独立成一块，不再混进黑名单列表 —— 两者是不同维度：
    黑名单=不抓，选修课=换受众。混在一起会让人以为"标了选修就等于不抓"。
    """
    import scrape
    elec = scrape.load_electives().get("courses", {})
    return {
        "ok": True,
        "total": len(elec),
        "electives": [
            {"lid": lid, "name": v.get("name"), "detail": v.get("detail"),
             "confirmed_at": v.get("confirmed_at")}
            for lid, v in elec.items()
        ],
        # 兼容前端统一渲染：沿用 blacklist 这个键名
        "blacklist": [
            {"lid": lid, "name": v.get("name"), "detail": v.get("detail"),
             "confirmed_at": v.get("confirmed_at")}
            for lid, v in elec.items()
        ],
    }


def run_elective_remove(lids):
    """取消选修课标记（恢复进班级群提醒）。"""
    import scrape
    elec = scrape.load_electives()
    removed = []
    for lid in lids:
        lid = str(lid)
        if lid in (elec.get("courses") or {}):
            removed.append({"lid": lid, "name": elec["courses"][lid].get("name")})
            elec["courses"].pop(lid)
    scrape.save_electives(elec)
    return {"ok": True, "removed": removed, "total": len(elec["courses"])}


def run_blacklist_remove(lids):
    """把课程从黑名单移出 —— 若它同时是「选修课」，连那个标记一起取消。

    界面上的「移出勾选项（恢复抓取）」语义是「这门课回到正常状态」，
    两个名单各留一半只会让人下次更困惑。
    """
    import scrape
    data = scrape.load_blacklist()
    removed = []
    for lid in lids:
        lid = str(lid)
        if lid in data["courses"]:
            removed.append({"lid": lid, "name": data["courses"][lid].get("name")})
            data["courses"].pop(lid)
    scrape.save_blacklist(data)

    elec = scrape.load_electives()
    unmarked = []
    for lid in lids:
        lid = str(lid)
        if lid in (elec.get("courses") or {}):
            unmarked.append({"lid": lid, "name": elec["courses"][lid].get("name")})
            elec["courses"].pop(lid)
    if unmarked:
        scrape.save_electives(elec)
    return {"ok": True, "removed": removed, "elective_removed": unmarked,
            "total": len(data["courses"])}


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


def run_content(hwtid: str = "", item_keys=None, hwtids=None, force: bool = False,
                limit: int | None = None):
    """Fill homework bodies; fresh cached bodies never start a browser."""
    import scrape
    wanted_keys = {str(x) for x in (item_keys or []) if x}
    wanted_ids = {str(x) for x in (hwtids or []) if x}
    if hwtid:
        wanted_ids.add(str(hwtid))
    blocked_now = scrape.blacklisted_lids()
    tests_blocked_now = scrape.test_blacklisted_lids()
    items = cache_store.load_items(
        exclude_lids=blocked_now, exclude_test_lids=tests_blocked_now)
    homework = [x for x in items
                if x.get("kind") == "homework" and x.get("hwtid")]
    by_key = {x["item_key"]: x for x in homework}
    by_hwtid = {str(x.get("hwtid")): x for x in homework}
    missing_keys = sorted(wanted_keys - set(by_key)) if wanted_keys else []
    missing_ids = sorted(wanted_ids - set(by_hwtid)) if wanted_ids else []
    selected = []
    if wanted_keys or wanted_ids:
        seen = set()
        for key in sorted(wanted_keys):
            item = by_key.get(key)
            if item and item["item_key"] not in seen:
                seen.add(item["item_key"])
                selected.append(item)
        for platform_id in sorted(wanted_ids):
            item = by_hwtid.get(platform_id)
            if item and item["item_key"] not in seen:
                seen.add(item["item_key"])
                selected.append(item)
    else:
        selected = [x for x in homework
                    if force or not x.get("content_ready")
                    or x.get("content_stale") or x.get("content_error")]
    selected.sort(key=lambda x: (x.get("deadline") or "", x.get("course") or ""))

    fresh, due = [], []
    for item in selected:
        is_fresh = (not force and item.get("content_ready")
                    and not item.get("content_stale") and not item.get("content_error"))
        (fresh if is_fresh else due).append(item)
    max_items = cache_store.cache_settings()["content_limit"] if limit is None else int(limit)
    queue_total = len(due)
    batch = due[:int(max_items)] if max_items and int(max_items) > 0 else list(due)
    remaining = max(0, len(due) - len(batch))
    next_keys = [x["item_key"] for x in due[len(batch):len(batch) + 12]]

    def cached_result(item):
        return {"ok": True, "cached": True, "item_key": item["item_key"],
                "hwtid": item.get("hwtid"), "title": item.get("title")}

    results = [cached_result(x) for x in fresh]
    error = None
    if batch:
        try:
            with FetchLock(wait_sec=120):
                with scrape.sync_playwright() as p:
                    ctx = scrape.make_context(p, headless=True)
                    try:
                        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                        page.goto(scrape.SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
                        if not scrape.wait_authenticated(page, ctx, timeout=12):
                            cache_store.invalidate_scope_scans(
                                "正文任务认证失败；旧扫描结果不可当作最新成功")
                            return {"ok": False, "requested": len(selected),
                                    "cached": len(fresh), "ok_count": 0,
                                    "failed": 0, "remaining": remaining,
                                    "error": "未登录或会话已过期，请运行 login.py",
                                    "items": results}
                        try:
                            scrape.bind_page_identity(page)
                        except cache_store.AccountIdentityConflict as exc:
                            cache_store.invalidate_scope_scans(str(exc))
                            return {"ok": False, "requested": len(selected),
                                    "cached": len(fresh), "failed": len(batch),
                                    "ok_count": 0, "remaining": remaining,
                                    "error": str(exc), "items": results}
                        for item in batch:
                            res = scrape.fetch_content_for_item(ctx, item, force=force)
                            results.append({"item_key": item["item_key"],
                                            "hwtid": item.get("hwtid"),
                                            "title": item.get("title"), **res})
                    finally:
                        try:
                            ctx.close()
                        except Exception:  # noqa: BLE001
                            pass
        except LockTimeout as exc:
            return {"ok": False, "requested": len(selected), "cached": len(fresh),
                    "ok_count": 0, "failed": len(batch), "remaining": remaining,
                    "error": str(exc), "items": results}

    fetched = [x for x in results if not x.get("cached")]
    failed = sum(1 for x in fetched if not x.get("ok"))
    ok_count = len(results) - failed
    missing = missing_keys + missing_ids
    if missing:
        error = ("本地缓存没有这些 hwtid：" + ", ".join(missing_ids[:8])
                 if missing_ids and not missing_keys else
                 "本地缓存没有这些条目；请先完成一次列表检索")
    if failed:
        error = (error + "；" if error else "") + \
            f"{failed}/{len(fetched)} 条正文获取失败；列表与其他作业不受影响"
    out = {"ok": not missing and failed == 0, "requested": len(selected),
           "cached": len(fresh), "queue_total": queue_total,
           "remaining": remaining, "next_item_keys": next_keys,
           "ok_count": ok_count, "failed": failed, "items": results}
    if error:
        out["error"] = error
    if len(selected) == 1:
        item = cache_store.get_item(selected[0]["item_key"])
        if item:
            out.update({"hwtid": item.get("hwtid"), "url": item.get("content_url"),
                        "content_text": item.get("content_text") or "",
                        "content_blocks": item.get("content_blocks") or [],
                        "source_version": item.get("source_version"),
                        "content_html": None})
    return out


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
    p_list.add_argument("--lids", nargs="+", metavar="LID",
                        help="只刷新指定课程（单课/失败课重试）")

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
    p_ct.add_argument("--force", action="store_true", help="忽略 TTL，强制重取正文")

    args = ap.parse_args()
    if args.cmd == "list":
        print(json.dumps(
            run_list(args.days, args.fetch, args.with_content, args.sweep,
                     args.pool, no_tests=args.no_tests, include_expired=args.all,
                     lids=args.lids),
            ensure_ascii=False, default=str))
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
        print(json.dumps(run_content(args.hwtid, force=args.force), ensure_ascii=False))


if __name__ == "__main__":
    main()
