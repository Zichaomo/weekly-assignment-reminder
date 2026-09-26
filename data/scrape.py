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
from datetime import datetime
from pathlib import Path

# Windows 下子进程 stdout 默认按 GBK(cp936) 编码，父进程用 utf-8 读会 UnicodeDecodeError。
# 强制本进程以 utf-8 输出，保证被 weekly.py 调用时解码一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from playwright.sync_api import sync_playwright

BASE = "https://course.buct.edu.cn"
SSO_LOGIN = BASE + "/meol/homepage/common/sso_login.jsp"
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path.home() / ".workbuddy" / "profiles" / "buct-course-edge"
STATE_FILE = ROOT / "storage_state.json"
DEBUG_DIR = ROOT / "debug"

# 平台时间格式：2026年11月26日21:13:00
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2}(?::\d{2})?)?")


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
    """只取「待提交作业」分组（onclick 含 t=hw）。"""
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
        m = DATE_RE.search(r[1]["text"]) if len(r) > 1 else None
        if not m:
            continue
        deadline = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if m.group(4):
            deadline += " " + m.group(4)[:5]
        out.append({
            "title": title_cell["text"].strip()[:60],
            "deadline": deadline,
            "publisher": (r[3]["text"].strip() if len(r) > 3 else ""),
            "hwtid": hwtid,
            "content_url": title_cell["href"],                       # hwtask.view.jsp?hwtid=
            "submit_url": (r[5]["href"] if len(r) > 5 else None),    # write.jsp（未提交）
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--days", type=int, default=0, help=">0 时只保留未来 N 天内截止的")
    ap.add_argument("--content", help="抓取某条作业正文（传 hwtid），输出 JSON")
    ap.add_argument("--with-content", action="store_true",
                    help="抓取列表的同时逐条抓作业正文（标题只是标题，正文才是内容，较慢）")
    args = ap.parse_args()

    if args.content:
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

    with sync_playwright() as p:
        ctx = make_context(p, headless=True)
        page = ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page()
        page.goto(SSO_LOGIN, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        dump(page, "home", args.debug)

        if not logged_in(page, ctx):
            print("ERROR: 未登录或会话已过期，请运行: python login.py", file=sys.stderr)
            print(json.dumps({"ok": False, "homeworks": []}, ensure_ascii=False))
            return 1
        print("已登录", file=sys.stderr)

        courses = parse_courses(page)
        print(f"待提交作业课程: {len(courses)} 门", file=sys.stderr)

        homeworks = []
        for c in courses:
            try:
                rows = fetch_course(ctx, page, c["lid"], args.debug)
                for r in rows:
                    r["course"] = c["course"]
                    r["lid"] = c["lid"]
                homeworks.extend(rows)
                print(f"  {c['course']}: {len(rows)} 条", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"  {c['course']} 抓取失败: {e}", file=sys.stderr)

        if args.days > 0:
            now = datetime.now()
            kept = []
            for hw in homeworks:
                try:
                    dt = datetime.strptime(hw["deadline"], "%Y-%m-%d %H:%M")
                except ValueError:
                    dt = datetime.strptime(hw["deadline"], "%Y-%m-%d")
                if now <= dt <= now + __import__("datetime").timedelta(days=args.days):
                    kept.append(hw)
            homeworks = kept

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

        print(json.dumps(
            {"ok": True, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "courses": courses, "homeworks": homeworks},
            ensure_ascii=False, indent=2))
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
