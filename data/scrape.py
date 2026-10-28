# -*- coding: utf-8 -*-
"""
抓取北化在线（THEOL）待提交作业及其截止时间。

实际验证过的流程（2026-09-09）：
  1. 必须走 SSO 入口 sso_login.jsp —— CASTGC 票据要在这里换成平台会话，
     直接访问 /meol 只会拿到未登录首页。
  2. 个人页 #reminder 下有三类提醒，只有 onclick 里带 t=hw 的是「待提交作业」，
     另外还有 t=info（未读通知）、t=test（待提交测试）、系统通知 —— 都要排除。
  3. 每门课的作业入口是 course_column_preview_transfer.jsp?columnId=<每课不同>，
     需要从课程页菜单里动态取，不能硬编码。
  4. 该地址会跳转到 /meol/common/hw/student/hwtask.jsp，表格列：
     标题 | 截止时间 | 分数 | 发布人 | 统计信息 | 提交作业 | 查看结果 | 优秀作品
     其中 标题链接 = hwtask.view.jsp?hwtid=XXX（作业内容详情页，已交/未交都能看正文）
     提交作业 = write.jsp?hwtid=XXX（未提交时才有）  查看结果 = taskanswer.jsp?hwtid=XXX（已提交时才有）
     三者共享同一个 hwtid，抓取时一并记录，供控制台「查看作业内容」用。

用法补充:
    python scrape.py content <hwtid>   # 抓取某条作业的正文（已登录会话），输出 JSON
                                        # 平台把正文放在隐藏域 `<input ... name="N_content" value="实体编码HTML">`


用法:
    python scrape.py                # 输出 JSON 到 stdout
    python scrape.py --debug        # 额外 dump HTML/截图到 debug/
    python scrape.py --days 7       # 只输出未来 7 天内截止的（默认全部）
"""
import argparse
import html
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urljoin, urlsplit

import cache_store
import content_pipeline
from platform_lock import FetchLock, LockTimeout

# Windows 下子进程 stdout 默认按 GBK(cp936) 编码，父进程用 utf-8 读会 UnicodeDecodeError。
# 强制本进程以 utf-8 输出，保证被 weekly.py 调用时解码一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# Playwright 只在需要开浏览器时才用得上；黑名单这类纯本地命令不该被它拖累
# （用系统 python 跑时没有 playwright，之前会直接 ModuleNotFoundError 崩掉）。
try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    sync_playwright = None
    HAVE_PLAYWRIGHT = False


def need_browser():
    """开浏览器前检查解释器是否带 playwright，给出可操作的提示。"""
    if not HAVE_PLAYWRIGHT:
        print(
            "ERROR: 当前 Python 没装 playwright，无法抓取。\n"
            f"当前解释器：{sys.executable}\n"
            "请改用装了 playwright 的那个解释器运行本脚本；\n"
            "（黑名单相关命令不需要 playwright，用哪个 python 都行）",
            file=sys.stderr,
        )
        return False
    return True

# 平台互斥由 platform_lock.FetchLock 统一负责：所有入口共用一把锁，超时失败。
LOCK_WAIT_SEC = 120


BASE = "https://course.buct.edu.cn"
SSO_LOGIN = BASE + "/meol/homepage/common/sso_login.jsp"
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path.home() / ".workbuddy" / "profiles" / "buct-course-edge"
STATE_FILE = ROOT / "storage_state.json"
DEBUG_DIR = ROOT / "debug"

# A single THEOL list is expected to fit comfortably in this bound.  Exceeding
# it is a hard failure, never a partial success.
MAX_LIST_PAGES = 50

TOTAL_RECORDS_RE = re.compile(r"共(?:有)?\s*(\d+)\s*条(?:记录|数据)?")
TOTAL_PAGES_RE = re.compile(r"共\s*(\d+)\s*页")
NEXT_PAGE_LABELS = ("下一页", "下页")
TERMINAL_PAGE_LABELS = ("尾页", "末页")
PAGE_NUMBER_QUERY_KEYS = ("s_gotopage", "gotopage", "pageno", "pagingpage", "page")

IDENTITY_SELECTORS = (
    "#userName,.userName,.user-name,#accountName,.account-name,.accountName,"
    "#loginName,.loginName,.login-name,[data-account-name]")
PERSONAL_IDENTITY_RE = re.compile(
    r"(?m)(?:^|[\r\n|；;])[ \t]*(?:学号|用户名|姓名)[ \t]*[:：][ \t]*"
    r"([^\s|；;，,]{1,40})")

# 平台有两套时间写法，都要认：
#   作业表：2026年11月26日21:13:00（偶尔出现残缺秒 9:04:0）
#   测试表：2026-12-13 23:59:00（ISO 风格）
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})?(?::\d{1,2})?")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?")
TIME_SEG_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{1,2}))?")


def norm_deadline(raw: str) -> str:
    """把平台时间文本归一化成 `YYYY-MM-DD HH:MM`（拿不到时间就只给日期）。

    必须同时支持中文式（作业表）与 ISO 式（测试表），否则会静默丢掉整类数据。
    """
    raw = raw or ""
    out = ""
    tm = None
    m = DATE_RE.search(raw)
    if m:
        out = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        tm = TIME_SEG_RE.search(raw)
    else:
        m = ISO_DATE_RE.search(raw)
        if not m:
            return ""
        out = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if m.group(4):
            hh, mm = int(m.group(4)), int(m.group(5))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return out + f" {hh:02d}:{mm:02d}"
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            out += f" {hh:02d}:{mm:02d}"
    return out


def make_context(p, headless=True):
    browser_kwargs = dict(headless=headless, channel="msedge", args=["--no-sandbox"])
    ctx_kwargs = dict(ignore_https_errors=True)
    if STATE_FILE.exists():
        return p.chromium.launch(**browser_kwargs).new_context(
            storage_state=str(STATE_FILE), **ctx_kwargs)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), **browser_kwargs, **ctx_kwargs)


def logged_in(page, ctx) -> bool:
    """已登录页面里仍保留「非统一身份认证登录」（切换账号入口），不能用来判定。"""
    try:
        names = {c["name"].upper() for c in ctx.cookies()}
        if "CASTGC" in names or "TGC" in names:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        t = page.inner_text("body", timeout=3000) or ""
    except Exception:  # noqa: BLE001
        return False
    return ("在线总时长" in t) or ("登录时间" in t)


def dump(page, name: str, debug: bool):
    if not debug:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    except Exception:  # noqa: BLE001
        pass


def parse_courses(page):
    """只取「待提交作业」分组（onclick 含 t=hw）。

    注意：这是**平台自己的推送视图**，不是全部课程。实测有的课挂着待提交作业
    （复变函数 26-27第五周作业，deadline 未过、提交链接存在）却不在这个视图里 ——
    所以要用 --sweep 做全量扫描兜底，别把这里当成全集。
    """
    items = page.eval_on_selector_all(
        "#reminder a",
        """els => els.map(a => ({
            text: (a.textContent||'').trim(),
            onclick: a.getAttribute('onclick')||'',
            href: a.href||''
        }))""",
    )
    out, seen = [], set()
    for it in items:
        src = it["onclick"] + " " + it["href"]
        if "t=hw" not in src:          # 排除通知/测试/系统通知
            continue
        m = re.search(r"lid=(\d+)", src)
        if not m or m.group(1) in seen:
            continue
        name = " ".join(it["text"].split())
        if not name:
            continue
        seen.add(m.group(1))
        out.append({"course": name, "lid": m.group(1)})
    return out


# ---------------------------------------------------------------- 全量课程扫描
COURSES_FILE = ROOT / "courses.json"
ALL_COURSES_URL = BASE + "/meol/lesson/blen.student.lesson.list.jsp"


def load_course_index() -> dict:
    """读本地课程索引 {lid: 课程名}。索引由 --refresh-courses / 控制台按钮维护。"""
    if not COURSES_FILE.exists():
        return {}
    try:
        data = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {str(k): str(v) for k, v in (data.get("courses") or {}).items()}


def collect_all_courses(page) -> dict:
    """从「我的课程」总表抓全部课程 -> {lid: 课程名}。

    实测（2026-10-12）：courseId 与 hwtask.jsp 的 lid 等价，
    同一门课的 hwtid 完全一致，所以 courseId 可直接当 lid 用。
    """
    page.goto(ALL_COURSES_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    pairs = page.eval_on_selector_all(
        "a",
        """els => els.map(a => {
            const oc = a.getAttribute('onclick')||'';
            const m = /course_index\\.jsp\\?courseId=(\\d+)/.exec(oc);
            const t = (a.textContent||'').replace(/\\s+/g,' ').trim();
            return m && t ? [m[1], t] : null;
        }).filter(Boolean)""",
    )
    out = {}
    for lid, name in pairs:
        out.setdefault(str(lid), str(name))
    return out


def _budget_ms(deadline: float, cap_ms: int = 60000) -> int:
    return max(1, min(int(cap_ms), int((deadline - time.monotonic()) * 1000)))


def _ensure_budget(deadline: float, step: str) -> None:
    if time.monotonic() >= deadline:
        raise RuntimeError(f"课程时间预算已耗尽（{step}）")


def _host_is_platform(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return (host == "buct.edu.cn" or host.endswith(".buct.edu.cn")
            or host in ("127.0.0.1", "localhost", "::1"))


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _url_origin(url: str):
    """Return normalized (scheme, hostname, effective port), or None."""
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower().rstrip(".")
        if scheme not in _DEFAULT_PORTS or not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        return None
    return scheme, host, port


def _url_key(url: str):
    """Stable comparison key; fragments and query ordering are irrelevant."""
    try:
        parts = urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        return None
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return (parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."),
            port, parts.path, query)


def _course_ids(url: str) -> set[str]:
    try:
        query = parse_qs(urlsplit(str(url or "")).query, keep_blank_values=True)
    except ValueError:
        return set()
    out: set[str] = set()
    for key in ("courseId", "courseid", "lid", "cid", "course_id"):
        out.update(str(v).strip() for v in query.get(key, []) if str(v).strip())
    return out


def _course_scope_ok(url: str, lid: str) -> tuple[bool, str]:
    """Every explicit course ID on a list URL must be the expected course."""
    expected = str(lid or "").strip()
    if not expected:
        return True, ""
    wrong = sorted(x for x in _course_ids(url) if x != expected)
    if wrong:
        return False, f"列表页包含其它课程 ID：{', '.join(wrong)}"
    return True, ""


def _current_list_page(url: str) -> tuple[int | None, bool]:
    """Return (explicit page number, explicit last-page evidence)."""
    page_no = _query_page_number(url)
    try:
        query = parse_qs(urlsplit(str(url or "")).query, keep_blank_values=True)
    except ValueError:
        return page_no, False
    terminal = any(str(v).strip().lower() in ("last", "end", "final")
                   for v in query.get("s_page", []))
    return page_no, terminal


def _query_page_number(url: str) -> int | None:
    """Read a page number from known list/paging query parameters."""
    try:
        query = parse_qs(urlsplit(str(url or "")).query, keep_blank_values=True)
    except ValueError:
        return None
    lowered: dict[str, list[str]] = {}
    for key, values in query.items():
        lowered.setdefault(str(key).lower(), []).extend(values)
    for key in PAGE_NUMBER_QUERY_KEYS:
        for value in lowered.get(key, []):
            if str(value).strip().isdigit():
                return int(str(value).strip())
    return None


def _is_page_size_control(anchor: dict, label: str, href: str,
                          onclick: str) -> bool:
    """A per-page selector is not a next-page control."""
    # Next-page handlers may also mention pagingNumberPer. Exempt only the
    # observed size-setting button, never links or unknown JS navigation.
    return (not href and label == "设置"
            and str(anchor.get("type") or "").lower() == "button"
            and "ItemsPerPageSubmitButton" in str(anchor.get("class") or "")
            and "pagingnumberper" in onclick.lower())


def _safe_next_target(current_url: str, href: str, lid: str) -> tuple[str | None, str]:
    """Resolve a next-page href only when it stays on this list and course."""
    raw = html.unescape(str(href or "")).strip()
    if not raw:
        return None, "下一页链接为空"
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "mailto:", "data:", "#")):
        return None, "下一页使用 JavaScript 或非 HTTP 跳转，无法确认分页"
    resolved = urljoin(str(current_url or ""), raw)
    try:
        base = urlsplit(str(current_url or ""))
        target = urlsplit(resolved)
        target_host = (target.hostname or "").lower().rstrip(".")
    except ValueError:
        return None, "下一页地址无法解析"
    if target.scheme.lower() not in ("http", "https"):
        return None, "下一页不是 HTTP(S) 地址"
    base_origin = _url_origin(current_url)
    target_origin = _url_origin(resolved)
    if base_origin is None:
        return None, "当前列表地址无法解析或端口非法"
    if target_origin is None:
        return None, "下一页地址无法解析或端口非法"
    if target_origin != base_origin:
        return None, f"下一页跨域/跨源（协议、主机或端口不一致）：{target_host or '?'}"
    if not _host_is_platform(target_host):
        return None, f"下一页跨域：{target_host or '?'}"
    if target.path != base.path:
        return None, f"下一页跨栏目路径：{target.path or '?'}"
    if any(x in target.path.lower() for x in
           ("sso_login", "/login", "course_column_preview_transfer", "enter_course")):
        return None, "下一页跳到了认证/课程入口"
    target_ids = _course_ids(resolved)
    expected = str(lid or "").strip()
    if expected and target_ids and any(x != expected for x in target_ids):
        return None, "下一页包含其它课程 ID"
    return resolved, ""


def _retry_list_url_usable(url: str | None) -> bool:
    """A retry may replay the landed list URL, never the one-time transfer."""
    if not url:
        return False
    if _url_origin(url) is None:
        return False
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    path = (parts.path or "").lower()
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if not _host_is_platform((parts.hostname or "").lower().rstrip(".")):
        return False
    if any(x in path for x in ("sso_login", "/login", "course_column_preview_transfer",
                               "enter_course", "layout/newpage/index.jsp")):
        return False
    return "hwtask.jsp" in path or "/question/test/student/list.jsp" in path


def _landed_list_scope_ok(landed: str, first_landed: str, lid: str) -> tuple[bool, str]:
    """Reject a next page that redirects away from the first landed list."""
    try:
        base = urlsplit(first_landed)
        target = urlsplit(landed)
        target_host = (target.hostname or "").lower().rstrip(".")
    except ValueError:
        return False, "分页落地地址无法解析"
    if target.scheme.lower() not in ("http", "https"):
        return False, "分页落地地址不是 HTTP(S)"
    base_origin = _url_origin(first_landed)
    target_origin = _url_origin(landed)
    if base_origin is None:
        return False, "分页落地基准地址无法解析或端口非法"
    if target_origin is None:
        return False, "分页落地地址无法解析或端口非法"
    if target_origin != base_origin:
        return False, f"分页落地跨域/跨源（协议、主机或端口不一致）：{target_host or '?'}"
    if not _host_is_platform(target_host):
        return False, f"分页落地跨域：{target_host or '?'}"
    if target.path != base.path:
        return False, f"分页落地跨栏目路径：{target.path or '?'}"
    course_ok, course_why = _course_scope_ok(landed, lid)
    if not course_ok:
        return False, course_why.replace("列表页", "分页落地", 1)
    return True, ""


