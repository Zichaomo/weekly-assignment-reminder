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
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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

BASE = "https://course.buct.edu.cn"
SSO_LOGIN = BASE + "/meol/homepage/common/sso_login.jsp"
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path.home() / ".workbuddy" / "profiles" / "buct-course-edge"
STATE_FILE = ROOT / "storage_state.json"
DEBUG_DIR = ROOT / "debug"

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


def fetch_course_both(ctx, page, lid: str, name: str, debug: bool = False,
                      want_homework: bool = True, want_tests: bool = False,
                      empty_retries: int = 2):
    """一次进课程页，把「课程作业」和「在线测试」两个栏目都取下来。

    提速要点（2026-10-13 实测，每门课 ~2 秒）：
      1. **复用同一个 page 直接 goto enter_course.jsp**，不再为每门课开新窗口
         （旧写法靠 `ctx.expect_page()` 抓 window.open 出来的新窗口，
          既慢又依赖主页面一直停在 SSO 首页，主页面一旦被导航就整轮失效）。
      2. **进一次课程页就把两个栏目链接都取下来**，不要跳完作业页再跑回来取测试链接
         （那次多余往返实测约 0.7 秒/门）。
      3. **找不到「在线测试」栏目时快速放弃**：这类课（实测 高等数学A（I））
         会让「等文字出现」白等满超时 —— 曾经一门课就吃掉 42 秒。

    ⚠️ 空表必须重试（2026-10-13 教训）：
      平台偶发「作业页已跳过去但表格没渲染 / 跳错页」，此时解析出 0 条。
      而 0 条本身是合法结果（有的课确实没作业），若不重试就会**静默丢数据**。
      实测普通物理(Ⅱ) 出现过一次 0 条 + 15.9 秒（白等超时）。
      所以：看起来是「加载失败」时重试 empty_retries 次；重试后仍 0 条才接受，
      并在 notes 里标记 empty-verified 供上层告警。

    返回 (homeworks, tests, notes)。某一栏目不存在不算失败，只记 note。
    """
    hw_rows, test_rows = [], []
    notes = []
    course_url = f"{BASE}/meol/lesson/enter_course.jsp?lid={lid}&t=hw"
    last_error = None

    for attempt in range(max(1, empty_retries + 1)):
        try:
            # 进课程页（这一跳就把服务端会话的「当前课程」切过来了），同时取两个栏目链接
            page.goto(course_url, wait_until="domcontentloaded", timeout=60000)
            if not wait_text(page, "课程作业", timeout=25):
                last_error = "课程页未出现「课程作业」入口"
                continue
            hw_href = get_column_href(page, "课程作业", timeout=15) if want_homework else None
            test_href = (get_column_href(page, "在线测试", timeout=4, retries=2)
                         if want_tests else None)

            if want_homework and not hw_href:
                last_error = "取不到「课程作业」链接"
                continue
            if want_tests and not test_href:
                notes = [n for n in notes if n != "no-test-link"] + ["no-test-link"]

            got_hw = None
            if hw_href:
                page.goto(hw_href, wait_until="domcontentloaded", timeout=60000)
                wait_text(page, "截止时间", timeout=15)
                time.sleep(0.3)
                dump(page, f"sweep_{lid}", debug)
                got_hw = parse_hw_table(page)

            got_tests = []
            if test_href:
                page.goto(test_href, wait_until="domcontentloaded", timeout=60000)
                wait_text(page, "截止时间", timeout=15)
                time.sleep(0.3)
                dump(page, f"sweeptest_{lid}", debug)
                got_tests = parse_test_table(page)

            # 空表可疑 -> 重试（这是本函数最关键的一处保护）
            if got_hw is not None and not got_hw and attempt < empty_retries:
                last_error = "作业表为空（疑似页面未加载），重试"
                print(f"    {name}: 作业表为空，疑似加载失败，重试 {attempt + 1}/{empty_retries}",
                      file=sys.stderr)
                continue

            if got_hw is not None and not got_hw:
                notes.append("empty-verified")     # 重试后仍空 -> 大概率真没作业
            if got_hw is not None:
                hw_rows = got_hw
            test_rows = got_tests
            return hw_rows, test_rows, notes
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"
            continue

    if last_error:
        raise RuntimeError(last_error)
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
    lids = set(load_test_blacklist().get("courses", {}).keys())
    for lid, m in load_index_meta().items():
        if int(m.get("no_test_streak", 0)) >= 2:
            lids.add(lid)
    return lids


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


