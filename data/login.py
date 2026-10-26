# -*- coding: utf-8 -*-
"""北化在线（THEOL）登录：自动填充 + 自动点登录，人工只兜底。

流程：
  1. 用持久 Edge profile（存过北化账号密码的那份）打开 sso_login.jsp（免密入口）。
  2. 会话仍有效 -> 直接保存 storage_state.json 并退出。
  3. 落到登录页 -> 在 iframe `login-normal.html` 里点一下用户名，触发 Edge 自动填充，
     再点密码框，然后自动点「登录」。
  4. 若平台弹腾讯滑块验证码（登录页确实加载了 turing.captcha.gtimg.com 的 SDK），
     脚本**不关窗口**，提示你在这个窗口里手动拖一下滑块；脚本同时继续轮询，
     一登录成功立刻把会话落盘。

密码不落盘：只保存登录后的 cookie / storage_state（复用 profile 里 Edge 自己存的密码）。

用法:
    python login.py              # 自动登录优先，卡住则转人工（同一窗口，默认）
    python login.py --manual     # 跳过自动填充，直接人工登录
    python login.py --no-wait    # 自动提交后只等 45 秒，不成不转人工（给定时任务用）
    python login.py --check      # 只检查 storage_state.json 是否仍有效
    python login.py --headless   # 无头（只能跑 --check 场景）
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from platform_lock import FetchLock, LockTimeout

# Windows 下子进程 stdout 默认 GBK，父进程按 utf-8 读会乱码，统一强制 utf-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BASE = "https://course.buct.edu.cn"
LOGIN_URL = BASE + "/meol/homepage/common/sso_login.jsp"   # 免密入口（不是 /meol）
HOME_URL = BASE + "/meol"

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path.home() / ".workbuddy" / "profiles" / "buct-course-edge"
STATE_FILE = ROOT / "storage_state.json"
SHOT_FILE = ROOT / "login_result.png"

AUTO_TIMEOUT_SEC = 150    # 自动阶段等待上限（秒）
MANUAL_TIMEOUT_SEC = 300  # 转人工后留给用户点滑块/输验证码的时间
POLL_STEP = 2


# ---------------------------------------------------------------- 启动浏览器
def launch_ctx(p, headless=False, retries=3):
    """本机 Edge 偶发启动即崩，重试几次。

    注意：登录必须用持久 profile —— Edge 的「自动填充账号密码」存在 profile 里，
    换成 storage_state 或临时 context 都拿不到自动填充。
    """
    last = None
    for i in range(retries):
        try:
            return p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                channel="msedge",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--start-maximized",          # 让你一眼看见窗口，方便手动过验证码
                ],
                viewport=None,
                locale="zh-CN",
                ignore_https_errors=True,
            )
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"浏览器启动失败({i+1}/{retries}): {type(e).__name__}: {e}")
            time.sleep(2)
    raise last


# ---------------------------------------------------------------- 登录判定
def collect_text(page) -> str:
    """主 frame + 所有子 frame 的文本。"""
    try:
        text = page.inner_text("body", timeout=3000) or ""
    except Exception:  # noqa: BLE001
        text = ""
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        try:
            text += "\n" + (fr.inner_text("body", timeout=2000) or "")
        except Exception:  # noqa: BLE001
            pass
    return text


def is_logged_in(page) -> bool:
    """登录判定。

    硬指标一：平台内页出现「在线总时长 / 登录时间」（登录后才有的个人页信息）。
    硬指标二：CAS 登录成功后下发 CASTGC / TGC（Ticket Granting Cookie）。

    注意：门户首页（portal.buct.edu.cn）无论登录与否 body 都可能为空（SPA），
    不能凭空 body 判登录；登录页本身会显示「忘记/修改密码」等字样，要排除。
    """
    url = (page.url or "").lower()
    # 停在任一登录页就一定是「未登录」：旧 CAS / 旧门户 iframe / 新版 sidportal
    if ("cas/login" in url or "experimental-auth-endpoint" in url
            or "sidportal" in url and "/login" in url):
        return False

    text = ""
    try:
        text = collect_text(page)
    except Exception:  # noqa: BLE001
        pass
    if "在线总时长" in text or "登录时间" in text:
        return True

    try:
        names = {c["name"].upper() for c in page.context.cookies()}
    except Exception:  # noqa: BLE001
        names = set()

    if "CASTGC" in names or "TGC" in names:
        # 有票据也可能还停在登录页（票据是门户的），用登录页特征排除
        if "忘记/修改密码" in text or "涉密不上网" in text:
            return False
        return True

    if "course.buct.edu.cn" in url:
        if "非统一身份认证登录" in text or "统一身份认证登录" in text:
            return False
        if "personal.do" in url:
            return True
        return any(m in text for m in ("我的课程", "注销", "退出", "个人中心"))

    # 门户首页：未登录也会显示，不能据此判已登录
    if "非统一身份认证登录" in text or "统一身份认证登录" in text:
        return False
    return any(m in text for m in ("我的课程", "注销", "退出", "个人中心"))


def _logged_in_via_state() -> bool:
    """用 storage_state.json 新建无头上下文判断登录是否仍有效。

    必须与 scrape.py 同路径（launch + new_context(storage_state=...)），
    不能查持久 profile —— 那是另一份登录态，会误报 NEED_LOGIN。

    ⚠️ 这里绝不能写 `page.goto(HOME_URL)`（/meol）做二次判定：
    那个地址是**未登录首页**，一旦被重定向过去，就算会话完全有效也会被判成 NEED_LOGIN
    ——每周日的定时任务会因此误报「登录态失效」并发出告警。只认 SSO 入口 + 平台内页特征。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge", args=["--no-sandbox"])
        ctx = browser.new_context(storage_state=str(STATE_FILE))
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        ok = is_logged_in(page)
        if not ok:
            # 兜底：SSO 入口若停在门户壳上，直接看平台内页的个人信息字样
            try:
                txt = ""
                for fr in page.frames:
                    try:
                        txt += (fr.inner_text("body", timeout=2500) or "")
                    except Exception:  # noqa: BLE001
                        pass
                ok = ("在线总时长" in txt) or ("登录时间" in txt)
            except Exception:  # noqa: BLE001
                pass
        try:
            ctx.close()
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        return ok