def _record_id(kind: str, row: dict) -> str:
    key = "hwtid" if kind == "homework" else "test_id"
    return str(row.get(key) or "").strip()


def _record_signature(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def _collect_list_pages(page, kind: str, source: str, lid: str, deadline: float,
                        debug: bool, dump_prefix: str) -> tuple[list[dict], str, str, str]:
    """Collect every explicitly linked page, returning rows only when complete.

    A partial traversal is never returned as success.  The landed first list URL
    is returned for a safe retry (the one-time transfer URL is never replayed).
    """
    collected: list[dict] = []
    seen_rows: dict[str, str] = {}
    visited: set = set()
    first_landed = ""
    total_records = None
    total_pages = None
    current = source
    page_count = 0

    try:
        while True:
            _ensure_budget(deadline, f"{kind} 列表第 {page_count + 1} 页")
            if page_count >= MAX_LIST_PAGES:
                return [], "incomplete", f"列表超过最大翻页数 {MAX_LIST_PAGES}，拒绝部分结果", first_landed
            try:
                page.goto(current, wait_until="domcontentloaded",
                          timeout=_budget_ms(deadline))
            except Exception as exc:  # noqa: BLE001
                return [], "incomplete", f"列表页加载失败：{exc}", first_landed
            _ensure_budget(deadline, f"{kind} 列表第 {page_count + 1} 页加载")

            landed = str(getattr(page, "url", "") or current)
            if page_is_login(page):
                return [], "auth", "列表页跳到了登录/认证页", first_landed
            try:
                landed_parts = urlsplit(landed)
            except ValueError:
                return [], "incomplete", "列表页落地地址无法解析", first_landed
            if landed_parts.scheme.lower() not in ("http", "https"):
                return [], "incomplete", "列表页不是 HTTP(S) 地址", first_landed
            if _url_origin(landed) is None:
                return [], "incomplete", "列表页地址无法解析或端口非法", first_landed
            if not _host_is_platform((landed_parts.hostname or "").lower().rstrip(".")):
                return [], "incomplete", f"列表页域名异常：{landed_parts.hostname or '?'}", first_landed
            course_ok, course_why = _course_scope_ok(landed, lid)
            if not course_ok:
                return [], "incomplete", course_why, first_landed
            if page_count == 0:
                source_origin = _url_origin(current)
                if (source_origin is not None
                        and _url_origin(landed) != source_origin):
                    return [], "incomplete", (
                        "列表首落地跨域/跨源（协议、主机或端口不一致）"), first_landed
            if not first_landed:
                first_landed = landed

            landed_key = _url_key(landed)
            if landed_key in visited:
                return [], "incomplete", f"分页循环/重复页：{landed}", first_landed
            visited.add(landed_key)
            visited.add(_url_key(current))
            if page_count > 0:
                scope_ok, scope_why = _landed_list_scope_ok(landed, first_landed, lid)
                if not scope_ok:
                    return [], "incomplete", scope_why, first_landed

            try:
                page_rows = parse_hw_table(page) if kind == "homework" else parse_test_table(page)
            except Exception as exc:  # noqa: BLE001
                return [], "incomplete", f"列表行解析失败：{exc}", first_landed
            status, why = classify_list_page(page, kind, page_rows)
            if status in ("auth", "invalid", "incomplete"):
                return [], status, why, first_landed

            evidence = _pagination_evidence(page)
            page_count += 1
            _ensure_budget(deadline, f"{kind} 列表第 {page_count} 页解析")
            if debug:
                dump(page, f"{dump_prefix}_p{page_count}", debug)

            this_total = evidence["total_records"]
            if this_total is not None:
                if total_records is None:
                    total_records = this_total
                elif this_total != total_records:
                    return [], "incomplete", (f"页脚总记录数前后不一致："
                                              f"{total_records} vs {this_total}"), first_landed
            this_pages = evidence["total_pages"]
            if this_pages is not None:
                if total_pages is None:
                    total_pages = this_pages
                elif this_pages != total_pages:
                    return [], "incomplete", (f"页脚总页数前后不一致："
                                              f"{total_pages} vs {this_pages}"), first_landed

            if status == "empty":
                if (page_count != 1 or evidence["next_links"]
                        or (evidence["has_terminal"] and total_records != 0)
                        or (total_pages or 0) > 1):
                    return [], "incomplete", "空列表仍出现分页证据，拒绝当作完整空页", first_landed
                if total_records not in (None, 0):
                    return [], "incomplete", f"页脚称有 {total_records} 条记录但页面为空", first_landed
                return [], "empty", why, first_landed

            for row in page_rows:
                rid = _record_id(kind, row)
                if not rid:
                    return [], "incomplete", f"第 {page_count} 页存在缺少平台 ID 的行", first_landed
                signature = _record_signature(row)
                if rid in seen_rows:
                    if seen_rows[rid] == signature:
                        return [], "incomplete", f"出现重复平台 ID {rid}，拒绝去重后冒充完整", first_landed
                    return [], "incomplete", f"平台 ID {rid} 内容冲突", first_landed
                seen_rows[rid] = signature
                collected.append(row)
            if total_records is not None and len(collected) > total_records:
                return [], "incomplete", (f"已汇总 {len(collected)} 行，超过页脚总数 "
                                          f"{total_records}"), first_landed

            current_page, explicit_terminal = _current_list_page(landed)
            if explicit_terminal or (total_pages is not None and current_page is not None
                                     and current_page >= total_pages):
                break
            if evidence["unknown_next"]:
                return [], "incomplete", "存在无法解析的 JavaScript 分页控件", first_landed

            next_targets: list[str] = []
            for href in evidence["next_links"]:
                resolved, reason = _safe_next_target(landed, href, lid)
                if not resolved:
                    return [], "incomplete", reason, first_landed
                key = _url_key(resolved)
                if key not in {_url_key(x) for x in next_targets}:
                    next_targets.append(resolved)
            if len(next_targets) > 1:
                return [], "incomplete", "页面同时给出多个不同的下一页地址", first_landed
            if not next_targets:
                break
            _ensure_budget(deadline, f"{kind} 列表第 {page_count + 1} 页翻页")
            current = next_targets[0]
            if _url_key(current) in visited:
                return [], "incomplete", f"分页循环/重复页：{current}", first_landed
    except RuntimeError as exc:
        return [], "incomplete", str(exc), first_landed

    try:
        _ensure_budget(deadline, f"{kind} 列表完整性核对")
    except RuntimeError as exc:
        return [], "incomplete", str(exc), first_landed
    if total_pages is not None and page_count != total_pages:
        return [], "incomplete", (f"页脚称共 {total_pages} 页，实际只遍历 "
                                  f"{page_count} 页"), first_landed
    if total_records is None:
        if page_count > 1:
            return [], "incomplete", "多页列表缺少页脚总记录数，无法核对完整性", first_landed
    elif len(collected) != total_records:
        return [], "incomplete", (f"页脚总记录数 {total_records}，实际汇总 "
                                  f"{len(collected)} 条"), first_landed
    return collected, "ok", f"{len(collected)} 行，已核对 {page_count} 页", first_landed


def enter_course_page(page, lid: str, name: str, deadline: float) -> bool:
    """Try the observed native landing page, otherwise use the official gateway.

    A decorative courseId is insufficient: require the protected native origin,
    exact course path/ID, expected course name, and homework menu together.
    Returns whether the direct landing was confirmed; both paths share the
    caller's deadline and subsequent course validation.
    """
    settings = cache_store.cache_settings()
    if (settings.get('direct_course_entry', True) and name
            and re.fullmatch(r'\d+', str(lid))):
        target = f'{BASE}/meol/jpk/course/layout/newpage/index.jsp?courseId={lid}'
        try:
            page.goto(target, wait_until='domcontentloaded',
                      timeout=_budget_ms(deadline, cap_ms=10000))
            if page_is_login(page):
                raise RuntimeError('认证上下文失效：课程直达入口跳到了登录页')
            landed = urlsplit(page.url)
            body = _body_text(page)
            if (_url_origin(page.url) == _url_origin(BASE)
                    and landed.path == '/meol/jpk/course/layout/newpage/index.jsp'
                    and _course_ids(page.url) == {str(lid)}
                    and name in body and '课程作业' in body):
                _ensure_budget(deadline, '确认课程直达页')
                return True
        except Exception as exc:  # noqa: BLE001 -- old layouts keep the gateway
            if '认证上下文失效' in str(exc):
                raise
        _ensure_budget(deadline, '回退课程入口')
    page.goto(f'{BASE}/meol/lesson/enter_course.jsp?lid={lid}&t=hw',
              wait_until='domcontentloaded', timeout=_budget_ms(deadline))
    return False


def fetch_course_both(ctx, page, lid: str, name: str, debug: bool = False,
                      want_homework: bool = True, want_tests: bool = False,
                      empty_retries: int = 2, return_status: bool = False):
    """Fetch both columns in one course context without treating empty as failure.

    Only an *invalid/unknown* empty page is retried, and only that column is
    revisited.  A valid empty table is accepted immediately.  Explicit
    ``want_tests=False`` never touches the test column.
    """
    notes: list[str] = []
    statuses = {"homework": "invalid", "tests": "disabled" if not want_tests else "invalid"}
    evidence: dict[str, str] = {}
    settings = cache_store.cache_settings()
    deadline = time.monotonic() + max(5.0, float(settings["course_timeout_sec"]))

    enter_course_page(page, lid, name, deadline)
    if page_is_login(page):
        raise RuntimeError("认证上下文失效：课程入口跳到了登录页")
    if not wait_text(page, "课程作业", timeout=min(12.0, max(0.1, deadline - time.monotonic()))):
        raise RuntimeError("课程页未出现「课程作业」入口")
    ok, why = validate_course_context(page, lid, name)
    if not ok:
        raise RuntimeError(why)
    _ensure_budget(deadline, "进入课程页")

    hw_href = (get_column_href(page, "课程作业", timeout=min(10.0, max(0.1, deadline - time.monotonic())))
               if want_homework else None)
    test_href = (get_column_href(page, "在线测试", timeout=min(1.2, max(0.1, deadline - time.monotonic())), retries=2)
                 if want_tests else None)
    if want_homework and not hw_href:
        raise RuntimeError("取不到「课程作业」链接")
    if want_tests and not test_href:
        notes.append("no-test-link")

    landed_first: dict[str, str] = {}

    def load_column(href: str, kind: str) -> tuple[list[dict], str, str]:
        last_status, last_why = "invalid", "未加载"
        attempts = max(1, int(empty_retries) + 1)
        for attempt in range(attempts):
            _ensure_budget(deadline, f"{kind} 列表")
            source = href if attempt == 0 else landed_first.get(kind)
            if not source:
                return [], "incomplete", (f"{last_why}；首次列表地址未落地，"
                                          "拒绝重放一次性栏目入口")
            rows, status, why, landed = _collect_list_pages(
                page, kind, source, lid, deadline, debug,
                f"sweep_{lid}" if kind == "homework" else f"sweeptest_{lid}")
            if landed and kind not in landed_first:
                landed_first[kind] = landed
            last_status, last_why = status, why
            if status in ("ok", "empty"):
                return rows, status, why
            if status == "auth":
                return [], status, why
            if attempt + 1 < attempts:
                if time.monotonic() >= deadline:
                    last_status, last_why = "incomplete", "课程时间预算耗尽"
                    break
                retry_url = landed_first.get(kind)
                if not _retry_list_url_usable(retry_url):
                    last_status, last_why = (
                        "incomplete", f"{why}；落地列表地址不可安全重试")
                    break
                print(f"    {name}: {kind} 页面未确认（{why}），从落地列表地址只重试该栏目 "
                      f"{attempt + 1}/{attempts - 1}", file=sys.stderr)
        return [], last_status, last_why

    hw_rows: list[dict] = []
    if hw_href:
        hw_rows, statuses["homework"], evidence["homework"] = load_column(hw_href, "homework")
        if statuses["homework"] == "empty":
            notes.append("homework-empty-verified")
        if statuses["homework"] in ("invalid", "incomplete", "auth"):
            raise RuntimeError(f"作业列表未确认完整：{evidence['homework']}")

    test_rows: list[dict] = []
    if want_tests and test_href:
        test_rows, statuses["tests"], evidence["tests"] = load_column(test_href, "test")
        if statuses["tests"] == "empty":
            notes.append("tests-empty-verified")
        if statuses["tests"] in ("invalid", "incomplete", "auth"):
            notes.append("tests-invalid")
    elif want_tests:
        statuses["tests"] = "no_entry"

    if return_status:
        return hw_rows, test_rows, notes, statuses
    return hw_rows, test_rows, notes


def get_column_href_from(page, course_url: str, label: str, timeout: int = 20):
    """回到课程页取某栏目的 href（保留给需要单栏目重试的场景）。"""
    page.goto(course_url, wait_until="domcontentloaded", timeout=60000)
    if not wait_text(page, "课程作业", timeout=timeout):
        return None
    return get_column_href(page, label, timeout=timeout)


# ---------------------------------------------------------------- 课程黑名单
BLACKLIST_FILE = ROOT / "course_blacklist.json"
# 只屏蔽「在线测试」、作业照常搜（例：物理实验的在线测试是多班共用的课前小测）
TEST_BLACKLIST_FILE = ROOT / "test_blacklist.json"
SEMESTER_MONTH = 8      # 学期边界：8/1 起算本学期；2/1 起算上学期
STALE_FAIL_STREAK = 2   # 连续 N 次扫描都没能进「课程作业」才建议拉黑（避免一次网络抖动就误杀）
EMPTY_CANDIDATE_STREAK = 2   # 连续 N 轮扫描都是 0 条作业 -> 列入候选（仅供用户参考）


def load_test_blacklist() -> dict:
    """读「仅屏蔽在线测试」名单。结构 {courses:{lid:{name,note,confirmed_at}}}。"""
    if not TEST_BLACKLIST_FILE.exists():
        return {"updated_at": None, "courses": {}}
    try:
        d = json.loads(TEST_BLACKLIST_FILE.read_text(encoding="utf-8"))
        d.setdefault("courses", {})
        return d
    except Exception:  # noqa: BLE001
        return {"updated_at": None, "courses": {}}


def save_test_blacklist(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    TEST_BLACKLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_blacklisted_lids() -> set:
    """需要跳过「在线测试」的课：

    1. 人工名单（test_blacklist.json）
    2. 自动识别：连续 >=2 次找不到「在线测试」栏目的课 —— 这些课每次都会白等一遍超时，
       而它们本来就没有测试可抓。只影响「要不要去找测试栏目」，不会漏掉任何作业。
    """
    return set(load_test_blacklist().get("courses", {}).keys())


def has_test_entry(page, timeout: float = 1.2) -> bool:
    return bool(get_column_href(page, "在线测试", timeout=timeout, retries=2))


def no_test_skip_is_fresh(m: dict, now=None) -> bool:
    """Auto-skip a confirmed missing test column only inside its review period."""
    now = now or datetime.now()
    if int(m.get("no_test_streak", 0)) < 2:
        return False
    raw = m.get("no_test_checked_at")
    if not raw:
        return False
    try:
        last = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    review_days = cache_store.cache_settings()["no_tests_review_days"]
    return (now - last).total_seconds() < review_days * 86400


def _semester_start(now=None) -> datetime:
    """当前学期的起点：>=8月 -> 当年8/1；否则 -> 当年2/1。"""
    now = now or datetime.now()
    if now.month >= SEMESTER_MONTH:
        return datetime(now.year, SEMESTER_MONTH, 1)
    return datetime(now.year, 2, 1)


def load_blacklist() -> dict:
    """读课程黑名单。结构 {courses:{lid:{name,reason,confirmed_at,evidence}}}。"""
    if not BLACKLIST_FILE.exists():
        return {"updated_at": None, "courses": {}}
    try:
        d = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
        d.setdefault("courses", {})
        return d
    except Exception:  # noqa: BLE001
        return {"updated_at": None, "courses": {}}


def save_blacklist(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    BLACKLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def blacklisted_lids() -> set:
    return set(load_blacklist().get("courses", {}).keys())


# ---------------------------------------------------------------- 选修课
# 语义（2026-10-18 改写）：选修课**不是**黑名单。
#   课要照抓（你要交），只是「班级群那条提醒」里删掉它、
#   「发给你个人 / 选修课小群那条」里保留它。
# 旧实现沿用黑名单的 reason=elective（等于整门不抓），结果连你自己的提醒
# 也一起没了 —— 与需求正好相反。所以拆成独立文件：
#   黑名单 = 不抓；选修课 = 换受众（照抓，但不发给班级群）。
ELECTIVES_FILE = ROOT / "electives.json"


def save_electives(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ELECTIVES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate_electives_from_blacklist(elec: dict) -> int:
    """把旧版写在黑名单里的 reason=elective 条目搬到 electives.json。

    不迁移的话，这些课会继续被黑名单跳过 —— 界面上显示「选修课」，
    却依然收不到它的任何提醒，症状是「标记了反而更没消息了」。
    """
    bl = load_blacklist()
    courses = bl.get("courses") or {}
    moved = 0
    for lid, v in list(courses.items()):
        if v.get("reason") != "elective":
            continue
        elec.setdefault("courses", {}).setdefault(lid, {
            "name": v.get("name"),
            "detail": "选修课（照常抓取，只发给个人小群，不进班级群提醒）",
            "confirmed_at": v.get("confirmed_at"),
            "confirmed_by": v.get("confirmed_by", "human"),
        })
        courses.pop(lid, None)
        moved += 1
    if moved:
        save_blacklist(bl)
    return moved


def load_electives() -> dict:
    """读选修课名单。结构 {updated_at, courses:{lid:{name,detail,confirmed_at,confirmed_by}}}。"""
    data = {"updated_at": None, "courses": {}}
    if ELECTIVES_FILE.exists():
        try:
            data = json.loads(ELECTIVES_FILE.read_text(encoding="utf-8"))
            data.setdefault("courses", {})
        except Exception:  # noqa: BLE001
            data = {"updated_at": None, "courses": {}}
    if _migrate_electives_from_blacklist(data):
        save_electives(data)
    return data


def elective_lids() -> set:
    """选修课的 lid 集合：这些课照抓，但只进「个人」那条提醒。"""
    return set(load_electives().get("courses", {}).keys())



def load_index_meta() -> dict:
    """读 courses.json 里的每课元数据（失败次数 / 最近作业时间）。"""
    if not COURSES_FILE.exists():
        return {}
    try:
        return json.loads(COURSES_FILE.read_text(encoding="utf-8")).get("meta") or {}
    except Exception:  # noqa: BLE001
        return {}


META_BACKUP_FILE = COURSES_FILE.with_name("courses.meta.bak.json")


def save_index_meta(meta: dict, force: bool = False) -> None:
    """把每课元数据并回 courses.json（保留 courses 不动）。

    护栏（2026-10-13 事故后加）：
      曾有一次调用方把「空 meta」写回，抹掉了全部课程的 last_homework / empty_streak，
      后果是黑名单候选（陈旧 / 暂无作业）全部消失，界面看起来像功能坏了。
      所以这里：
        · 写之前先把旧 meta 备份到 courses.meta.bak.json
        · 若旧 meta 非空而这次要写成空，直接拒绝（除非显式 force=True）
    """
    try:
        d = json.loads(COURSES_FILE.read_text(encoding="utf-8")) if COURSES_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    old_meta = d.get("meta") or {}
    if old_meta and not meta and not force:
        print("WARN: 拒绝把非空元数据写成空（会丢失最近作业时间与空课计数）。"
              "确实要清空请传 force=True。", file=sys.stderr)
        return
    if old_meta:
        try:
            META_BACKUP_FILE.write_text(
                json.dumps({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "meta": old_meta}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
    d["meta"] = meta
    d.setdefault("refreshed_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    COURSES_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def update_index_meta(per_course: list, now=None) -> dict:
    """扫描后更新每课元数据，并按规则算出「建议拉黑」候选（只建议，不落黑名单）。"""
    now = now or datetime.now()
    meta = load_index_meta()
    seen_lids = set()
    for pc in per_course:
        lid = pc["lid"]
        seen_lids.add(lid)
        m = meta.get(lid) or {"course": pc["course"], "fail_streak": 0, "last_homework": None}
        m["course"] = pc["course"]
        if pc["error"]:
            m["fail_streak"] = int(m.get("fail_streak", 0)) + 1
        else:
            m["fail_streak"] = 0
        # 记录该课「历史上见过的最新作业截止时间」
        rows = pc.get("_deadlines") or []
        if rows:
            latest = max(rows)
            if not m.get("last_homework") or latest > m["last_homework"]:
                m["last_homework"] = latest
        # 记录「连续多少轮扫描都是 0 条作业」（能进课程页但确实没作业）
        # 用于把「空课」也列进黑名单候选，交给用户判断 —— 不自动降频，
        # 因为有些课本学期就期末布置一次作业。
        if not pc.get("error") and pc.get("total", 0) == 0:
            m["empty_streak"] = int(m.get("empty_streak", 0)) + 1
        elif not pc.get("error"):
            m["empty_streak"] = 0
        m["last_scan"] = now.strftime("%Y-%m-%d %H:%M")
        m["last_error"] = pc.get("error")
        meta[lid] = m
    # 本次没扫到的课保留原元数据（别丢历史）
    sem_start = _semester_start(now)
    bl = blacklisted_lids()
    elec = elective_lids()
    candidates = []
    for lid, m in meta.items():
        if lid in bl or lid in elec:
            # 选修课是你明确要跟的课，不该再被列为"建议拉黑"候选（拉黑=连个人提醒也没了）
            continue
        lh = m.get("last_homework")
        lh_dt = None
        if lh:
            try:
                lh_dt = datetime.strptime(lh, "%Y-%m-%d %H:%M")
            except ValueError:
                lh_dt = None
        stale = lh_dt is not None and lh_dt < sem_start
        never = lh_dt is None and int(m.get("fail_streak", 0)) >= STALE_FAIL_STREAK
        empty = (lh_dt is None
                 and int(m.get("empty_streak", 0)) >= EMPTY_CANDIDATE_STREAK)
        if stale or never or empty:
            if stale:
                reason, advisory = "stale", False
                detail = f"最近一次作业 {lh}（早于本学期 {sem_start:%Y-%m-%d}）"
            elif never:
                reason, advisory = "no-homework-entry", False
                detail = f"连续 {m.get('fail_streak')} 次扫描都没能进入「课程作业」"
            else:
                reason, advisory = "no-homework-yet", True
                detail = (f"能正常进入课程页，但连续 {m.get('empty_streak')} 轮扫描都是 0 条作业"
                          f"（可能本学期还没布置，也可能已结课）"
                          f"；若这门课本学期还有课，请勿拉黑")
            candidates.append({
                "lid": lid,
                "course": m.get("course", "?"),
                "reason": reason,
                "advisory": advisory,      # True = 仅供你参考，程序不建议拉黑
                "detail": detail,
                "last_homework": lh,
                "fail_streak": m.get("fail_streak", 0),
                "empty_streak": int(m.get("empty_streak", 0)),
                "scanned": lid in seen_lids,
            })
    # 建议拉黑的（advisory=False）排前面，仅供参考的空课排后面，界面读起来更清楚
    candidates.sort(key=lambda c: (bool(c.get("advisory")), c.get("last_homework") or ""))
    save_index_meta(meta)
    return {"meta": meta, "candidates": candidates, "semester_start": sem_start.strftime("%Y-%m-%d")}


def sweep_all_courses(ctx, page, debug=False, delay=None, skip_lids=None,
                      want_tests=False, test_skip_lids=None,
                      scan_id: str | None = None, scope: str | None = None,
                      index_version: str = "", only_lids=None) -> tuple:
    """逐门课抓「课程作业」（可选「在线测试」），一次进课程页把两个栏目都取下来。

    skip_lids:      整体拉黑的课，直接跳过（不算失败、不重试）
    test_skip_lids: 只屏蔽在线测试的课（作业照抓）
    want_tests:     是否同时抓在线测试
    返回 (homeworks, tests, per_course, errors, skipped, test_notes)
    """
    if delay is None:
        delay = cache_store.cache_settings()["course_gap_sec"]
    index = load_course_index()
    if only_lids:
        wanted = {str(x) for x in only_lids}
        index = {lid: name for lid, name in index.items() if lid in wanted}
    if not index:
        return ([], [], [],
                ["课程索引为空：请先跑 scrape.py --refresh-courses 或用控制台「刷新课程表」"],
                [], {})

    skip_lids = set(skip_lids or ())
    test_skip_lids = set(test_skip_lids or ())
    auto_test_skip = set()
    skipped = [{"lid": k, "course": index.get(k, "?")} for k in skip_lids if k in index]
    homeworks, tests, per_course, errors = [], [], [], []
    # 手工名单与「自动判定无测试栏目」分开记，避免报告里混成一类
    manual_test_skip = set(load_test_blacklist().get("courses", {}).keys())
    test_notes = {"skipped": [], "manual_skipped": [], "no_entry": [],
                  "auto_skipped": [], "errors": []}
    meta = load_index_meta()
    now_dt = datetime.now()
    for lid in index:
        if no_test_skip_is_fresh(meta.get(lid) or {}, now_dt):
            auto_test_skip.add(lid)
    prev_ids = None      # 兜底：连续两门课内容完全相同 => 疑似没切课
    for lid, name in index.items():
        if lid in skip_lids:
            continue
        want_t = (bool(want_tests) and lid not in test_skip_lids
                  and lid not in auto_test_skip)
        if want_tests and lid in test_skip_lids:
            test_notes["skipped"].append(name)
            if lid in manual_test_skip:
                test_notes["manual_skipped"].append(name)
        if want_tests and lid in auto_test_skip:
            test_notes["auto_skipped"].append(name)
        try:
            rows, trows, notes, statuses = fetch_course_both(
                ctx, page, lid, name, debug, want_homework=True,
                want_tests=want_t, return_status=True)
            if "no-test-link" in notes:
                auto_test_skip.add(lid)
            ids = tuple(sorted(str(r.get("hwtid")) for r in rows))
            if ids and ids == prev_ids:
                raise RuntimeError("内容与上一门课完全相同（疑似未切课）")
            prev_ids = ids
            unsub = [r for r in rows if r.get("submit_url")]
            for r in rows:
                r["course"] = name
                r["lid"] = lid
                r.setdefault("kind", "homework")
            for t in trows:
                t["course"] = name
                t["lid"] = lid
                t.setdefault("kind", "test")
            if not want_tests:
                tests_status = "disabled"
            elif lid in test_skip_lids:
                tests_status = "blocked"
            elif lid in auto_test_skip:
                tests_status = "no_entry"
            else:
                tests_status = statuses.get("tests", "invalid")
            if tests_status in ("no_entry", "empty"):
                test_notes["no_entry"].append(name)
            if tests_status in ("invalid", "incomplete", "auth", "error"):
                test_notes["errors"].append(name)
            homeworks.extend(rows)
            tests.extend(trows)
            per_course.append({
                "lid": lid, "course": name, "total": len(rows),
                "unsubmitted": len(unsub), "error": None,
                "tests": len(trows),
                "homework_status": statuses.get("homework"),
                "tests_status": tests_status,
                "notes": notes,
                "_deadlines": [r.get("deadline") for r in rows if r.get("deadline")],
            })
            if tests_status == "auth":
                cache_store.invalidate_scope_scans("认证上下文失效：测试栏目返回登录页", scope=scope)
                break
            if scan_id:
                cache_store.commit_course_scan(
                    scan_id, lid=lid, course=name, homeworks=rows, tests=trows,
                    homework_status=statuses.get("homework", "invalid"),
                    tests_status=tests_status, notes=notes,
                    index_version=index_version, scope=scope)
            # Record the test-column discovery result for its review period.
            m = meta.get(lid) or {}
            m["course"] = name
            if want_tests and lid not in test_skip_lids:
                m["no_test_checked_at"] = now_dt.strftime("%Y-%m-%d %H:%M")
                if tests_status == "no_entry":
                    m["no_test_streak"] = int(m.get("no_test_streak", 0)) + 1
                    if m["no_test_streak"] >= 2:
                        m["no_test_reason"] = "连续找不到「在线测试」栏目，复查周期内自动跳过（作业不受影响）"
                elif tests_status in ("ok", "empty"):
                    m["no_test_streak"] = 0
                    m.pop("no_test_reason", None)
            meta[lid] = m
            if unsub or trows:
                extra = f"，测试 {len(trows)} 条" if want_t and trows else ""
                print(f"  {name}: 作业 {len(rows)} 条（未交 {len(unsub)}）{extra}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            hw_status = "error"
            tests_status = ("error" if want_tests and lid not in test_skip_lids
                            else "blocked" if want_tests else "disabled")
            per_course.append({
                "lid": lid, "course": name, "total": 0,
                "unsubmitted": 0, "error": f"{type(e).__name__}: {e}",
                "homework_status": hw_status, "tests_status": tests_status,
                "_deadlines": [],
            })
            errors.append(f"{name}({lid}): {type(e).__name__}: {e}")
            if scan_id:
                try:
                    cache_store.commit_course_scan(
                        scan_id, lid=lid, course=name, homeworks=None, tests=None,
                        homework_status=hw_status, tests_status=tests_status,
                        homework_error=str(e)[:400], tests_error=str(e)[:400],
                        notes=[f"scan-error:{type(e).__name__}"],
                        index_version=index_version, scope=scope)
                except Exception as cache_exc:  # noqa: BLE001
                    print(f"  {name}: 写入缓存失败 {cache_exc}", file=sys.stderr)
            print(f"  {name}: 抓取失败 {type(e).__name__}: {e}", file=sys.stderr)
            # Once authentication is gone, every following course would produce
            # the same false failure.  Stop instead of burning the whole scan.
            if "认证上下文失效" in str(e) or page_is_login(page):
                cache_store.invalidate_scope_scans("认证上下文失效：课程扫描中断", scope=scope)
                print("  认证上下文已失效，停止本轮扫描。", file=sys.stderr)
                break
        time.sleep(delay)
    # 记住「没有测试栏目」的课，下次不再为它白等一遍
    if meta:
        save_index_meta(meta)
    if test_notes["auto_skipped"]:
        test_notes["auto_skip"] = sorted(set(test_notes["auto_skipped"]))
    # 落一份扫描快照：候选推导依赖它，且它不会被「刷新课程表」覆盖
    save_scan_snapshot(per_course)
    return homeworks, tests, per_course, errors, skipped, test_notes


# ---------------------------------------------------------------- 在线测试
# 「在线测试」与「课程作业」是两套页面，但规则一致（都按截止时间进待办）。
# 实测入口（2026-10-12）：
#   课程页左侧菜单「在线测试」href = ../../course_column_preview_transfer.jsp?tagbug=client&columnId=<每课不同>
#   → 注意真实层级是 /meol/jpk/course/，手拼 /meol/course_column_preview_transfer.jsp 会 404
#   → 落到 /meol/common/question/test/student/list.jsp?cateId=...&status=1
#   表格列：标题 | 开始时间 | 截止时间 | 允许次数 | 限制用时 | 开始测试 | 交卷 | 查看结果
#   「开始测试」是 <a onclick="return gotostart('150409458','client','new03')">，测试 id 带引号
TEST_START_RE = re.compile(r"gotostart\(\s*'?(\d+)'?")
TEST_URL = BASE + "/meol/common/question/test/student/testpaper.jsp?testId={tid}&strStyle=new03&tagbug=client"


def get_column_href(child, label: str, timeout: float = 30, retries: int = None):
    """通用：取课程页左侧菜单里某个栏目（如「在线测试」）的 href。

    用 textContent 匹配（隐藏元素的 innerText 为空）。
    timeout 是「最多尝试几次」，每次间隔 1 秒。
    retries 是「没找到时最多再试几次」的覆盖值：给个很小的值能让
    「这门课没有该栏目」快速返回，不必白等满超时
    （实测 高等数学A（I） 没有「在线测试」栏目，曾经一门课白等 40 秒）。
    """
    budget = float(timeout) if retries is None else max(0.0, float(retries) * 0.2)
    deadline = time.monotonic() + budget
    while True:
        href = child.evaluate(
            """(label) => {
                const a = [...document.querySelectorAll('a')].find(x =>
                    (x.textContent||'').replace(/\\s+/g,'') === label);
                return a ? a.href : null;
            }""",
            label,
        )
        if href:
            return href
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.15)


def parse_test_table(page):
    """解析在线测试列表页。返回 [{title, deadline, test_id, submit_url, ...}]。

    与作业表一样：有「开始测试」链接 = 还能做/还没做；「查看结果」链接 = 有答题记录。
    """
    rows = page.eval_on_selector_all(
        "table tr",
        """trs => trs.map(tr => [...tr.querySelectorAll('td')].map(td => {
            const a = td.querySelector('a');
            return {
                text: (td.textContent||'').trim().replace(/\\s+/g,' '),
                href: a ? a.href : null,
                onclick: a ? (a.getAttribute('onclick')||'') : ''
            };
        })).filter(r => r.length >= 6)""",
    )
    out = []
    for r in rows:
        title = (r[0]["text"] or "").strip()
        if not title or title in ("测试标题",) or "截止时间" in r[0]["text"]:
            continue
        deadline = norm_deadline(r[2]["text"] if len(r) > 2 else "")
        if not deadline:
            continue
        start_cell = r[5] if len(r) > 5 else {"href": None, "onclick": ""}
        m = TEST_START_RE.search(start_cell.get("onclick") or "")
        result_url = (r[7]["href"] if len(r) > 7 else None)
        out.append({
            "kind": "test",
            "title": title[:60],
            "deadline": deadline,
            "start_time": norm_deadline(r[1]["text"] if len(r) > 1 else ""),
            "allow_times": (r[3]["text"].strip() if len(r) > 3 else ""),
            "limit_minutes": (r[4]["text"].strip() if len(r) > 4 else ""),
            "test_id": m.group(1) if m else None,
            "content_url": (TEST_URL.format(tid=m.group(1)) if m else None),
            "submit_url": (start_cell.get("href") if start_cell.get("href") else None),
            "start_url": (TEST_URL.format(tid=m.group(1)) if m else None),
            "result_url": result_url,
            "can_start": bool(m),
        })
    return out


LOGIN_PAGE_MARKERS = ("统一身份认证登录", "用户名密码", "忘记/修改密码", "涉密不上网")
EMPTY_PAGE_MARKERS = ("暂无作业", "没有作业", "暂无数据", "没有数据", "暂无测试",
                      "没有测试", "暂无记录", "列表为空")


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=1500) or ""
    except Exception:  # noqa: BLE001
        return ""


def page_is_login(page) -> bool:
    try:
        parts = urlsplit(str(getattr(page, "url", "") or ""))
        path = parts.path.lower().rstrip("/")
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    # Match the destination path, not a return URL embedded in its query.
    if (path.endswith("/login") or "sso_login" in path
            or "experimental-auth-endpoint" in path):
        return True
    try:
        if page.eval_on_selector(
                "input[type='password']",
                "el => !!(el.getClientRects().length && "
                "getComputedStyle(el).visibility !== 'hidden' && !el.disabled)"):
            return True
    except Exception:  # noqa: BLE001
        pass
    text = _body_text(page)
    # THEOL's authenticated personal page retains a login/switch-account link.
    # Only its two specific account statistics override that incidental text;
    # generic course/logout words must not override an actual login page.
    personal_evidence = (host == "course.buct.edu.cn"
                         and path == "/meol/personal.do"
                         and "在线总时长" in text and "登录时间" in text)
    if personal_evidence:
        return False
    return any(marker in text for marker in LOGIN_PAGE_MARKERS)


def _table_evidence(page) -> dict:
    try:
        tables = page.eval_on_selector_all(
            "table",
            """tables => tables.map(table => {
                const rows = [...table.querySelectorAll('tr')].map(tr =>
                    [...tr.querySelectorAll('td,th')].map(x =>
                        (x.textContent||'').trim().replace(/\\s+/g,' '))
                        .filter(Boolean)).filter(r => r.length);
                return rows;
            }).filter(rows => rows.length)""")
    except Exception:  # noqa: BLE001
        tables = []
    return {"tables": tables}


def _pagination_evidence(page) -> dict:
    """Extract only pagination facts that can be checked without JS parsing."""
    text = _body_text(page)
    total_match = TOTAL_RECORDS_RE.search(text)
    pages_match = TOTAL_PAGES_RE.search(text)
    try:
        anchors = page.eval_on_selector_all(
            "a,button,input", """els => els.map(a => ({
                text:((a.textContent||a.value||'')+'').trim(),
                href:a.getAttribute('href')||'', onclick:a.getAttribute('onclick')||'',
                type:a.getAttribute('type')||'', class:a.getAttribute('class')||'',
                name:a.getAttribute('name')||'', id:a.getAttribute('id')||'',
                disabled:a.hasAttribute('disabled') || a.disabled === true}))""")
    except Exception:  # noqa: BLE001
        anchors = []
    current_url = str(getattr(page, "url", "") or "")
    current_page, explicit_terminal = _current_list_page(current_url)
    labeled_next: list[str] = []
    numeric_candidates: list[tuple[int, str]] = []
    terminal_links: list[str] = []
    unknown_next = False

    for anchor in anchors or []:
        label = re.sub(r"\s+", "", str(anchor.get("text") or ""))
        href = str(anchor.get("href") or "").strip()
        onclick = str(anchor.get("onclick") or "").strip()
        if anchor.get("disabled"):
            continue
        if _is_page_size_control(anchor, label, href, onclick):
            continue
        lowered_href = href.lower()
        usable_href = bool(href) and not lowered_href.startswith(
            ("javascript:", "mailto:", "data:", "#"))

        page_no = None
        next_query = False
        if usable_href:
            try:
                target_url = urljoin(current_url, href)
                page_no = _query_page_number(target_url)
                query = parse_qs(urlsplit(target_url).query, keep_blank_values=True)
                next_query = any(str(v).strip().lower() in ("next", "nextpage")
                                 for v in query.get("s_page", []))
            except ValueError:
                page_no = None

        is_next = any(x in label for x in NEXT_PAGE_LABELS) or next_query
        is_prev = "上一页" in label or "上页" in label
        is_terminal = any(x in label for x in TERMINAL_PAGE_LABELS)
        if is_terminal:
            terminal_links.append(href)
            continue
        if is_prev:
            continue
        numeric_next = (page_no is not None and
                        ((current_page is None and page_no > 1)
                         or (current_page is not None and page_no > current_page)))
        if is_next:
            if usable_href:
                labeled_next.append(href)
            else:
                unknown_next = True
            continue
        if numeric_next:
            if usable_href:
                numeric_candidates.append((page_no, href))
            continue
        if not usable_href and onclick and re.search(
                r"(?:next|gotopage|s_gotopage|pagingpage\s*=|"
                r"(?<![a-z])page\s*=|翻页)", onclick, re.I):
            unknown_next = True

    if labeled_next:
        next_links = labeled_next
    elif numeric_candidates:
        nearest = min(page_no for page_no, _href in numeric_candidates)
        next_links = [href for page_no, href in numeric_candidates
                      if page_no == nearest]
    else:
        next_links = []
    return {
        "total_records": int(total_match.group(1)) if total_match else None,
        "total_pages": int(pages_match.group(1)) if pages_match else None,
        "next_links": next_links,
        "unknown_next": unknown_next,
        "current_page": current_page,
        "explicit_terminal": explicit_terminal,
        "terminal_links": terminal_links,
        "has_terminal": bool(terminal_links) or explicit_terminal,
    }


def _pagination_block_reason(page) -> str:
    """Return why pagination cannot be followed, or an empty string."""
    evidence = _pagination_evidence(page)
    if evidence["unknown_next"]:
        return "存在无法解析的下一页/JavaScript 分页控件，未确认完整"
    if evidence["next_links"]:
        current_url = str(getattr(page, "url", "") or "")
        for href in evidence["next_links"]:
            _target, reason = _safe_next_target(current_url, href, "")
            if reason:
                return reason
        return ""
    total_pages = evidence["total_pages"]
    if total_pages is not None and total_pages > 1:
        current_page = evidence["current_page"]
        if evidence["explicit_terminal"] or (
                current_page is not None
                and current_page >= total_pages):
            return ""
        return (f"页脚称共 {total_pages} 页，但当前页没有可安全跟随的"
                "下一页")
    if (evidence["has_terminal"] and not evidence["explicit_terminal"]
            and total_pages is None and evidence["total_records"] is None):
        return "只有尾页链接，缺少总记录数/总页数等可靠末页证据，未确认完整"
    return ""


def pagination_uncertain(page) -> bool:
    """True only when pagination is present but cannot be followed safely."""
    return bool(_pagination_block_reason(page))


def classify_list_page(page, kind: str, rows: list[dict]) -> tuple[str, str]:
    """Return (status, evidence): ok/empty/invalid/incomplete/auth.

    An empty table with valid headers and no pagination is a legitimate empty
    snapshot.  A blank/broken/login page is never promoted to "empty".
    """
    if page_is_login(page):
        return "auth", "页面跳到了登录/认证页"
    evidence = _table_evidence(page)
    target = None
    for table in evidence["tables"]:
        header = " ".join(str(x) for x in (table[0] if table else []))
        if "标题" in header and "截止时间" in header:
            target = table
            break
    text = _body_text(page)
    header_ok = target is not None
    total_match = TOTAL_RECORDS_RE.search(text)
    footer_total = int(total_match.group(1)) if total_match else None
    explicit_empty = (any(marker in text for marker in EMPTY_PAGE_MARKERS)
                      or footer_total == 0)
    pagination_reason = _pagination_block_reason(page)
    if pagination_reason:
        return "incomplete", pagination_reason
    if not header_ok:
        if evidence["tables"] or rows:
            return "invalid", "未找到带「标题/截止时间」的目标表格"
        return "invalid", "列表结构或空态证据不足"
    data_rows = max(0, len(target) - 1)
    if data_rows == 0:
        if explicit_empty:
            return "empty", "目标表头有效且页面明确显示空态"
        if footer_total is not None:
            return "incomplete", f"目标表头无数据行，但页脚称共 {footer_total} 条"
        return "invalid", "只有目标表头，没有数据行或明确空态证据"
    if footer_total is not None and footer_total == 0:
        return "invalid", "页面有数据行但页脚总记录数为 0"
    if footer_total is not None and footer_total < data_rows:
        return "incomplete", f"页脚总记录数 {footer_total} 小于本页 {data_rows} 行"
    if not rows:
        return "incomplete", f"目标表有 {data_rows} 个数据行，但一条也未解析"
    if len(rows) < data_rows:
        return "incomplete", f"仅解析 {len(rows)}/{data_rows} 行，禁止覆盖旧缓存"
    if explicit_empty:
        return "invalid", "页面同时出现数据行和空态标记，结构不一致"
    if kind == "test" and not any(str(r.get("test_id") or "") for r in rows):
        return "incomplete", "测试数据行缺少测试 ID"
    if kind == "homework" and not any(str(r.get("hwtid") or "") for r in rows):
        return "incomplete", "作业数据行缺少作业 ID"
    if rows:
        return "ok", f"{len(rows)} 行，目标表头有效"
    if explicit_empty:
        return "empty", "页面明确显示空态"
    return "invalid", "列表结构或空态证据不足"


def validate_course_context(page, lid: str, course_name: str = "") -> tuple[bool, str]:
    """Reject login/foreign pages and require positive evidence for *this* course."""
    url = str(getattr(page, "url", "") or "")
    if page_is_login(page):
        return False, "认证上下文失效"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001
        parts = None
        host = ""
    host_ok = (host == "buct.edu.cn" or host.endswith(".buct.edu.cn")
               or host in ("127.0.0.1", "localhost", "::1"))
    if not host or not host_ok:
        return False, f"课程上下文域名异常：{host}"
    lid = str(lid)
    query = parse_qs(parts.query if parts else "")
    ids = set(query.get("lid", [])) | set(query.get("courseId", []))
    page_path = (parts.path if parts else "").lower()
    if lid in ids:
        # The course entry URL itself is authoritative only when it is a known
        # course page; unrelated pages can carry a decorative lid parameter.
        if ("enter_course" in page_path or "course_index" in page_path
                or "course_column" in page_path or "hwtask" in page_path
                or page_path == "/meol/jpk/course/layout/newpage/index.jsp"):
            return True, ""
    try:
        body = page.inner_text("body", timeout=1500) or ""
    except Exception:  # noqa: BLE001
        body = ""
    try:
        links = page.eval_on_selector_all(
            "a", "els => els.map(a => (a.getAttribute('href')||'') + ' ' + "
                 "(a.getAttribute('onclick')||''))")
    except Exception:  # noqa: BLE001
        links = []
    needle = re.compile(rf"(?:lid|courseId)={re.escape(lid)}(?:\D|$)")
    # A foreign page can contain a link to this course.  Require the page to be
    # a course/hwtask page and, when a name is supplied, show that course name.
    context_path = any(x in page_path for x in
                       ("enter_course", "course_index", "course_column", "hwtask"))
    name_ok = bool(course_name and course_name in body)
    if context_path and name_ok and any(needle.search(str(x)) for x in links or []):
        return True, ""
    return False, f"页面没有课程 {lid} 的上下文证据（可能串课或未切换成功）"


def wait_text(page, keyword: str, timeout: float = 30) -> bool:
    """Wait up to *timeout seconds* on a monotonic clock (not loop count)."""
    budget = max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    while True:
        left = max(0.05, deadline - time.monotonic())
        try:
            if keyword in (page.inner_text("body", timeout=min(1500, int(left * 1000))) or ""):
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.12, max(0.02, deadline - time.monotonic())))


def wait_authenticated(page, ctx, timeout: float = 12) -> bool:
    """Wait for positive evidence in the *page*, never for cookies alone."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            if page_authenticated(page):
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


AUTH_PAGE_MARKERS = ("我的课程", "课程作业", "在线总时长", "登录时间",
                     "个人中心", "注销", "退出")


def page_authenticated(page) -> bool:
    """Positive page evidence for an authenticated platform session."""
    if page_is_login(page):
        return False
    try:
        parts = urlsplit(str(getattr(page, "url", "") or ""))
        host = (parts.hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001
        return False
    if not (host == "buct.edu.cn" or host.endswith(".buct.edu.cn")
            or host in ("127.0.0.1", "localhost", "::1")):
        return False
    text = _body_text(page)
    if any(marker in text for marker in AUTH_PAGE_MARKERS):
        return True
    # Course pages sometimes expose the personal header only through a stable
    # element rather than body text.
    try:
        return bool(page.eval_on_selector(
            "#userName,.userName,.user-name,.user-info,.userInfo",
            "el => (el.textContent||'').trim().length > 0"))
    except Exception:  # noqa: BLE001
        return False


def _authenticated_personal_identity_page(page) -> bool:
    """Only the protected personal account page may supply a body fallback."""
    try:
        parts = urlsplit(str(getattr(page, "url", "") or ""))
        host = (parts.hostname or "").lower().rstrip(".")
        path = (parts.path or "").rstrip("/")
    except ValueError:
        return False
    if not _host_is_platform(host) or path != "/meol/personal.do":
        return False
    text = _body_text(page)
    return ("在线总时长" in text and "登录时间" in text) or page_authenticated(page)


def _clean_identity_marker(value: str) -> str | None:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value or "教师姓名" in value or "教师信息" in value:
        return None
    return value[:120]


def platform_identity(page) -> str | None:
    """Return only a trusted, non-credential visible account marker."""
    try:
        parts = urlsplit(str(getattr(page, "url", "") or ""))
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not _host_is_platform(host) or page_is_login(page):
        return None

    try:
        value = page.eval_on_selector(
            IDENTITY_SELECTORS,
            """el => (el.textContent||el.getAttribute('title')||'')
                .replace(/\\s+/g,' ').trim()""")
    except Exception:  # noqa: BLE001
        value = ""
    marker = _clean_identity_marker(str(value or ""))
    if marker:
        return marker

    if not _authenticated_personal_identity_page(page):
        return None
    text = _body_text(page)
    match = PERSONAL_IDENTITY_RE.search(text)
    return _clean_identity_marker(match.group(1)) if match else None


def resolve_platform_identity(page) -> str | None:
    """Read this account's protected profile when the personal header lacks an ID.

    Only follow the own-profile link observed on an authenticated personal page;
    course teacher profiles and arbitrary URLs must never supply an identity.
    Keep the caller's page in place so course discovery still starts there.
    """
    marker = platform_identity(page)
    if marker or not _authenticated_personal_identity_page(page) or page_is_login(page):
        return marker
    profile = None
    try:
        href = page.eval_on_selector(
            '.name a.info.chgright[href]', "el => el.getAttribute('href')")
        if not href:
            return None
        target = urljoin(page.url, str(href))
        def allowed(url):
            parts = urlsplit(url)
            base = urlsplit(BASE)
            return (parts.scheme, parts.netloc, parts.path) == (
                base.scheme, base.netloc, '/meol/popups/viewstudent_info.jsp')
        if not allowed(target):
            return None
        profile = page.context.new_page()
        profile.goto(target, wait_until='domcontentloaded', timeout=12000)
        if not allowed(profile.url) or page_is_login(profile):
            return None
        pairs = profile.eval_on_selector_all('tr', """rows => rows.flatMap(r => {
            const cells = [...r.cells];
            return cells.flatMap((c,i) => c.tagName === 'TH' && cells[i+1]?.tagName === 'TD'
                ? [[c.textContent.trim(), cells[i+1].textContent.trim()]] : []);
        })""")
        # Prefer stable account identifiers. A display name alone is insufficient.
        for label in ('用户名', '学号'):
            values = {str(value).strip() for key, value in pairs
                      if re.sub(r'[\s:：]', '', str(key)) == label and str(value).strip()}
            if len(values) > 1:
                return None
            if values:
                value = values.pop()
                return value if re.fullmatch(r'[A-Za-z0-9_.@-]{1,96}', value) else None
        return None
    except Exception:  # noqa: BLE001 -- missing/changed profile keeps publishing blocked
        return None
    finally:
        if profile is not None:
            profile.close()


def bind_page_identity(page, scope: str | None = None):
    """Bind the visible account marker on every authenticated entry point."""
    marker = resolve_platform_identity(page)
    if not marker:
        return None
    return cache_store.bind_identity(marker, scope=scope)


def resolve_want_tests(explicit: bool | None, deadline_only: bool) -> bool:
    """Explicit --no-tests/--with-tests wins; only unspecified follows mode."""
    if explicit is not None:
        return bool(explicit)
    return bool(deadline_only)


def get_hw_href(child, timeout: float = 30):
    """轮询取「课程作业」的绝对 URL（子菜单默认隐藏，textContent 才可靠）。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        href = child.evaluate("""() => {
            const a = [...document.querySelectorAll('a')].find(x =>
                (x.textContent||'').replace(/\\s+/g,'') === '课程作业');
            return a ? a.href : null;
        }""")
        if href:
            return href
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def parse_hw_table(page):
    rows = page.eval_on_selector_all(
        "table tr",
        """trs => trs.map(tr => [...tr.querySelectorAll('td,th')].map(td => {
            const a = td.querySelector('a');
            return {
                text: (td.textContent||'').trim().replace(/\\s+/g,' '),
                href: a ? a.href : null
            };
        }))""",
    )
    out = []
    for r in rows:
        if len(r) < 2:
            continue
        if r[0]["text"] in ("标题", "作业标题：") or "截止时间" in (r[0]["text"] + r[1]["text"]):
            continue
        # 标题单元格里有 hwtid 与内容页链接
        title_cell = r[0]
        mh = re.search(r"hwtid=(\d+)", title_cell["href"] or "")
        if not mh:
            continue
        hwtid = mh.group(1)
        deadline = norm_deadline(r[1]["text"]) if len(r) > 1 else ""
        if not deadline:
            continue
        out.append({
            "title": title_cell["text"].strip()[:60],
            "deadline": deadline,
            "publisher": (r[3]["text"].strip() if len(r) > 3 else ""),
            "hwtid": hwtid,
            "content_url": title_cell["href"],                       # hwtask.view.jsp?hwtid=
            "submit_url": (r[5]["href"] if len(r) > 5 else None),    # write.jsp（未提交/可重交）
            "result_url": (r[6]["href"] if len(r) > 6 else None),    # taskanswer.jsp（已提交）
        })
    return out


def fetch_course(ctx, page, lid: str, debug: bool):
    """进入课程 -> 作业 -> 解析。"""
    # 窗口名必须唯一：同名会被浏览器复用，不再触发新 page 事件
    with ctx.expect_page(timeout=30000) as newp:
        page.evaluate(
            f"window.open('./lesson/enter_course.jsp?lid={lid}&t=hw','course_win_{lid}')")
    child = newp.value
    try:
        if not wait_text(child, "课程作业", timeout=30):
            return []
        href = get_hw_href(child)
        if not href:
            return []
        child.goto(href, wait_until="domcontentloaded", timeout=60000)
        wait_text(child, "截止时间", timeout=20)
        time.sleep(1.5)
        dump(child, f"hw_{lid}", debug)
        return parse_hw_table(child)
    finally:
        try:
            child.close()
        except Exception:  # noqa: BLE001
            pass


def html_to_text(s: str) -> str:
    """Convert already-decoded HTML once through the structured parser."""
    return content_pipeline.flatten_blocks_text(
        content_pipeline.parse_blocks(s or "", BASE))


def get_hw_content(ctx, hwtid: str, debug: bool = False,
                   lid: str | None = None, course_name: str = "") -> dict:
    """抓取某条作业的详情正文。

    平台把正文放在隐藏域 `<input type="hidden" name="N_content" value="实体编码的HTML">`，
    与 ueditor iframe 同步，无需进 iframe 即可拿到，解码后即为作业题目/要求。
    """
    if not lid:
        return {"ok": False, "hwtid": hwtid,
                "error": "缺少所属课程 lid，不能在明确课程上下文中读取正文"}
    url = BASE + f"/meol/common/hw/student/hwtask.view.jsp?hwtid={hwtid}"
    course_url = f"{BASE}/meol/lesson/enter_course.jsp?lid={lid}&t=hw"
    page = ctx.new_page()
    try:
        course_deadline = time.monotonic() + max(
            5.0, float(cache_store.cache_settings()["course_timeout_sec"]))
        page.goto(course_url, wait_until="domcontentloaded", timeout=_budget_ms(course_deadline))
        if page_is_login(page):
            cache_store.invalidate_scope_scans("认证上下文失效：正文课程入口返回登录页")
            return {"ok": False, "hwtid": hwtid,
                    "error": "认证上下文失效：进入所属课程时跳到了登录页", "url": course_url}
        if not wait_text(page, "课程作业",
                         timeout=min(12.0, max(0.1, course_deadline - time.monotonic()))):
            return {"ok": False, "hwtid": hwtid,
                    "error": "进入所属课程失败：课程页未就绪", "url": course_url}
        valid, why = validate_course_context(page, str(lid), course_name)
        if not valid:
            return {"ok": False, "hwtid": hwtid,
                    "error": f"进入所属课程失败：{why}", "url": course_url}
        try:
            bind_page_identity(page)
        except cache_store.AccountIdentityConflict as exc:
            cache_store.invalidate_scope_scans(str(exc))
            return {"ok": False, "hwtid": hwtid, "error": str(exc), "url": course_url}
        _ensure_budget(course_deadline, "进入正文所属课程")
        page.goto(url, wait_until="domcontentloaded", timeout=_budget_ms(course_deadline))
        dump(page, f"content_{hwtid}", debug)
        inner = None
        deadline = time.monotonic() + 6.0
        while True:
            src = page.content()
            inner = content_pipeline.extract_hidden_content(src)
            if inner is not None or time.monotonic() >= deadline:
                break
            if page_is_login(page):
                cache_store.invalidate_scope_scans("认证上下文失效：正文页返回登录页")
                return {"ok": False, "hwtid": hwtid,
                        "error": "认证上下文失效：作业正文页跳到了登录页", "url": url}
            time.sleep(0.2)
        if inner is None:
            return {"ok": False, "hwtid": hwtid,
                    "error": "未找到作业正文（页面结构可能变化，或会话已失效需重新登录）", "url": url}
        return {
            "ok": True, "hwtid": hwtid, "url": url,
            "content_html": inner, "content_text": html_to_text(inner),
        }
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def fetch_content_for_item(ctx, item: dict, *, force: bool = False,
                           debug: bool = False) -> dict:
    """Load one homework body and cache structured blocks + safe images."""
    item_key = item.get("item_key")
    if not item_key:
        return {"ok": False, "error": "缓存条目缺少 item_key"}
    if (not force and item.get("content_ready") and not item.get("content_stale")
            and not item.get("content_error")):
        return {"ok": True, "cached": True, "item_key": item_key}
    hwtid = str(item.get("hwtid") or "")
    if not hwtid:
        return {"ok": False, "error": "该条没有 hwtid"}
    try:
        raw = get_hw_content(ctx, hwtid, debug,
                             lid=str(item.get("lid") or ""),
                             course_name=str(item.get("course") or ""))
        if not raw.get("ok"):
            cache_store.record_content_error(
                item_key, raw.get("error") or "正文获取失败",
                source_url=raw.get("url"), scope=item.get("account_scope"),
                expected_identity_generation=item.get("identity_generation"))
            return {"ok": False, "item_key": item_key,
                    "error": raw.get("error") or "正文获取失败"}
        base_url = raw.get("url") or item.get("content_url") or BASE
        result = content_pipeline.ingest_html(
            raw.get("content_html") or "", base_url,
            session=content_pipeline.PlaywrightAssetSession(ctx))
        saved = cache_store.save_content(
            item_key, result, scope=item.get("account_scope"),
            expected_identity_generation=item.get("identity_generation"))
        return {"ok": True, "item_key": item_key,
                "source_version": saved.get("source_version"),
                "blocks": len(result.get("blocks") or []),
                "assets": len(result.get("assets") or [])}
    except Exception as exc:  # noqa: BLE001
        cache_store.record_content_error(
            item_key, f"{type(exc).__name__}: {exc}",
            source_url=item.get("content_url"), scope=item.get("account_scope"),
            expected_identity_generation=item.get("identity_generation"))
        return {"ok": False, "item_key": item_key,
                "error": f"{type(exc).__name__}: {exc}"}


def populate_content_after_scan(ctx, *, limit: int | None = None,
                                window_days: int | None = None,
                                force: bool = False, debug: bool = False,
                                item_keys=None) -> dict:
    """Fill due bodies using the already authenticated context (never tests)."""
    settings = cache_store.cache_settings()
    limit = (settings["content_limit"] if limit is None
             else (None if int(limit) <= 0 else int(limit)))
    window_days = (settings["content_window_days"] if window_days is None
                   else max(0, int(window_days)))
    items = cache_store.content_due(
        days=window_days, limit=limit, force=force, item_keys=item_keys,
        exclude_lids=blacklisted_lids(),
        exclude_test_lids=test_blacklisted_lids())
    ok = failed = cached = 0
    for item in items:
        result = fetch_content_for_item(ctx, item, force=force, debug=debug)
        if result.get("ok"):
            ok += 1
            cached += 1 if result.get("cached") else 0
        else:
            failed += 1
        print(f"  正文 {item.get('course', '?')}｜{item.get('title', '?')}: "
              f"{'OK' if result.get('ok') else '失败 ' + str(result.get('error', ''))[:120]}",
              file=sys.stderr)
    return {"requested": len(items), "ok": ok, "failed": failed, "cached": cached}


SCAN_SNAPSHOT_FILE = COURSES_FILE.with_name("courses.scan.json")


def save_scan_snapshot(per_course: list, now=None) -> None:
    """把本轮扫描的每课原始证据落一份独立快照。

    与 courses.json 的 meta 分开存，专门保证「候选推导」有数据可用：
    meta 会被刷新课程表等操作整体覆盖，而这份快照只在真正扫描后重写。
    """
    now = now or datetime.now()
    try:
        SCAN_SNAPSHOT_FILE.write_text(json.dumps({
            "scanned_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "semester_start": _semester_start(now).strftime("%Y-%m-%d"),
            "courses": [{k: v for k, v in pc.items() if not k.startswith("_")}
                        | {"_deadlines": pc.get("_deadlines") or []}
                        for pc in per_course],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_scan_snapshot() -> dict:
    try:
        return json.loads(SCAN_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def candidates_from_scan(scan: dict) -> list:
    """由扫描证据直接推导候选（不依赖 meta）。

    规则（与 update_index_meta 保持一致）：
      · 该课最新一次作业早于本学期起点            -> stale（建议拉黑）
      · 该课抓取出错                              -> no-homework-entry（建议拉黑）
      · 该课能进页面但 0 条作业                    -> no-homework-yet（仅供参考）
    """
    if not scan:
        return []
    try:
        sem_start = datetime.strptime(scan["semester_start"], "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        sem_start = _semester_start()
    out = []
    for pc in scan.get("courses") or []:
        lid = pc.get("lid")
        if not lid:
            continue
        name = pc.get("course") or "?"
        deadlines = [d for d in (pc.get("_deadlines") or []) if d]
        latest = max(deadlines) if deadlines else None
        entry = {
            "lid": lid, "course": name,
            "last_homework": latest,
            "fail_streak": 1 if pc.get("error") else 0,
            # 由本轮证据决定；历史累计由 update_index_meta/meta 负责，这里只表达"本轮是空课"
            "empty_streak": 1 if (not pc.get("error") and not pc.get("total")) else 0,
            "scanned": True, "from": "scan",
            "last_error": pc.get("error"),
        }
        if pc.get("error"):
            entry["reason"] = "no-homework-entry"
            entry["advisory"] = False
            entry["detail"] = f"本轮扫描未能进入「课程作业」：{str(pc['error'])[:60]}"
            out.append(entry)
            continue
        if latest:
            try:
                dt = datetime.strptime(latest, "%Y-%m-%d %H:%M")
            except ValueError:
                dt = None
            if dt is not None and dt < sem_start:
                entry["reason"] = "stale"
                entry["advisory"] = False
                entry["detail"] = f"最近一次作业 {latest}，早于本学期 {sem_start:%Y-%m-%d}"
                out.append(entry)
            continue
        # 没有任何作业记录：能进页面 -> 仅供参考的空课
        entry["reason"] = "no-homework-yet"
        entry["advisory"] = True
        entry["detail"] = ("本轮扫描能正常进入课程页但 0 条作业"
                           "（可能本学期还没布置，也可能已结课）；"
                           "若这门课本学期还有课，请勿拉黑")
        out.append(entry)
    return out


def build_blacklist_candidates(now=None) -> dict:
    """只读地算出当前的建议拉黑候选（不改任何文件）。

    规则（三选一即候选）：
      stale             —— 该课最近一次作业截止时间早于本学期起点（8/1 或 2/1）→ 建议拉黑
      no-homework-entry —— 连续 >=2 次扫描都没能进入「课程作业」，且从未见过作业 → 建议拉黑
      no-homework-yet   —— 能正常进入、但连续 >=2 轮都是 0 条作业 → 仅供参考（advisory）
                            有些课本学期就期末布置一次作业，是否拉黑由用户决定

    ⚠️ 这里的三条规则必须与 update_index_meta() 里的保持一致 ——
       那个在扫描时算、这个在 CLI/界面读取时算，两处不一致就会出现「种了数据却看不到」。
    """
    now = now or datetime.now()
    sem_start = _semester_start(now)
    meta = load_index_meta()
    # 先读选修课：它会把旧版写在黑名单里的 reason=elective 迁出去，
    # 所以必须排在读黑名单之前，否则这里拿到的还是一份「选修课仍在黑名单里」的旧数据。
    elec = elective_lids()
    bl = load_blacklist().get("courses", {})
    index = load_course_index()

    # 优先用「扫描快照」的候选证据；meta 只作为兜底。
    # 原因：meta 会被「刷新课程表」等操作整体覆盖成空，导致候选凭空消失。
    scan = load_scan_snapshot()
    from_scan = {c["lid"]: c for c in candidates_from_scan(scan)} if scan else {}

    candidates, kept = [], []
    for lid, name in index.items():
        m = meta.get(lid) or {}
        lh = m.get("last_homework")
        lh_dt = None
        if lh:
            try:
                lh_dt = datetime.strptime(lh, "%Y-%m-%d %H:%M")
            except ValueError:
                lh_dt = None
        sc = from_scan.get(lid) or {}
        # 扫描证据优先：它能反映"刚刚这一轮"的真实情况，且不会被覆盖掉
        if sc:
            lh = sc.get("last_homework") or lh
            if lh:
                try:
                    lh_dt = datetime.strptime(lh, "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
        entry = {
            "lid": lid,
            "course": name,
            "blacklisted": lid in bl,
            "elective": lid in elec,
            "last_homework": lh,
            "fail_streak": max(int(m.get("fail_streak", 0)), int(sc.get("fail_streak", 0))),
            "empty_streak": max(int(m.get("empty_streak", 0)), int(sc.get("empty_streak", 0))),
            "last_error": m.get("last_error") or sc.get("last_error"),
            "from": "scan" if sc else "meta",
        }
        if lid in bl or lid in elec:
            continue
        if lh_dt is not None and lh_dt < sem_start:
            entry["reason"] = "stale"
            entry["advisory"] = False
            entry["detail"] = f"最近一次作业 {lh}，早于本学期 {sem_start:%Y-%m-%d}"
            candidates.append(entry)
        elif lh_dt is None and entry["fail_streak"] >= STALE_FAIL_STREAK:
            entry["reason"] = "no-homework-entry"
            entry["advisory"] = False
            entry["detail"] = f"连续 {entry['fail_streak']} 次扫描都没进入「课程作业」"
            candidates.append(entry)
        elif (lh_dt is None
              and (sc.get("reason") == "no-homework-yet"
                   or entry["empty_streak"] >= EMPTY_CANDIDATE_STREAK)):
            # 扫描证据优先：本轮快照显示是空课就直接列出来（不必等累计 2 轮）。
            # empty_streak 设为至少 1，界面显示「连续 N 轮」时不会出现 0。
            entry["empty_streak"] = max(entry["empty_streak"], 1)
            entry["reason"] = "no-homework-yet"
            entry["advisory"] = True
            entry["detail"] = (f"能正常进入课程页，但连续 {entry['empty_streak']} 轮扫描都是 0 条作业"
                               f"（可能本学期还没布置，也可能已结课）；若这门课本学期还有课，请勿拉黑")
            candidates.append(entry)
        else:
            kept.append(entry)
    for c in candidates:
        if c.get("elective"):
            # 拉黑 = 完全不抓；选修课标记 = 只发个人。两者叠加会把"个人那条"也一起消掉。
            c["detail"] = (c.get("detail") or "") + \
                "；⚠️ 这门课已标记为选修课，拉黑后连「个人小群那条」也不会再有"
    candidates.sort(key=lambda c: (bool(c.get("advisory")), c.get("last_homework") or ""))
    return {
        "semester_start": sem_start.strftime("%Y-%m-%d"),
        "candidates": candidates,
        "blacklist": bl,
        "electives": elec,
        "total_courses": len(index),
    }


def resolve_lid(token: str, index: dict) -> str:
    """把「lid」或「课程名的一部分」解析成 lid；找不到或有歧义返回 None。

    为什么要按名字找：课程名像「嵌入式系统创新应用与实践」，手打 lid 太麻烦。
    只在唯一匹配时才返回，多个匹配宁可报错让人写全 —— 拉错课会漏掉整门课的提醒，
    这个代价比多敲几个字大得多。
    """
    token = (token or "").strip()
    if not token:
        return None
    if token in index:                      # 直接给了 lid
        return token
    # 先精确同名，再退化为子串匹配
    for lid, name in index.items():
        if name == token:
            return lid
    hits = [(lid, name) for lid, name in index.items() if token.lower() in name.lower()]
    if len(hits) == 1:
        return hits[0][0]
    if len(hits) > 1:
        print(f"「{token}」匹配到 {len(hits)} 门课，请写得更具体或直接用 lid：",
              file=sys.stderr)
        for lid, name in hits:
            print(f"    [{lid}] {name}", file=sys.stderr)
    return None


def _blacklist_cli(args) -> int:
    """处理黑名单相关子命令。

    两级黑名单：
      课程黑名单       —— 该课整体跳过（作业 + 在线测试都不抓）
      测试黑名单       —— 只跳过「在线测试」，作业照常搜
    两者都强制人工确认，脚本永远不会自己写入。
    """
    info = build_blacklist_candidates()
    index = load_course_index()

    # ---------- 只屏蔽在线测试（作业照搜） ----------
    if args.test_blacklist_list:
        tb = load_test_blacklist().get("courses", {})
        print(f"【仅屏蔽在线测试】{len(tb)} 门（这些课的作业仍然照常搜）：")
        for lid, v in tb.items():
            print(f"  [{lid}] {v.get('name', index.get(lid, '?')):32s} {v.get('note', '')}"
                  f"  确认于 {v.get('confirmed_at', '?')}")
        if not tb:
            print("  （空）")
        print()
        print("加入：python scrape.py --test-blacklist-add <lid> ...")
        print("移出：python scrape.py --test-blacklist-remove <lid> ... （或 all）")
        return 0

    if args.test_blacklist_add:
        data = load_test_blacklist()
        added, unknown = [], []
        for lid in args.test_blacklist_add:
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
            added.append(lid)
        save_test_blacklist(data)
        print(f"已加入「仅屏蔽在线测试」 {len(added)} 门（作业继续搜）：")
        for lid in added:
            print(f"  [{lid}] {index[lid]}")
        if unknown:
            print(f"（跳过未知 lid：{', '.join(unknown)}）")
        return 0

    if args.test_blacklist_remove:
        data = load_test_blacklist()
        if "all" in args.test_blacklist_remove:
            n = len(data["courses"])
            data["courses"] = {}
            save_test_blacklist(data)
            print(f"已清空「仅屏蔽在线测试」名单（{n} 门）。")
            return 0
        removed = []
        for lid in args.test_blacklist_remove:
            lid = str(lid)
            if lid in data["courses"]:
                removed.append(f"[{lid}] {data['courses'][lid].get('name', '?')}")
                data["courses"].pop(lid)
        save_test_blacklist(data)
        print(f"已移出 {len(removed)} 门（这些课的在线测试恢复抓取）："
              + ("；".join(removed) if removed else "（无匹配）"))
        return 0

    if args.blacklist_list:
        bl = info["blacklist"]
        cands = info["candidates"]
        suggested = [c for c in cands if not c.get("advisory")]
        advisory = [c for c in cands if c.get("advisory")]
        print(f"学期起点 = {info['semester_start']} | 课程总数 = {info['total_courses']} "
              f"| 已拉黑 = {len(bl)} | 选修课（只发个人）= {len(info.get('electives') or {})} "
              f"| 建议拉黑 {len(suggested)} 门 "
              f"| 仅供参考（暂无作业）{len(advisory)} 门")
        print()
        print("【建议拉黑】确认后才会生效（往学期的课 / 进不去作业栏目）：")
        for c in suggested:
            tag = "陈旧" if c["reason"] == "stale" else "无入口"
            print(f"  [{c['lid']}] {c['course']:32s} {tag:8s} {c['detail']}")
        if not suggested:
            print("  （无）")
        print()
        print("【仅供参考 · 暂无作业】能正常进课程页但连续多轮 0 条 —— 是否拉黑由你决定：")
        print("  提醒：有些课本学期就期末布置一次作业，拉黑后会漏掉它。")
        for c in advisory:
            print(f"  [{c['lid']}] {c['course']:32s} 连续 {c['empty_streak']} 轮 0 条")
        if not advisory:
            print("  （无）")
        print()
        print("【已拉黑】扫描时跳过：")
        LBL = {"stale": "陈旧", "no-homework-entry": "无入口",
               "no-homework-yet": "暂无作业", "manual": "手动"}
        for lid, v in bl.items():
            reason = v.get("reason", "")
            print(f"  [{lid}] {v.get('name', index.get(lid, '?')):32s} "
                  f"{LBL.get(reason, reason):8s} 确认于 {v.get('confirmed_at', '?')}")
        if not bl:
            print("  （无）")
        elec = load_electives().get("courses", {})
        print()
        print(f"【选修课】照常抓取，但**只发给个人小群**，不进班级群提醒（{len(elec)} 门）：")
        for lid, v in elec.items():
            print(f"  [{lid}] {v.get('name', index.get(lid, '?')):32s} 确认于 {v.get('confirmed_at', '?')}")
        if not elec:
            print("  （无）")
        print()
        print("确认：python scrape.py --blacklist-confirm <lid> ...  （或 all-candidates）")
        print("选修：python scrape.py --blacklist-elective <lid 或 课程名片段> ...  （只发个人）")
        print("移除：python scrape.py --blacklist-remove <lid> ...  （或 all）")
        print("退选：python scrape.py --elective-remove <lid> ...  （或 all，恢复进班级群）")
        return 0

    if args.blacklist_elective:
        # 「选修课」= 换受众：课照抓，但班级群那条删掉、个人小群那条保留。
        # 它和黑名单是两个维度，所以写进 electives.json 而不是 course_blacklist.json。
        data = load_electives()
        added, unknown = [], []
        for token in args.blacklist_elective:
            lid = resolve_lid(token, index)
            if not lid:
                unknown.append(token)
                continue
            if lid in data["courses"]:
                continue
            data["courses"][lid] = {
                "name": index[lid],
                "detail": "选修课（照常抓取，只发给个人小群，不进班级群提醒）",
                "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "confirmed_by": "human",
            }
            added.append(lid)
        save_electives(data)
        if added:
            print(f"已标记为选修课 {len(added)} 门（照常抓取；班级群那条会删掉它）：")
            for lid in added:
                print(f"  [{lid}] {index[lid]}")
        else:
            print("没有新增（可能已是选修课）。")
        if unknown:
            print(f"未找到匹配课程：{', '.join(unknown)}", file=sys.stderr)
            print("提示：可以给 lid，也可以给课程名的一部分，例如 --blacklist-elective 嵌入式",
                  file=sys.stderr)
        return 0

    if getattr(args, "elective_list", False):
        elec = load_electives().get("courses", {})
        print(f"【选修课】{len(elec)} 门（照常抓取，只发给个人小群）：")
        for lid, v in elec.items():
            print(f"  [{lid}] {v.get('name', index.get(lid, '?')):32s} 确认于 {v.get('confirmed_at', '?')}")
        if not elec:
            print("  （无）")
        return 0

    if getattr(args, "elective_remove", None):
        data = load_electives()
        if "all" in args.elective_remove:
            n = len(data["courses"])
            data["courses"] = {}
            save_electives(data)
            print(f"已清空选修课名单（{n} 门），这些课恢复进班级群提醒。")
            return 0
        removed = []
        for lid in args.elective_remove:
            lid = str(lid)
            if lid in data["courses"]:
                removed.append(f"[{lid}] {data['courses'][lid].get('name', '?')}")
                data["courses"].pop(lid)
        save_electives(data)
        print(f"已取消选修课标记 {len(removed)} 门（恢复进班级群提醒）："
              + ("；".join(removed) if removed else "（无匹配）"))
        return 0

    if args.blacklist_confirm:
        data = load_blacklist()
        targets = []
        if "all-candidates" in args.blacklist_confirm:
            # 只确认「建议拉黑」那类。绝不把「仅供参考·暂无作业」的课一起拉黑 ——
            # 那些可能只是本学期还没布置作业（有的课就期末布置一次），拉黑会漏课。
            targets = [c["lid"] for c in info["candidates"] if not c.get("advisory")]
        elif "all-including-advisory" in args.blacklist_confirm:
            # 明确要求时才连参考类一起确认
            targets = [c["lid"] for c in info["candidates"]]
        else:
            targets = [t for t in args.blacklist_confirm]
        added = []
        for lid in targets:
            if lid not in index:
                print(f"跳过 {lid}：课程索引里没有这个 lid", file=sys.stderr)
                continue
            if lid in data["courses"]:
                continue
            cand = next((c for c in info["candidates"] if c["lid"] == lid), None)
            data["courses"][lid] = {
                "name": index[lid],
                "reason": (cand or {}).get("reason", "manual"),
                "detail": (cand or {}).get("detail", "人工手动拉黑"),
                "last_homework": (cand or {}).get("last_homework"),
                "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "confirmed_by": "human",
            }
            added.append(lid)
        save_blacklist(data)
        print(f"已加入黑名单 {len(added)} 门：")
        for lid in added:
            print(f"  [{lid}] {index[lid]}")
        return 0

    if args.blacklist_remove:
        data = load_blacklist()
        if "all" in args.blacklist_remove:
            n = len(data["courses"])
            data["courses"] = {}
            save_blacklist(data)
            print(f"已清空黑名单（{n} 门）。")
            return 0
        removed = []
        for lid in args.blacklist_remove:
            if lid in data["courses"]:
                removed.append(f"[{lid}] {data['courses'][lid].get('name', '?')}")
                data["courses"].pop(lid)
        save_blacklist(data)
        print(f"已移出黑名单 {len(removed)} 门：" + ("；".join(removed) if removed else "（无匹配）"))
        return 0
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--days", type=int, default=0, help=">0 时只保留未来 N 天内截止的")
    ap.add_argument("--content", help="抓取某条作业正文（传 hwtid），输出 JSON")
    ap.add_argument("--with-content", action="store_true",
                    help="抓取列表的同时逐条抓作业正文（标题只是标题，正文才是内容，较慢）")
    ap.add_argument("--refresh-courses", action="store_true",
                    help="只刷新本地课程索引 courses.json（从我的课程总表取全部课程），不抓作业")
    ap.add_argument("--sweep", action="store_true",
                    help="全量扫描：逐门课直取 hwtask.jsp，兜住首页「待提交作业」视图漏掉的课（约 1-2 分钟）")
    ap.add_argument("--sweep-delay", type=float, default=None,
                    help="全量扫描时每门课之间的间隔秒数（默认读配置，0.3）")
    ap.add_argument("--deadline-only", action="store_true",
                    help="【推荐】按截止日期取作业，不看是否已提交：列出所有截止时间落在窗口内的作业"
                         "（本工具是给全班发待办的，已提交与否不影响该不该提醒）")
    ap.add_argument("--only-unsubmitted", action="store_true",
                    help="只保留未提交的作业（旧行为；与 --deadline-only 互斥）")
    ap.add_argument("--with-tests", dest="with_tests", action="store_const", const=True,
                    default=None, help="同时抓「在线测试」")
    ap.add_argument("--no-tests", dest="with_tests", action="store_const", const=False,
                    help="明确不抓在线测试（优先于截止日期模式的默认值）")
    ap.add_argument("--lids", nargs="+", metavar="LID",
                    help="只刷新指定课程（用于单课/失败课重试）")
    ap.add_argument("--content-after-scan", action="store_true",
                    help="列表写缓存后，用同一认证上下文补齐窗口内作业正文")
    ap.add_argument("--content-limit", type=int, default=None,
                    help="本次自动补齐正文的最大条数")
    ap.add_argument("--content-window-days", type=int, default=None,
                    help="自动补齐正文的未来天数窗口")
    ap.add_argument("--scan-id", default=None,
                    help="内部使用：绑定到发起本次扫描的任务 ID，保证进度不串任务")
    ap.add_argument("--blacklist-list", action="store_true",
                    help="列出课程黑名单与「建议拉黑」候选（只读，不改动）")
    ap.add_argument("--blacklist-confirm", nargs="+", metavar="LID",
                    help="人工确认把指定课程加入黑名单（可多个 lid）。"
                         "all-candidates = 只确认「建议拉黑」那类；"
                         "all-including-advisory = 连「仅供参考·暂无作业」的也一起确认")
    ap.add_argument("--blacklist-remove", nargs="+", metavar="LID",
                    help="把指定课程移出黑名单（可多个 lid，或用 all）")
    ap.add_argument("--blacklist-elective", nargs="+", metavar="LID_OR_NAME",
                    help="标记为「选修课」：课照常抓，但班级群那条提醒里删掉它、"
                         "个人小群那条保留它。可给 lid 或课程名的一部分（唯一匹配时生效），"
                         "例如 --blacklist-elective 嵌入式")
    ap.add_argument("--elective-list", action="store_true",
                    help="列出「选修课」名单（照抓、只发个人；只读）")
    ap.add_argument("--elective-remove", nargs="+", metavar="LID",
                    help="取消选修课标记（恢复进班级群提醒，可多个 lid 或用 all）")
    ap.add_argument("--test-blacklist-list", action="store_true",
                    help="列出「仅屏蔽在线测试」的课程（作业仍照常搜）")
    ap.add_argument("--test-blacklist-add", nargs="+", metavar="LID",
                    help="把指定课程加入「仅屏蔽在线测试」（作业继续搜）")
    ap.add_argument("--test-blacklist-remove", nargs="+", metavar="LID",
                    help="把指定课程移出「仅屏蔽在线测试」（可多个 lid，或用 all）")
    args = ap.parse_args()
    # Explicit no-tests wins even if a caller supplied both switches.
    if "--no-tests" in sys.argv:
        args.with_tests = False

    # --- 黑名单命令（纯本地，不需要浏览器）---
    if (args.blacklist_list or args.blacklist_confirm or args.blacklist_remove
            or args.blacklist_elective or args.elective_list or args.elective_remove
            or args.test_blacklist_list or args.test_blacklist_add
            or args.test_blacklist_remove):
        return _blacklist_cli(args)

    if args.content:
        if not need_browser():
            return 1
        try:
            with FetchLock(wait_sec=LOCK_WAIT_SEC):
                with sync_playwright() as p:
                    ctx = make_context(p, headless=True)
                    page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
                    page.goto(SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
                    if not wait_authenticated(page, ctx, timeout=12):
                        cache_store.invalidate_scope_scans("认证失败：正文 CLI 登录态失效")
                        print(json.dumps({"ok": False, "error": "未登录或会话已过期，请运行 login.py"}, ensure_ascii=False))
                        return 1
                    cached = next((x for x in cache_store.load_items(include_inactive=True)
                                   if str(x.get("hwtid")) == str(args.content)), None)
                    if not cached:
                        print(json.dumps({"ok": False,
                                          "error": "该 hwtid 不在本地课程索引中，无法确定所属课程上下文"},
                                         ensure_ascii=False))
                        return 1
                    res = get_hw_content(ctx, args.content, args.debug,
                                         lid=cached.get("lid"), course_name=cached.get("course"))
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                    return 0
        except LockTimeout as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1

    if not need_browser():
        return 1

    # 整个抓取过程持锁；超时直接失败，绝不无锁继续。
    try:
        with FetchLock(wait_sec=LOCK_WAIT_SEC):
            return _run_scan(args)
    except LockTimeout as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc), "homeworks": []},
                         ensure_ascii=False))
        return 1


def _run_scan(args):
    T_START = time.time()
    T = {"login": 0.0, "pool": 0.0, "homework": 0.0, "test": 0.0, "content": 0.0, "save": 0.0}
    run_scan_id = str(args.scan_id or uuid.uuid4().hex[:12])
    cache_store.migrate_identity_state(cache_store.account_scope())

    with sync_playwright() as p:
        ctx = make_context(p, headless=True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        _t = time.time()
        page.goto(SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
        dump(page, "home", args.debug)

        if not wait_authenticated(page, ctx, timeout=12):
            cache_store.invalidate_scope_scans("认证失败：列表扫描登录态失效")
            print("ERROR: 未登录或会话已过期，请运行: python login.py", file=sys.stderr)
            try:
                index_now = load_course_index()
                expected_now = [lid for lid in index_now if lid not in blacklisted_lids()]
                cache_store.begin_scan(
                    run_scan_id, mode=("deadline-only" if args.deadline_only else "pool"),
                    expected_lids=expected_now, requested_lids=expected_now,
                    tests_requested=resolve_want_tests(args.with_tests, args.deadline_only),
                    course_index_version=cache_store.version_hash(index_now),
                    scope=cache_store.account_scope())
                cache_store.update_scan(
                    run_scan_id, status="error", finished_at=time.time(),
                    error="authentication failed before list scan")
            except Exception as cache_exc:  # noqa: BLE001
                print(f"写入认证失败扫描记录失败：{cache_exc}", file=sys.stderr)
            print(json.dumps({"ok": False, "homeworks": [], "scan_id": run_scan_id},
                             ensure_ascii=False))
            return 1
        try:
            marker = resolve_platform_identity(page)
            if marker:
                cache_store.bind_identity(marker, scope=cache_store.account_scope())
            else:
                print("提示：未能从页面取得非凭据账号身份标记；默认范围将只允许离线查看，"
                      "正式发布保持阻断。", file=sys.stderr)
        except cache_store.AccountIdentityConflict as exc:
            cache_store.invalidate_scope_scans(str(exc))
            print(f"ERROR: {exc}", file=sys.stderr)
            print(json.dumps({"ok": False, "homeworks": [], "error": str(exc)},
                             ensure_ascii=False))
            return 1
        T["login"] = time.time() - _t
        print("已登录", file=sys.stderr)

        # --- 只刷新课程索引 ---
        if args.refresh_courses:
            try:
                index = collect_all_courses(page)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR: 拉取课程总表失败: {e}", file=sys.stderr)
                return 1
            COURSES_FILE.write_text(
                json.dumps({
                    "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": ALL_COURSES_URL,
                    "courses": index,
                    # 保留已有的每课元数据（失败次数 / 最近作业时间），别刷新课程表就把它清掉
                    "meta": load_index_meta(),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"课程索引已更新：{len(index)} 门 -> {COURSES_FILE}", file=sys.stderr)
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            print(json.dumps({"ok": True, "count": len(index)}, ensure_ascii=False))
            return 0

        courses = parse_courses(page)
        pool_names = {c["course"] for c in courses}
        pool_lids = {c["lid"] for c in courses}
        print(f"首页待提交作业课程: {len(courses)} 门", file=sys.stderr)

        # 【核心】按截止日期取作业时，必须全量扫描：
        # 首页池只列「我还没交」的课，谁交了哪门课，全班同学就再也收不到那门课的提醒。
        # 这个工具是给全班发待办的，所以候选池必须来自「全部课程」，而不是「我的待提交」。
        use_sweep = args.sweep or args.deadline_only
        if args.deadline_only and not args.sweep:
            print("模式：按截止日期取作业（不看是否已提交）-> 自动启用全量课程扫描", file=sys.stderr)
        # 三态：未指定时由模式决定；显式 --no-tests / --with-tests 优先。
        do_tests = resolve_want_tests(args.with_tests, args.deadline_only)

        sweep_meta = None
        tests = []
        test_meta = None
        scan_id = None
        scan_status = "not-started"
        scope = cache_store.account_scope()
        if use_sweep:
            scan_id = run_scan_id
            bl = blacklisted_lids()
            bl_names = {v.get("name") for v in load_blacklist().get("courses", {}).values()}
            tbl = test_blacklisted_lids() if do_tests else set()
            # 选修课**不跳过**：课要照抓（你可能要交），只是不进班级群那条提醒。
            elec = elective_lids()
            index_now = load_course_index()
            full_expected = [lid for lid in index_now if lid not in bl]
            selected = [str(x) for x in (args.lids or []) if str(x) in index_now
                        and str(x) not in bl]
            if args.lids and not selected:
                print(json.dumps({"ok": False, "homeworks": [],
                                  "error": "指定的课程不在当前课程索引中，或已被整门拉黑"},
                                 ensure_ascii=False))
                return 1
            expected_lids = selected or full_expected
            cache_store.begin_scan(
                scan_id, mode="deadline-only" if args.deadline_only else "pool",
                expected_lids=expected_lids, requested_lids=expected_lids,
                tests_requested=do_tests,
                course_index_version=cache_store.version_hash(index_now),
                scope=scope)
            print(f"全量扫描开始（课程索引 {len(load_course_index())} 门，"
                  f"黑名单跳过 {len(bl)} 门"
                  + (f"，选修课 {len(elec)} 门照抓（只发个人）" if elec else "")
                  + (f"，仅屏蔽在线测试 {len(tbl)} 门" if tbl else "") + "）...", file=sys.stderr)
            _t = time.time()
            # 一次进课程页把「课程作业 + 在线测试」都取下来（比两轮扫描省近一半时间）
            try:
                homeworks, tests, per_course, sweep_errors, skipped, test_notes = sweep_all_courses(
                    ctx, page, args.debug, args.sweep_delay,
                    skip_lids=bl, want_tests=do_tests, test_skip_lids=tbl,
                    scan_id=scan_id, scope=scope,
                    index_version=cache_store.version_hash(index_now),
                    only_lids=selected or None)
            except Exception as exc:
                cache_store.update_scan(scan_id, status="error", finished_at=time.time(),
                                        error=f"{type(exc).__name__}: {exc}")
                raise
            T["homework"] = time.time() - _t
            if args.only_unsubmitted:
                homeworks = [h for h in homeworks if h.get("submit_url")]
            print(f"  扫描用时 {T['homework']:.0f} 秒"
                  f"（{len(per_course)} 门课，每门课一次进页面同时取作业+测试）", file=sys.stderr)

            if do_tests:
                t_ok = [p for p in per_course if p.get("tests")]
                # 「没有测试栏目」= 本轮没找到的 + 之前已判定并自动跳过的（不然会自相矛盾：
                # 一旦进了自动跳过名单，它就不再产生 no-test-link，于是统计里反而消失）
                meta_now = load_index_meta()
                auto_skipped = [m.get("course", "?") for m in meta_now.values()
                                if no_test_skip_is_fresh(m)]
                no_entry = sorted(set(test_notes.get("no_entry", [])) | set(auto_skipped))
                test_meta = {
                    "courses_with_tests": len(t_ok),
                    "no_test_entry": no_entry,
                    "auto_skipped_no_test": sorted(set(test_notes.get("auto_skip", []))),
                    "skipped_by_test_blacklist": test_notes.get("manual_skipped", []),
                    "errors": test_notes.get("errors", []),
                    "total": len(tests),
                }
                print(f"在线测试抓取完成：{len(tests)} 条，"
                      f"{len(t_ok)} 门课有测试，"
                      f"{len(no_entry)} 门课没有「在线测试」栏目"
                      + (f"，你指定的屏蔽名单 {len(test_meta['skipped_by_test_blacklist'])} 门"
                         if test_meta['skipped_by_test_blacklist'] else "")
                      + (f"，本轮新判定 {len(test_meta['auto_skipped_no_test'])} 门"
                         if test_meta['auto_skipped_no_test'] else ""), file=sys.stderr)
            scan_ok = (not sweep_errors and not test_notes.get("errors")
                       and not selected and len(per_course) == len(full_expected))
            scan_status = "complete" if scan_ok else "partial"
            cache_store.update_scan(
                scan_id, status=scan_status, finished_at=time.time(),
                coverage_json={
                    "courses_total": len(per_course),
                    "expected_total": len(full_expected),
                    "subset_refresh": bool(selected),
                    "failed_courses": sweep_errors,
                    "test_errors": test_notes.get("errors", []),
                })
            # 首页没列、但该课确实有作业 -> 说明首页视图漏了这门课（黑名单命中名字也算漏）
            missed = [p for p in per_course
                      if p["total"] > 0
                      and (p["unsubmitted"] > 0 or args.deadline_only)
                      and p["lid"] not in pool_lids
                      and p["course"] not in pool_names
                      and p["course"] not in bl_names]
            # 更新每课元数据，并生成「建议拉黑」候选（只建议，需人工确认）
            meta_info = update_index_meta(per_course)
            cand = meta_info["candidates"]
            sweep_meta = {
                "courses_total": len(per_course),
                "courses_with_homework": sum(1 for p in per_course if p["total"] > 0),
                "courses_unsubmitted": sum(1 for p in per_course if p["unsubmitted"] > 0),
                "missed_from_pool": missed,
                "errors": sweep_errors,
                "skipped_blacklist": skipped,
                "semester_start": meta_info["semester_start"],
                "blacklist_candidates": cand,
            }
            print(f"全量扫描完成：{len(per_course)} 门课，"
                  f"{sweep_meta['courses_with_homework']} 门有作业、"
                  f"{sweep_meta['courses_unsubmitted']} 门存在未提交"
                  f"（黑名单跳过 {len(skipped)} 门）", file=sys.stderr)
            if missed:
                print(f"⚠️ 首页视图漏掉 {len(missed)} 门有作业的课（这些课的同学可能收不到提醒）：",
                      file=sys.stderr)
                for m in missed:
                    print(f"    · {m['course']}（{m['total']} 条作业，未提交 {m['unsubmitted']} 条）",
                          file=sys.stderr)
            if sweep_errors:
                print(f"⚠️ {len(sweep_errors)} 门课抓取失败（覆盖不完整，会写进 warnings）："
                      f"{'; '.join(sweep_errors[:5])}", file=sys.stderr)
            if cand:
                print(f"📋 有 {len(cand)} 门课建议加入黑名单（本学期 {meta_info['semester_start']} 起算），"
                      f"需要你确认才会生效：", file=sys.stderr)
                for c in cand:
                    print(f"    · {c['course']}（{c['reason']}）{c['detail']}", file=sys.stderr)
                print("    确认命令：python scrape.py --blacklist-confirm <lid> [<lid>...]", file=sys.stderr)
        else:
            homeworks = []
            per_course = []
            for c in courses:
                try:
                    rows = fetch_course(ctx, page, c["lid"], args.debug)
                    for r in rows:
                        r["course"] = c["course"]
                        r["lid"] = c["lid"]
                    homeworks.extend(rows)
                    per_course.append({
                        "lid": c["lid"], "course": c["course"], "total": len(rows),
                        "unsubmitted": sum(1 for r in rows if r.get("submit_url")), "error": None,
                    })
                    print(f"  {c['course']}: {len(rows)} 条", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    per_course.append({
                        "lid": c["lid"], "course": c["course"], "total": 0,
                        "unsubmitted": 0, "error": str(e),
                    })
                    print(f"  {c['course']} 抓取失败: {e}", file=sys.stderr)

        # --- 统一按截止时间过滤（作业 + 在线测试同一套规则）---
        def _parse_dl(s):
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s or "", fmt)
                except ValueError:
                    continue
            return None

        for item in homeworks:
            item.setdefault("kind", "homework")

        if args.days > 0:
            now = datetime.now()
            end = now + timedelta(days=args.days)
            bad = []
            tests_total = len(tests)

            def _drop_summary(items, label):
                """窗口外的条目按原因归类，让输出能看出「不是没抓到，是窗口外」。"""
                expired = [it for it in items
                           if (_parse_dl(it.get("deadline", "")) or now) < now]
                beyond = [it for it in items
                          if (_parse_dl(it.get("deadline", "")) or now) > end]
                if expired or beyond:
                    print(f"  （{label}：窗口外滤掉 已过期 {len(expired)} 条 / "
                          f"晚于 {end:%m-%d} 的 {len(beyond)} 条）", file=sys.stderr)

            def _filter(items, label):
                kept = []
                for it in items:
                    dt = _parse_dl(it.get("deadline", ""))
                    if dt is None:
                        bad.append(f"{it.get('course', '?')}/{it.get('title', '?')}"
                                   f" 截止时间无法解析: {it.get('deadline', '')!r}")
                        continue
                    if now <= dt <= end:
                        kept.append(it)
                _drop_summary(items, label)
                return kept

            all_tests_raw = list(tests)      # 过滤前的副本，用于「测试都被窗口滤掉了」的提示
            homeworks = _filter(homeworks, "作业")
            tests = _filter(tests, "在线测试")
            if tests_total and not tests:
                earliest = min([(t.get("deadline") or "") for t in all_tests_raw if t.get("deadline")],
                               default="")
                print(f"  ⓘ 在线测试抓到 {tests_total} 条，但截止时间都不在未来 {args.days} 天内"
                      f"（最早一条 {earliest}）—— 想看全部测试请加大 --days", file=sys.stderr)
            if bad:
                print(f"⚠️ {len(bad)} 条截止时间无法解析，已跳过（见 warnings）：{bad[:3]}",
                      file=sys.stderr)

        # 正文阶段：列表已按课程写入缓存；这里复用同一认证上下文按窗口补齐。
        content_stats = None
        if args.with_content or args.content_after_scan:
            _t = time.time()
            if args.with_content:
                cached_by_hwtid = {
                    str(x.get("hwtid")): x for x in cache_store.load_items(scope)
                    if x.get("kind") == "homework" and x.get("hwtid")}
                ok = failed_n = 0
                for hw in homeworks:
                    item = cached_by_hwtid.get(str(hw.get("hwtid") or ""))
                    if not item:
                        continue
                    result = fetch_content_for_item(ctx, item, debug=args.debug)
                    if result.get("ok"):
                        ok += 1
                    else:
                        failed_n += 1
                    hw["content_ok"] = bool(result.get("ok"))
                    hw["content_error"] = result.get("error")
                content_stats = {"requested": ok + failed_n, "ok": ok, "failed": failed_n}
            else:
                content_stats = populate_content_after_scan(
                    ctx, limit=args.content_limit,
                    window_days=args.content_window_days, debug=args.debug)
            T["content"] = time.time() - _t

        # 标记每条（作业 / 在线测试）的完成状态（仅供查看/排版，不参与筛选）：
        #   submit_url/start_url 有 -> 还能做（未做，或已做但允许重做）
        #   result_url 有           -> 平台上存在我的记录
        for item in list(homeworks) + list(tests):
            item.setdefault("kind", "homework")
            if item.get("submit_url"):
                item["submitted"] = bool(item.get("result_url"))
            elif item.get("result_url"):
                item["submitted"] = True
            else:
                item["submitted"] = None      # 未知：截止后入口会消失
            item["submit_state"] = (
                "unsubmitted" if item["submitted"] is False
                else "submitted" if item["submitted"] is True
                else "unknown"
            )

        # 可疑覆盖检测：有课程抓取失败、或返回 0 条，都可能造成漏报，显式告警
        zero = [p["course"] for p in per_course if p["total"] == 0 and not p["error"]]
        failed = [p["course"] for p in per_course if p["error"]]
        if zero:
            print(f"⚠️ {len(zero)} 门课返回 0 条作业（可能是课程确实没作业，也可能是平台未同步）："
                  f"{', '.join(zero[:6])}{'...' if len(zero) > 6 else ''}", file=sys.stderr)
        if failed:
            print(f"⚠️ {len(failed)} 门课抓取失败（这些课的作业可能漏报）：{', '.join(failed[:6])}", file=sys.stderr)

        if args.deadline_only:
            def _count(items, st):
                return sum(1 for h in items if h.get("submit_state") == st)
            print(f"按截止日期取数：作业 {len(homeworks)} 条（我未交 {_count(homeworks,'unsubmitted')} / "
                  f"我已交 {_count(homeworks,'submitted')}）"
                  f" + 在线测试 {len(tests)} 条（未做 {_count(tests,'unsubmitted')}）"
                  f" —— 全都保留，因为提醒是发给全班的", file=sys.stderr)

        # ---- 计时汇总 ----
        _t = time.time()
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        T["save"] = time.time() - _t
        TOTAL = time.time() - T_START

        def _m(sec):
            return f"{sec / 60:.1f} 分" if sec >= 60 else f"{sec:.0f} 秒"

        T["total"] = round(TOTAL, 1)
        scan_n = len(per_course)
        print("", file=sys.stderr)
        print("──────────── 本次抓取用时 ────────────", file=sys.stderr)
        print(f"  登录/建会话      {_m(T['login'])}", file=sys.stderr)
        if T["homework"]:
            print(f"  扫描课程         {_m(T['homework'])}"
                  f"（{scan_n} 门课，每门课一次进页面同时取作业+测试；"
                  f"黑名单跳过 {len(skipped)} 门）", file=sys.stderr)
        if T["content"]:
            print(f"  抓作业正文       {_m(T['content'])}", file=sys.stderr)
        print(f"  ──────────────────────────", file=sys.stderr)
        print(f"  合计             {_m(TOTAL)}", file=sys.stderr)
        if scan_n:
            print(f"  平均每门课       {T['homework'] / scan_n:.1f} 秒", file=sys.stderr)
        print("─────────────────────────────────────", file=sys.stderr)

        out = {
            "ok": True,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "deadline-only" if args.deadline_only else "pool",
            "elapsed_sec": round(TOTAL, 1),
            "elapsed_human": _m(TOTAL),
            "timing": T,
            "courses": courses,
            "homeworks": homeworks,
            "tests": tests,
            # 选修课 lid：让上层（weekly/hub）能把「班级群那条」里的这些课删掉，
            # 同时保留在「个人小群那条」里。抓取本身不会被它们影响。
            "electives": sorted(elective_lids()),
            "tests_requested": bool(do_tests),
            "per_course": [{k: v for k, v in p.items() if not k.startswith("_")} for p in per_course],
            "warnings": {
                "zero_homework_courses": zero,
                "failed_courses": failed,
                "unparsable_deadlines": bad if args.days > 0 else [],
            },
        }
        if sweep_meta:
            out["sweep"] = sweep_meta
        if test_meta:
            out["test_meta"] = test_meta
        if scan_id:
            out["scan_id"] = scan_id
            out["scan_status"] = scan_status
            out["cache_progress"] = cache_store.scan_progress(scan_id, scope)
        if content_stats:
            out["content_stats"] = content_stats
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