def load_index_meta() -> dict:
    """读 courses.json 里的每课元数据（失败次数 / 最近作业时间）。"""
    if not COURSES_FILE.exists():
        return {}
    try:
        return json.loads(COURSES_FILE.read_text(encoding="utf-8")).get("meta") or {}
    except Exception:  # noqa: BLE001
        return {}


def save_index_meta(meta: dict) -> None:
    """把每课元数据并回 courses.json（保留 courses 不动）。"""
    try:
        d = json.loads(COURSES_FILE.read_text(encoding="utf-8")) if COURSES_FILE.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
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
        m["last_scan"] = now.strftime("%Y-%m-%d %H:%M")
        m["last_error"] = pc.get("error")
        meta[lid] = m
    # 本次没扫到的课保留原元数据（别丢历史）
    sem_start = _semester_start(now)
    bl = blacklisted_lids()
    candidates = []
    for lid, m in meta.items():
        if lid in bl:
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
        if stale or never:
            candidates.append({
                "lid": lid,
                "course": m.get("course", "?"),
                "reason": "stale" if stale else "no-homework-entry",
                "detail": (f"最近一次作业 {lh}（早于本学期 {sem_start:%Y-%m-%d}）"
                           if stale else
                           f"连续 {m.get('fail_streak')} 次扫描都没能进入「课程作业」"),
                "last_homework": lh,
                "fail_streak": m.get("fail_streak", 0),
                "scanned": lid in seen_lids,
            })
    save_index_meta(meta)
    return {"meta": meta, "candidates": candidates, "semester_start": sem_start.strftime("%Y-%m-%d")}