# ---------------------------------------------------------------- 自动登录
def is_new_sso(page) -> bool:
    """是否落在 2026-10 改版后的新统一认证页（sidportal，Angular 单页、没有 iframe）。"""
    u = (page.url or "").lower()
    return ("sidportal" in u) or ("experimental-auth-endpoint" in u)


def ensure_password_tab(page) -> bool:
    """新版登录页默认停在「企业微信扫码」，必须以「用户名密码」标签为前提。

    这一步是改版后最关键的新增动作：以前表单在 iframe 里直接可见，
    现在它是四个标签中的第一个，不切过去就根本填不了。
    """
    if not is_new_sso(page):
        return True
    for sel in (".ant-tabs-tab:has-text('用户名密码')",
                "[class*=tabs-tab]:has-text('用户名密码')"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4000)
                page.wait_for_timeout(1200)
                if page.locator("#submitBtn, input[type=password]").count():
                    return True
        except Exception:  # noqa: BLE001
            continue
    try:
        page.get_by_text("用户名密码", exact=True).first.click(timeout=3000)
        page.wait_for_timeout(1200)
        return True
    except Exception:  # noqa: BLE001
        return False


def _saved_credentials():
    """从本机 Edge 密码库取北化账号（运行时解密，不落盘）。"""
    try:
        sys.path.insert(0, str(ROOT))
        import edge_cred
        return edge_cred.get_saved_credentials(verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"  凭据：读取 Edge 密码库失败（{type(e).__name__}: {e}）")
        return None, None


def _visible(loc):
    """在一个定位器匹配到的多个元素里挑第一个可见的。"""
    try:
        n = loc.count()
    except Exception:  # noqa: BLE001
        return None
    for i in range(min(n, 6)):
        try:
            el = loc.nth(i)
            if el.is_visible():
                return el
        except Exception:  # noqa: BLE001
            continue
    return loc.first if n else None


def find_login_frame(page):
    """登录表单所在位置。

    旧版：portal 的 iframe `normal/login-normal.html`。
    新版（2026-10）：sidportal 的 Angular 主页面上，直接返回 page。
    """
    for fr in page.frames:
        try:
            if "login-normal.html" in (fr.url or ""):
                return fr
        except Exception:  # noqa: BLE001
            pass
    if is_new_sso(page):
        return page
    return None


