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
    if "cas/login" in url or "experimental-auth-endpoint" in url:
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
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge", args=["--no-sandbox"])
        ctx = browser.new_context(storage_state=str(STATE_FILE))
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        ok = is_logged_in(page)
        if not ok:
            # storage_state 里没带 TGC 时，判定只能靠平台内页文本
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                txt = page.inner_text("body", timeout=5000) or ""
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
def find_login_frame(page):
    """登录表单在 portal 的 iframe `normal/login-normal.html` 里。"""
    for fr in page.frames:
        try:
            if "login-normal.html" in (fr.url or ""):
                return fr
        except Exception:  # noqa: BLE001
            pass
    return None


def read_value(loc) -> str:
    """读输入框当前值；读不到就返回空串。"""
    try:
        return (loc.input_value(timeout=800) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def has_captcha(page) -> bool:
    """腾讯滑块 / 短信验证码是否已弹出。"""
    try:
        if page.locator(
            "iframe[src*='captcha'], iframe[src*='turing'], iframe[id*='tcaptcha']"
        ).count():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        for s in ("#tcaptcha_iframe", ".tcaptcha-transform", "#TencentCaptcha",
                  ".nc-container", "#nc_1_wrapper", "#aliyunCaptcha"):
            if page.locator(s).count():
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


def auto_submit(page) -> str:
    """尝试自动登录一次，返回结果码：

    ok       —— 已点下登录按钮
    noframe  —— 没找到登录 iframe（多半已经在登录后的页面，或页面结构变了）
    nopass   —— iframe 在，但密码框是空的（Edge 没有自动填充）
    noclick  —— 填了密码但没点到登录按钮
    """
    fr = find_login_frame(page)
    if not fr:
        return "noframe"

    user = fr.locator("input[name='username']").first
    pwd = fr.locator("input[name='password']").first
    try:
        user.wait_for(state="visible", timeout=15000)
        pwd.wait_for(state="attached", timeout=5000)
    except Exception:  # noqa: BLE001
        return "noframe"

    # 点一下用户名，触发 Edge 密码管理器的自动填充（实测要几秒才灌进 DOM）
    u_val = ""
    for _ in range(10):
        try:
            user.click(timeout=1000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        u_val = read_value(user)
        if u_val:
            break
    if not u_val:
        return "nopass"

    # 再点密码框，让密码也灌进来
    p_val = ""
    for _ in range(8):
        try:
            pwd.click(timeout=1000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
        p_val = read_value(pwd)
        if p_val:
            break
    masked = "*" * min(len(p_val), 8) if p_val else "（空！）"
    print(f"  自动填充状态：账号 {u_val}，密码 {masked}")
    if not p_val:
        return "nopass"

    # 兜底：把值写回去并派发 input/change，保证 Vue 的 v-model 与 DOM 同步
    # （自动填充有时只改 DOM 不触发事件，提交时表单拿到的是空值）
    try:
        fr.evaluate("""([u, p]) => {
            const us = document.querySelectorAll("input[name='username']");
            const ps = document.querySelectorAll("input[name='password']");
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

    # 点「登录」：优先按钮，其次回车
    btn = fr.locator("button:has-text('登录'), input[type=submit][value*='登录'], .login-btn, #loginBtn")
    try:
        if btn.count():
            btn.first.click(timeout=4000)
            return "ok"
    except Exception as e:  # noqa: BLE001
        print(f"  点登录按钮失败（{type(e).__name__}），改按回车")
    try:
        pwd.press("Enter")
        return "ok"
    except Exception as e:  # noqa: BLE001
        print(f"  回车也失败：{e}")
        return "noclick"


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


# ---------------------------------------------------------------- 主流程
def run_auto(headless=False, no_wait=False) -> int:
    """自动登录优先；卡住则不关窗口，转人工，同一窗口继续等。"""
    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        if is_logged_in(page):
            ok = save_state(ctx, page, "会话仍然有效，免登录")
            ctx.close()
            return 0 if ok else 1

        print("=" * 64)
        print("未登录，开始自动登录：Edge 会自动填充账号密码，脚本自动点「登录」。")
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

    if args.check:
        ok = _logged_in_via_state()
        print("LOGGED_IN" if ok else "NEED_LOGIN")
        return 0 if ok else 1

    if args.manual:
        return run_manual(headless=args.headless)
    return run_auto(headless=args.headless, no_wait=args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
