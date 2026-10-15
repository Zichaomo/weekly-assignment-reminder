# -*- coding: utf-8 -*-
"""把已存在的「作业提醒控制台」窗口置于前台（第二次双击 console.bat 时用）。

为什么不新开窗口：用户要的是复用同一个窗口。Edge 的 --app 每次都会新建实例，
所以第二次双击时必须改为"找到旧窗口并激活它"。

做法：用 Win32 API 按【窗口标题】找到顶层窗口，然后激活并置前。
窗口标题来自 console.html 的 <title>：作业提醒控制台

用法：python focus_console.py [窗口标题关键字]
退出码：0 = 已激活；1 = 没找到（由批处理决定如何提示）
"""
import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_RESTORE = 9
SW_SHOW = 5

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# 只认控制台窗口的标题（来自 console.html 的 <title>）。
# 注意：不能用「作业提醒」这种宽泛词 —— 本项目所在文件夹的
# 资源管理器窗口标题是「每周作业提醒」，会被误激活（实测踩过）。
DEFAULT_KEYWORDS = ("作业提醒控制台",)


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_windows(keywords) -> list:
    """返回 [(hwnd, title), ...]，按顶层可见窗口过滤。"""
    found = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title(hwnd)
        if t and any(k in t for k in keywords):
            found.append((hwnd, t))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def activate(hwnd: int) -> bool:
    """激活并置前。Windows 对前台切换有限制，必要时用 AttachThreadInput 绕过。"""
    user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.SetForegroundWindow(hwnd):
        return True
    # 兜底：把自己的输入队列挂到前台窗口线程上，再试一次
    try:
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(tid_me, tid_fg, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(tid_me, tid_fg, False)
    except Exception:  # noqa: BLE001
        pass
    return user32.GetForegroundWindow() == hwnd


def main() -> int:
    kws = tuple(sys.argv[1:]) or DEFAULT_KEYWORDS
    wins = find_windows(kws)
    if not wins:
        return 1
    # 优先选标题完全匹配的
    wins.sort(key=lambda x: (x[1] != "作业提醒控制台",))
    hwnd, title = wins[0]
    ok = activate(hwnd)
    print(f"  已置前窗口：{title}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