def read_value(loc) -> str:
    """读输入框当前值；读不到就返回空串。"""
    try:
        return (loc.input_value(timeout=800) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def has_captcha(page) -> bool:
    """腾讯滑块 / 图形验证码 / 短信验证码是否已弹出（含新版页面的选择器）。"""
    try:
        if page.locator(
            "iframe[src*='captcha'], iframe[src*='turing'], iframe[id*='tcaptcha'], "
            "iframe[src*='challenge']"
        ).count():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        for s in ("#tcaptcha_iframe", ".tcaptcha-transform", "#TencentCaptcha",
                  ".nc-container", "#nc_1_wrapper", "#aliyunCaptcha",
                  "img[src*='captcha' i]", ".captcha-img", "#captchaImg",
                  "input[placeholder*='验证码']"):
            el = page.locator(s)
            if el.count() and el.first.is_visible():
                return True
    except Exception:  # noqa: BLE001
        pass
    return False



def dismiss_device_dialog(page) -> bool:
    """登录后门户会弹「是否添加常用设备？」。

    选「是」= 把本机记为常用设备，之后再登录通常不再要滑块 —— 这是自动登录的长期收益，
    所以这里点「是」；点不到就跳过，不影响主流程。
    """
    for fr in page.frames:
        try:
            yes = fr.locator(".device-dialog-btns-change-rember")
            if yes.count():
                yes.first.click(timeout=1500)
                print("  已自动点击「是否添加常用设备？→ 是」")
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _sync_values(fr, u_val: str, p_val: str) -> None:
    """把账号密码写回 DOM 并派发 input/change 事件。

    自动填充（或 fill）有时只改 DOM 不触发框架事件，提交时 Angular/Vue
    从自己的模型取到的仍是空值 —— 那样点下去只会白等。
    """
    try:
        fr.evaluate("""([u, p]) => {
            const us = document.querySelectorAll(
                "#nameInput, input[name='username']:not([type=hidden])");
            const ps = document.querySelectorAll("input[type='password']");
            const fire = (el, v) => {
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            us.forEach(el => fire(el, u));
            ps.forEach(el => fire(el, p));
        }""", [u_val, p_val])
    except Exception as e:  # noqa: BLE001
        print(f"  提示：同步表单值失败（{type(e).__name__}），继续尝试提交")


def _click_submit(page, fr, p_loc):
    """点登录按钮。新版是 #submitBtn（两个框都有值才从 disabled 变可点）。"""
    if is_new_sso(page):
        btn = _visible(page.locator("#submitBtn"))
        if btn is not None:
            try:
                page.wait_for_function(
                    "() => { const b = document.querySelector('#submitBtn');"
                    " return !!b && !/\\bdisabled\\b/.test(b.className); }",
                    timeout=8000)
            except Exception:  # noqa: BLE001
                print("  提示：登录按钮仍是禁用态（可能值没进框架模型），稍后重试")
            try:
                btn.click(timeout=4000)
                return "ok"
            except Exception as e:  # noqa: BLE001
                print(f"  点「立即登录」失败（{type(e).__name__}），改按回车")

    btn = fr.locator("button:has-text('登录'), button:has-text('立即登录'), "
                     "input[type=submit][value*='登录'], .login-btn, #loginBtn")
    try:
        b = _visible(btn)
        if b is not None:
            b.click(timeout=4000)
            return "ok"
    except Exception as e:  # noqa: BLE001
        print(f"  点登录按钮失败（{type(e).__name__}），改按回车")
    try:
        p_loc.press("Enter")
        return "ok"
    except Exception as e:  # noqa: BLE001
        print(f"  回车也失败：{e}")
        return "noclick"


def auto_submit(page) -> str:
    """尝试自动登录一次，返回结果码：

    ok       —— 已点下登录按钮
    noframe  —— 没找到登录表单（多半已经在登录后的页面，或页面结构又变了）
    nopass   —— 表单在，但账号密码拿不到（自动填充为空，Edge 密码库里也没有）
    noclick  —— 填了密码但没点到登录按钮

    2026-10 平台改版后新增了两步：
      1. 登录页默认停在「企业微信扫码」标签 —— 必须切到「用户名密码」；
      2. Edge 的自动填充绑在**旧域名** origin 上，新域名填不上 ——
         所以自动填充只是"优先尝试"，失败就用 Edge 密码库（运行时解密）兜底。
    """
    if not ensure_password_tab(page):
        return "noframe"
    fr = find_login_frame(page)
    if not fr:
        return "noframe"

    if is_new_sso(page):
        u_loc = _visible(fr.locator("#nameInput"))
        p_loc = _visible(fr.locator("input[type='password']"))
    else:
        u_loc = _visible(fr.locator("input[name='username']"))
        p_loc = _visible(fr.locator("input[name='password']"))
    if u_loc is None or p_loc is None:
        return "noframe"
    try:
        u_loc.wait_for(state="visible", timeout=15000)
        p_loc.wait_for(state="attached", timeout=5000)
    except Exception:  # noqa: BLE001
        return "noframe"

    # ---- 第一步：点一下用户名，看 Edge 自动填充会不会灌值 ----
    u_val = ""
    for _ in range(5):
        try:
            u_loc.click(timeout=1000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
        u_val = read_value(u_loc)
        if u_val:
            break

    p_val = ""
    if u_val:
        for _ in range(4):
            try:
                p_loc.click(timeout=1000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.6)
            p_val = read_value(p_loc)
            if p_val:
                break
        print(f"  Edge 自动填充：账号 {u_val}，"
              f"密码 {'*' * min(len(p_val), 8) if p_val else '（空）'}")

    # ---- 第二步：填充拿不到就用 Edge 密码库（新版域名必然走这里）----
    if not u_val or not p_val:
        user, pwd = _saved_credentials()
        if not user or not pwd:
            return "nopass"
        try:
            u_loc.fill(user)
            p_loc.fill(pwd)
        except Exception as e:  # noqa: BLE001
            print(f"  填入凭据失败（{type(e).__name__}: {e}）")
            return "nopass"
        u_val, p_val = read_value(u_loc), read_value(p_loc)
        print(f"  已用 Edge 密码库填入：账号 {u_val}，"
              f"密码 {'*' * min(len(p_val), 8) if p_val else '（空）'}")
    if not u_val or not p_val:
        return "nopass"

    _sync_values(fr, u_val, p_val)
    return _click_submit(page, fr, p_loc)



def save_state(ctx, page, why: str) -> bool:
    """落盘会话 + 截图。"""
    time.sleep(1.5)
    try:
        ctx.storage_state(path=str(STATE_FILE))
    except Exception as e:  # noqa: BLE001
        print(f"保存会话失败：{e}")
        return False
    print(f"OK: {why}，会话已保存到 {STATE_FILE}")
    print("URL:", page.url)
    try:
        page.screenshot(path=str(SHOT_FILE), full_page=False)
        print("截图:", SHOT_FILE)
    except Exception:  # noqa: BLE001
        pass
    return True


def launch_any(p, headless=False):
    """优先用持久 profile；被别的窗口占用时退回临时上下文。

    为什么要这层降级（2026-10-18 实测）：计划任务在 09:08 补跑时，持久 profile
    正被另一个 Edge 窗口占用，Edge 只回一句「已在现有浏览器会话中打开」就退出，
    `launch_persistent_context` 抛 TargetClosedError，3 次重试全废 —— 一次登录
    都没尝试就失败了。而改版后凭据本来就不依赖 profile（由 edge_cred 从密码库取），
    所以退回临时上下文同样能自动登录，只是少了一点设备指纹的连续性。
    """
    try:
        return launch_ctx(p, headless=headless)
    except Exception as e:  # noqa: BLE001
        print(f"！持久 profile 启动失败（{type(e).__name__}），改用临时上下文继续…")
        browser = p.chromium.launch(headless=headless, channel="msedge",
                                    args=["--no-sandbox"])
        return browser.new_context(ignore_https_errors=True, locale="zh-CN",
                                   viewport=None)


# ---------------------------------------------------------------- 主流程
def run_auto(headless=False, no_wait=False) -> int:
    """自动登录优先；卡住则不关窗口，转人工，同一窗口继续等。"""
    with sync_playwright() as p:
        ctx = launch_any(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        if is_logged_in(page):
            ok = save_state(ctx, page, "会话仍然有效，免登录")
            ctx.close()
            return 0 if ok else 1

        print("=" * 64)
        print("未登录，开始自动登录：")
        if is_new_sso(page):
            print("  平台已改版为 sidportal 统一身份认证（无 iframe）——")
            print("  自动切到「用户名密码」标签，填账号密码后点「立即登录」。")
            print("  凭据优先取 Edge 自动填充；取不到则从 Edge 密码库解密（不落盘）。")
        else:
            print("  Edge 会自动填充账号密码，脚本自动点「登录」。")
        print("=" * 64)

        result = auto_submit(page)

        if result == "nopass":
            print("！Edge 没有自动填充密码（profile 里没存 / 密码已变）。")
            print("  请在浏览器窗口里手动输入一次账号密码并登录；")
            print("  登录成功后 Edge 会询问是否保存密码，选「保存」，下次就能全自动了。")
        elif result == "noframe":
            print("！没找到登录表单（页面可能还在加载，或已经处于登录态）。")
        elif result == "noclick":
            print("！找到了密码但点不到「登录」按钮，请手动点一下。")
        else:
            print("已提交登录，等待平台响应...")

        deadline = time.time() + (45 if no_wait else AUTO_TIMEOUT_SEC)
        manual_deadline = None
        submitted = result == "ok"
        warned_captcha = False
        warned_manual = False
        last_retry = 0.0

        while True:
            time.sleep(POLL_STEP)

            if is_logged_in(page):
                dismiss_device_dialog(page)
                time.sleep(1.5)
                if is_logged_in(page):
                    ok = save_state(ctx, page, "自动登录成功")
                    ctx.close()
                    return 0 if ok else 1

            now = time.time()

            if has_captcha(page) and not warned_captcha:
                warned_captcha = True
                print("=" * 64)
                print("⚠️ 平台弹出了验证码（腾讯滑块 / 短信验证码）。")
                print("   请在已打开的浏览器窗口里拖动滑块完成验证 —— 脚本会继续等，")
                print("   一登录成功就自动保存会话并退出。")
                print("=" * 64)

            # 自动阶段：每 8 秒重试一次提交（验证码过掉后 / 自动填充晚到都能补上）
            if now < deadline and not no_wait and not warned_captcha:
                if now - last_retry >= 8:
                    last_retry = now
                    if not submitted:
                        r = auto_submit(page)
                        submitted = r == "ok"
                        if submitted:
                            print("  重试提交成功，继续等待...")

            if now >= deadline:
                if no_wait:
                    print("自动阶段结束（--no-wait）。")
                    break
                if manual_deadline is None:
                    manual_deadline = now + MANUAL_TIMEOUT_SEC
                    print("=" * 64)
                    print(f"自动登录还没成功。窗口保持打开，我再等 {MANUAL_TIMEOUT_SEC} 秒：")
                    print("  请在该窗口手动完成登录（滑块 / 验证码 / 输密码都行）。")
                    print("  成功的话我立刻保存会话；不想等了，直接关掉浏览器窗口即可。")
                    print("=" * 64)

            if manual_deadline is not None and now >= manual_deadline:
                print("TIMEOUT: 人工阶段也超时，未检测到登录成功。")
                try:
                    page.screenshot(path=str(SHOT_FILE))
                    print("已截图:", SHOT_FILE)
                except Exception:  # noqa: BLE001
                    pass
                break

            # 用户手动关了窗口 -> 别再等了
            try:
                if page.is_closed():
                    print("浏览器窗口已被关闭，退出。")
                    break
            except Exception:  # noqa: BLE001
                pass

        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        return 1


def run_manual(headless=False) -> int:
    """纯人工登录（保留原来的行为）。"""
    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        print("=" * 64)
        print("浏览器已打开，请在窗口中完成统一认证登录（学号/密码/验证码）。")
        print(f"登录成功后脚本会自动检测并保存会话，最长等待 {MANUAL_TIMEOUT_SEC} 秒。")
        print("=" * 64)

        deadline = time.time() + MANUAL_TIMEOUT_SEC
        ok = False
        while time.time() < deadline:
            if is_logged_in(page):
                ok = True
                break
            time.sleep(POLL_STEP)

        if not ok:
            print("TIMEOUT: 未检测到登录成功。当前 URL:", page.url)
            try:
                page.screenshot(path=str(SHOT_FILE))
            except Exception:  # noqa: BLE001
                pass
            ctx.close()
            return 1

        ok = save_state(ctx, page, "人工登录成功")
        try:
            body = page.inner_text("body")
            head = "\n".join(l.strip() for l in body.splitlines() if l.strip())[:500]
            print("---- 页面摘录 ----")
            print(head)
        except Exception as e:  # noqa: BLE001
            print("读取页面文本失败:", e)
        ctx.close()
        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查会话是否有效")
    ap.add_argument("--auto", action="store_true", help="自动登录（默认行为，保留此参数兼容旧用法）")
    ap.add_argument("--manual", action="store_true", help="跳过自动填充，直接人工登录")
    ap.add_argument("--no-wait", action="store_true",
                    help="自动提交后只等 45 秒，不成不转人工（给定时任务用）")
    ap.add_argument("--headless", action="store_true", help="无头模式（无法登录，只能验证脚本）")
    args = ap.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with FetchLock(wait_sec=120):
            if args.check:
                ok = _logged_in_via_state()
                print("LOGGED_IN" if ok else "NEED_LOGIN")
                return 0 if ok else 1
            if args.manual:
                return run_manual(headless=args.headless)
            return run_auto(headless=args.headless, no_wait=args.no_wait)
    except LockTimeout as exc:
        print(f"NEED_LOGIN: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
