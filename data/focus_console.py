# -*- coding: utf-8 -*-
"""控制台窗口工具：置前 / 最大化（console.bat 用）。

两个用途：
  1. 第二次双击 console.bat：把已存在的「作业提醒控制台」窗口置于前台
     （Edge 的 --app 每次都会新建实例，所以要改成"找旧窗口并激活"）。
  2. 首次启动：`--maximize` 把刚打开的应用窗口最大化。
     为什么还要这一步：Edge 对 `--app` 窗口**并不总是**遵守 `--start-maximized`
     （实测有版本会忽略），所以启动器两条都做：命令行带参数 + 这里用 Win32 强制。

做法：用 Win32 API 按【窗口标题】找到顶层窗口。窗口标题来自 console.html 的 <title>。

用法：
    python focus_console.py                     # 置前（标题默认「作业提醒控制台」）
    python focus_console.py --maximize --wait 8 # 最多等 8 秒找到窗口再最大化
    python focus_console.py 我的标题            # 自定义标题关键字
退出码：0 = 成功；1 = 没找到
"""
import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_MAXIMIZE = 3
SW_RESTORE = 9
SW_SHOW = 5

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# ⚠️ 必须声明函数原型（argtypes/restype）。
# 64 位下不声明时，ctypes 会把 HWND 当成 32 位 int 传进去 —— 调用会**静默失败**
# （实测：ShowWindow 返回了，窗口纹丝不动，IsZoomed 永远 False）。
# 这个坑本项目在 notify.py 的剪贴板代码里也踩过一次，同一个原因。
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetMonitorInfoW.restype = wintypes.BOOL
kernel32 = ctypes.windll.kernel32

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
    """激活并置前。Windows 对前台切换有限制，必要时用 AttachThreadInput 绕过。

    ⚠️ 只在**最小化**时才 SW_RESTORE。以前无条件 RESTORE，导致紧跟其后的
    最大化被自己撤销（maximize -> activate -> RESTORE，窗口又变回小窗），
    表现是"脚本说已最大化，窗口其实没变"。
    """
    if user32.IsIconic(hwnd):
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


def maximize(hwnd: int, tries: int = 6, delay: float = 0.35) -> bool:
    """最大化窗口；最小化的先还原，最后用「铺满工作区」兜底。

    两个实测坑：
      1. `ShowWindow(SW_MAXIMIZE)` 是**异步**的 —— 调用后立刻 `IsZoomed()` 往往还是 False，
         所以必须轮询，不能调一次就下结论（第一版就是因此误判成失败）。
      2. 有些 Edge 版本对 `--app` 窗口会自己恢复上次的窗口尺寸，把最大化状态冲掉；
         这时直接 `SetWindowPos` 铺满监视器工作区，效果等同最大化。
    """
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        for _ in range(max(1, tries)):
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            time.sleep(delay)
            if user32.IsZoomed(hwnd):
                return True
        return _fill_workarea(hwnd)
    except Exception:  # noqa: BLE001
        return False


def _fill_workarea(hwnd: int) -> bool:
    """把窗口铺满所在显示器的工作区（不含任务栏）。"""
    try:
        MONITOR_DEFAULTTONEAREST = 2
        SWP_SHOWWINDOW = 0x0040

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        w = mi.rcWork.right - mi.rcWork.left
        h = mi.rcWork.bottom - mi.rcWork.top
        user32.SetWindowPos(hwnd, None, mi.rcWork.left, mi.rcWork.top, w, h,
                            SWP_SHOWWINDOW)
        return True
    except Exception:  # noqa: BLE001
        return False


def _parse_args(argv: list) -> tuple:
    """-> (keywords, do_maximize, wait_sec)。以 -- 开头的是开关，其余都是标题关键字。"""
    kws, do_max, wait = [], False, 0.0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--maximize":
            do_max = True
        elif a == "--wait":
            if i + 1 < len(argv):
                try:
                    wait = float(argv[i + 1])
                except ValueError:
                    wait = 0.0
                i += 1
        elif a.startswith("--wait="):
            try:
                wait = float(a.split("=", 1)[1])
            except ValueError:
                wait = 0.0
        elif not a.startswith("--"):
            kws.append(a)
        i += 1
    return tuple(kws) or DEFAULT_KEYWORDS, do_max, wait


def main() -> int:
    kws, do_max, wait = _parse_args(sys.argv[1:])
    # 刚启动时标题还是 URL（"127.0.0.1"），要等页面加载完才变成「作业提醒控制台」。
    deadline = time.time() + max(0.0, wait)
    wins = []
    while True:
        wins = find_windows(kws)
        if wins or time.time() >= deadline:
            break
        time.sleep(0.4)
    if not wins:
        return 1
    # 优先选标题完全匹配的
    wins.sort(key=lambda x: (x[1] != "作业提醒控制台",))
    hwnd, title = wins[0]
    if do_max:
        ok_max = maximize(hwnd)
        activate(hwnd)
        print(f"  已最大化窗口：{title}" if ok_max else f"  最大化失败（仍已置前）：{title}")
        return 0      # 置前成功也算成功，别让批处理当失败
    ok = activate(hwnd)
    print(f"  已置前窗口：{title}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