def sweep_all_courses(ctx, page, debug=False, delay=1.0, skip_lids=None,
                      want_tests=False, test_skip_lids=None) -> tuple:
    """逐门课抓「课程作业」（可选「在线测试」），一次进课程页把两个栏目都取下来。

    skip_lids:      整体拉黑的课，直接跳过（不算失败、不重试）
    test_skip_lids: 只屏蔽在线测试的课（作业照抓）
    want_tests:     是否同时抓在线测试
    返回 (homeworks, tests, per_course, errors, skipped, test_notes)
    """
    index = load_course_index()
    if not index:
        return ([], [], [],
                ["课程索引为空：请先跑 scrape.py --refresh-courses 或用控制台「刷新课程表」"],
                [], {})

    skip_lids = set(skip_lids or ())
    test_skip_lids = set(test_skip_lids or ())
    auto_test_skip = set()      # 连续找不到测试栏目 -> 下次直接跳过（见 update_index_meta）
    skipped = [{"lid": k, "course": index.get(k, "?")} for k in skip_lids if k in index]
    homeworks, tests, per_course, errors = [], [], [], []
    # 手工名单与「自动判定无测试栏目」分开记，避免报告里混成一类
    manual_test_skip = set(load_test_blacklist().get("courses", {}).keys())
    test_notes = {"skipped": [], "manual_skipped": [], "no_entry": []}
    prev_ids = None      # 兜底：连续两门课内容完全相同 => 疑似没切课
    for lid, name in index.items():
        if lid in skip_lids:
            continue
        want_t = bool(want_tests) and lid not in test_skip_lids
        if want_tests and lid in test_skip_lids:
            test_notes["skipped"].append(name)
            if lid in manual_test_skip:
                test_notes["manual_skipped"].append(name)
        try:
            rows, trows, notes = fetch_course_both(
                ctx, page, lid, name, debug, want_homework=True, want_tests=want_t)
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
            homeworks.extend(rows)
            tests.extend(trows)
            if "no-test-link" in notes or "test-entry-missing" in notes:
                test_notes["no_entry"].append(name)
            per_course.append({
                "lid": lid, "course": name, "total": len(rows),
                "unsubmitted": len(unsub), "error": None,
                "tests": len(trows),
                "notes": notes,
                "_deadlines": [r.get("deadline") for r in rows if r.get("deadline")],
            })
            if unsub or trows:
                extra = f"，测试 {len(trows)} 条" if want_t and trows else ""
                print(f"  {name}: 作业 {len(rows)} 条（未交 {len(unsub)}）{extra}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            per_course.append({
                "lid": lid, "course": name, "total": 0,
                "unsubmitted": 0, "error": f"{type(e).__name__}: {e}",
                "_deadlines": [],
            })
            errors.append(f"{name}({lid}): {type(e).__name__}: {e}")
            print(f"  {name}: 抓取失败 {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(delay)
    # 记住「没有测试栏目」的课，下次不再为它白等一遍
    if auto_test_skip:
        meta = load_index_meta()
        for lid in auto_test_skip:
            m = meta.get(lid) or {}
            m["no_test_streak"] = int(m.get("no_test_streak", 0)) + 1
            if m["no_test_streak"] >= 2:
                m["no_test_reason"] = "连续找不到「在线测试」栏目，已自动跳过（不影响作业）"
            meta[lid] = m
        save_index_meta(meta)
        test_notes["auto_skip"] = [index.get(k, "?") for k in auto_test_skip]
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


def get_column_href(child, label: str, timeout: int = 30, retries: int = None):
    """通用：取课程页左侧菜单里某个栏目（如「在线测试」）的 href。

    用 textContent 匹配（隐藏元素的 innerText 为空）。
    timeout 是「最多尝试几次」，每次间隔 1 秒。
    retries 是「没找到时最多再试几次」的覆盖值：给个很小的值能让
    「这门课没有该栏目」快速返回，不必白等满超时
    （实测 高等数学A（I） 没有「在线测试」栏目，曾经一门课白等 40 秒）。
    """
    tries = timeout if retries is None else retries
    for _ in range(max(1, tries)):
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
        time.sleep(1)
    return None


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


def wait_text(page, keyword: str, timeout: int = 30) -> bool:
    for _ in range(timeout):
        try:
            if keyword in (page.inner_text("body", timeout=1500) or ""):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def get_hw_href(child, timeout: int = 30):
    """轮询取「课程作业」的绝对 URL（子菜单默认隐藏，textContent 才可靠）。"""
    for _ in range(timeout):
        href = child.evaluate("""() => {
            const a = [...document.querySelectorAll('a')].find(x =>
                (x.textContent||'').replace(/\\s+/g,'') === '课程作业');
            return a ? a.href : null;
        }""")
        if href:
            return href
        time.sleep(1)
    return None


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
    """把作业正文的 HTML 转成可读多行文本。"""
    s = html.unescape(s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|li|div|tr|h[1-6])>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\u00a0", " ")
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def get_hw_content(ctx, hwtid: str, debug: bool = False) -> dict:
    """抓取某条作业的详情正文。

    平台把正文放在隐藏域 `<input type="hidden" name="N_content" value="实体编码的HTML">`，
    与 ueditor iframe 同步，无需进 iframe 即可拿到，解码后即为作业题目/要求。
    """
    url = BASE + f"/meol/common/hw/student/hwtask.view.jsp?hwtid={hwtid}"
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(1.5)
        dump(page, f"content_{hwtid}", debug)
        src = page.content()
        m = re.search(r'name="\d+_content"[^>]*value="([^"]*)"', src)
        if not m:
            return {"ok": False, "hwtid": hwtid,
                    "error": "未找到作业正文（页面结构可能变化，或会话已失效需重新登录）", "url": url}
        inner = html.unescape(m.group(1))
        return {
            "ok": True, "hwtid": hwtid, "url": url,
            "content_html": inner, "content_text": html_to_text(inner),
        }
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def build_blacklist_candidates(now=None) -> dict:
    """只读地算出当前的建议拉黑候选（不改任何文件）。

    规则（两选一即候选）：
      stale            —— 该课最近一次作业截止时间早于本学期起点（8/1 或 2/1）
      no-homework-entry—— 连续 >=2 次扫描都没能进入「课程作业」，且从未见过作业
    """
    now = now or datetime.now()
    sem_start = _semester_start(now)
    meta = load_index_meta()
    bl = load_blacklist().get("courses", {})
    index = load_course_index()
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
        entry = {
            "lid": lid,
            "course": name,
            "blacklisted": lid in bl,
            "last_homework": lh,
            "fail_streak": int(m.get("fail_streak", 0)),
            "last_error": m.get("last_error"),
        }
        if lid in bl:
            continue
        if lh_dt is not None and lh_dt < sem_start:
            entry["reason"] = "stale"
            entry["detail"] = f"最近一次作业 {lh}，早于本学期 {sem_start:%Y-%m-%d}"
            candidates.append(entry)
        elif lh_dt is None and entry["fail_streak"] >= STALE_FAIL_STREAK:
            entry["reason"] = "no-homework-entry"
            entry["detail"] = f"连续 {entry['fail_streak']} 次扫描都没进入「课程作业」"
            candidates.append(entry)
        else:
            kept.append(entry)
    return {
        "semester_start": sem_start.strftime("%Y-%m-%d"),
        "candidates": candidates,
        "blacklist": bl,
        "total_courses": len(index),
    }


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
        print(f"学期起点 = {info['semester_start']} | 课程总数 = {info['total_courses']} "
              f"| 已拉黑 = {len(bl)} | 待确认候选 = {len(info['candidates'])}")
        print()
        print("【建议拉黑候选】确认后才会生效：")
        for c in info["candidates"]:
            tag = "陈旧" if c["reason"] == "stale" else "无作业入口"
            print(f"  [{c['lid']}] {c['course']:32s} {tag:8s} {c['detail']}")
        print()
        print("【已拉黑】扫描时跳过：")
        for lid, v in bl.items():
            print(f"  [{lid}] {v.get('name', index.get(lid, '?')):32s} {v.get('reason', '')}"
                  f"  确认于 {v.get('confirmed_at', '?')}")
        print()
        print("确认：python scrape.py --blacklist-confirm <lid> ...  （或 all-candidates）")
        print("移除：python scrape.py --blacklist-remove <lid> ...  （或 all）")
        return 0

    if args.blacklist_confirm:
        data = load_blacklist()
        targets = []
        if "all-candidates" in args.blacklist_confirm:
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
    ap.add_argument("--sweep-delay", type=float, default=1.0, help="全量扫描时每门课之间的间隔秒数")
    ap.add_argument("--deadline-only", action="store_true",
                    help="【推荐】按截止日期取作业，不看是否已提交：列出所有截止时间落在窗口内的作业"
                         "（本工具是给全班发待办的，已提交与否不影响该不该提醒）")
    ap.add_argument("--only-unsubmitted", action="store_true",
                    help="只保留未提交的作业（旧行为；与 --deadline-only 互斥）")
    ap.add_argument("--with-tests", dest="with_tests", action="store_true", default=False,
                    help="同时抓「在线测试」（截止规则与作业一致）")
    ap.add_argument("--no-tests", dest="with_tests", action="store_false",
                    help="不抓在线测试（默认在 --deadline-only 模式下会抓）")
    ap.add_argument("--blacklist-list", action="store_true",
                    help="列出课程黑名单与「建议拉黑」候选（只读，不改动）")
    ap.add_argument("--blacklist-confirm", nargs="+", metavar="LID",
                    help="人工确认把指定课程加入黑名单（可多个 lid，或用 all-candidates）")
    ap.add_argument("--blacklist-remove", nargs="+", metavar="LID",
                    help="把指定课程移出黑名单（可多个 lid，或用 all）")
    ap.add_argument("--test-blacklist-list", action="store_true",
                    help="列出「仅屏蔽在线测试」的课程（作业仍照常搜）")
    ap.add_argument("--test-blacklist-add", nargs="+", metavar="LID",
                    help="把指定课程加入「仅屏蔽在线测试」（作业继续搜）")
    ap.add_argument("--test-blacklist-remove", nargs="+", metavar="LID",
                    help="把指定课程移出「仅屏蔽在线测试」（可多个 lid，或用 all）")
    args = ap.parse_args()

    # --- 黑名单命令（纯本地，不需要浏览器）---
    if (args.blacklist_list or args.blacklist_confirm or args.blacklist_remove
            or args.test_blacklist_list or args.test_blacklist_add
            or args.test_blacklist_remove):
        return _blacklist_cli(args)

    if args.content:
        if not need_browser():
            return 1
        with sync_playwright() as p:
            ctx = make_context(p, headless=True)
            page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
            page.goto(SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            if not logged_in(page, ctx):
                print(json.dumps({"ok": False, "error": "未登录或会话已过期，请运行 login.py"}, ensure_ascii=False))
                return 1
            res = get_hw_content(ctx, args.content, args.debug)
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

    if not need_browser():
        return 1

    T_START = time.time()
    T = {"login": 0.0, "pool": 0.0, "homework": 0.0, "test": 0.0, "content": 0.0, "save": 0.0}

    with sync_playwright() as p:
        ctx = make_context(p, headless=True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        _t = time.time()
        page.goto(SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        dump(page, "home", args.debug)

        if not logged_in(page, ctx):
            print("ERROR: 未登录或会话已过期，请运行: python login.py", file=sys.stderr)
            print(json.dumps({"ok": False, "homeworks": []}, ensure_ascii=False))
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
        # 在线测试与作业同规则：--deadline-only 模式下默认一起抓
        do_tests = args.with_tests or args.deadline_only

        sweep_meta = None
        tests = []
        test_meta = None
        if use_sweep:
            bl = blacklisted_lids()
            bl_names = {v.get("name") for v in load_blacklist().get("courses", {}).values()}
            tbl = test_blacklisted_lids() if do_tests else set()
            print(f"全量扫描开始（课程索引 {len(load_course_index())} 门，"
                  f"黑名单跳过 {len(bl)} 门"
                  + (f"，仅屏蔽在线测试 {len(tbl)} 门" if tbl else "") + "）...", file=sys.stderr)
            _t = time.time()
            # 一次进课程页把「课程作业 + 在线测试」都取下来（比两轮扫描省近一半时间）
            homeworks, tests, per_course, sweep_errors, skipped, test_notes = sweep_all_courses(
                ctx, page, args.debug, args.sweep_delay,
                skip_lids=bl, want_tests=do_tests, test_skip_lids=tbl)
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
                                if int(m.get("no_test_streak", 0)) >= 2]
                no_entry = sorted(set(test_notes.get("no_entry", [])) | set(auto_skipped))
                test_meta = {
                    "courses_with_tests": len(t_ok),
                    "no_test_entry": no_entry,
                    "auto_skipped_no_test": sorted(set(test_notes.get("auto_skip", []))),
                    "skipped_by_test_blacklist": test_notes.get("manual_skipped", []),
                    "errors": [],
                    "total": len(tests),
                }
                print(f"在线测试抓取完成：{len(tests)} 条，"
                      f"{len(t_ok)} 门课有测试，"
                      f"{len(no_entry)} 门课没有「在线测试」栏目"
                      + (f"，你指定的屏蔽名单 {len(test_meta['skipped_by_test_blacklist'])} 门"
                         if test_meta['skipped_by_test_blacklist'] else "")
                      + (f"，本轮新判定 {len(test_meta['auto_skipped_no_test'])} 门"
                         if test_meta['auto_skipped_no_test'] else ""), file=sys.stderr)
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

        # 逐条抓正文：列表里的 title 只是标题，真正的内容（题目/要求）在 hwtask.view.jsp 里
        if args.with_content:
            for hw in homeworks:
                hwtid = hw.get("hwtid")
                if not hwtid:
                    continue
                try:
                    r = get_hw_content(ctx, hwtid, args.debug)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e)}
                hw["content"] = r.get("content_text", "") if r.get("ok") else ""
                hw["content_ok"] = bool(r.get("ok"))
                if not r.get("ok"):
                    hw["content_error"] = r.get("error", "")
                print(f"  正文 hwtid={hwtid}: {'OK' if r.get('ok') else '失败'}", file=sys.stderr)

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
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
