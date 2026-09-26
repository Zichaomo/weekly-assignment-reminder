# -*- coding: utf-8 -*-
"""
通过本机已登录的 PC 微信客户端发送文本消息（纯键盘 + 剪贴板驱动）。

为什么不用 UIA：微信 4.x(Weixin.exe) 是 Qt 渲染，控件树几乎为空，
拿不到搜索框/输入框控件，只能靠窗口激活 + 键盘快捷键 + 剪贴板粘贴。
本脚本不注入微信进程、不使用第三方微信协议，只做本机键鼠模拟。

局限（务必知道）：
  - 无法程序化校验搜索命中的是不是预期会话，只能靠结果截图人工确认。
  - 因此默认目标是「文件传输助手」，只有你自己能看到，发错也无损失。

用法:
    python wechat_send.py --to "文件传输助手" --message "hello" --no-send  # 演练
    python wechat_send.py --to "文件传输助手" --file message.txt           # 真发
"""
import argparse
import ctypes
import subprocess
import sys
import time

# Windows 下子进程 stdout 默认按 GBK(cp936) 编码，父进程用 utf-8 读会 UnicodeDecodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import uiautomation as auto
from pywinauto import Application

VK_SHIFT = 0x10


def _force_foreground(hwnd):
    """Windows 经典技巧：Alt 键事件 + AttachThreadInput 把 hwnd 强制前置并获得焦点。"""
    VK_MENU = 0x12
    ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_MENU, 0, 2, 0)
    fg = ctypes.windll.user32.GetForegroundWindow()
    fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(fg, 0)
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != cur_tid:
        ctypes.windll.user32.AttachThreadInput(cur_tid, fg_tid, True)
        attached = True
    try:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.SetFocus(hwnd)
    finally:
        if attached:
            ctypes.windll.user32.AttachThreadInput(cur_tid, fg_tid, False)

WINDOW_CLASS_CANDIDATES = ["Qt51514QWindowIcon", "WeChatMainWndForPC", "WeixinWindow"]
WINDOW_NAME_CANDIDATES = ["微信", "WeChat", "Weixin"]


def find_window():
    """返回 (hwnd, name, classname)。"""
    for cls in WINDOW_CLASS_CANDIDATES:
        try:
            w = auto.WindowControl(searchDepth=1, ClassName=cls)
            if w.Exists(maxSearchSeconds=1):
                return w.NativeWindowHandle, w.Name, w.ClassName
        except Exception:  # noqa: BLE001
            pass
    for name in WINDOW_NAME_CANDIDATES:
        try:
            w = auto.WindowControl(searchDepth=1, Name=name)
            if w.Exists(maxSearchSeconds=1):
                return w.NativeWindowHandle, w.Name, w.ClassName
        except Exception:  # noqa: BLE001
            pass
    return None, None, None


def set_clipboard_text(text: str) -> None:
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
        return
    except Exception as e:  # noqa: BLE001
        print("win32clipboard 失败，回退 clip.exe:", e)
    p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
    p.communicate(input=text.encode("utf-16-le"))


def send(target: str, message: str, do_send: bool = True):
    hwnd, name, cls = find_window()
    if not hwnd:
        print("未找到微信窗口，请确认 PC 微信已登录运行。")
        return 1
    print(f"窗口: {name} | {cls} | hwnd={hwnd}")

    app = Application(backend="win32").connect(handle=hwnd)
    win = app.window(handle=hwnd)

    # 唤醒（若最小化）
    try:
        if win.is_minimized():
            win.restore()
    except Exception:  # noqa: BLE001
        pass

    # 绕前台锁：模拟 Shift + 反复 SetForegroundWindow（最多 5 次）
    activated = False
    for _ in range(5):
        _force_foreground(hwnd)
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            activated = True
            break
    if not activated:
        print("WARN: 微信未能切到前台（前台锁未解除），请把微信手动切前台后再重试。")

    try:
        win.set_focus()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.8)

    # 打开搜索（Ctrl+F）
    win.type_keys("^f")
    time.sleep(1.2)

    # 粘贴目标名（剪贴板避免输入法问题）
    set_clipboard_text(target)
    win.type_keys("^v")
    time.sleep(2.5)  # 等候选项列表渲染

    # 强制 DOWN 选第一项再 Enter（避免光标未移到候选项导致进入原会话）
    win.type_keys("{DOWN}")
    time.sleep(0.3)
    win.type_keys("{ENTER}")
    time.sleep(1.2)

    # 粘贴正文
    set_clipboard_text(message)
    win.type_keys("^v")
    time.sleep(0.8)

    if not do_send:
        print("演练模式：已填入输入框，未发送。请到微信窗口确认会话正确后手动回车。")
        return 0

    win.type_keys("{ENTER}")
    time.sleep(0.6)
    print("已发送。")
    return 0


def fill_and_screenshot(target: str, message: str, shot_path: str):
    """填好输入框并截图，用于人工/程序校验发送对象。"""
    hwnd, name, cls = find_window()
    if not hwnd:
        print("未找到微信窗口。")
        return None
    app = Application(backend="win32").connect(handle=hwnd)
    win = app.window(handle=hwnd)
    try:
        if win.is_minimized():
            win.restore()
    except Exception:
        pass
    for _ in range(5):
        _force_foreground(hwnd)
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            break
    try:
        win.set_focus()
    except Exception:
        pass
    time.sleep(0.8)
    win.type_keys("^f")
    time.sleep(1.2)
    set_clipboard_text(target)
    win.type_keys("^v")
    time.sleep(2.5)
    win.type_keys("{DOWN}")
    time.sleep(0.3)
    win.type_keys("{ENTER}")
    time.sleep(1.2)
    set_clipboard_text(message)
    win.type_keys("^v")
    time.sleep(0.8)

    if shot_path:
        from ctypes import wintypes

        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        from PIL import ImageGrab

        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom)).save(shot_path)
    print("已填好输入框，截图:", shot_path)
    return shot_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="文件传输助手", help="目标：好友备注名 / 群名 / 文件传输助手")
    ap.add_argument("--message", help="要发送的文本")
    ap.add_argument("--file", help="从 utf-8 文件读取正文")
    ap.add_argument("--no-send", action="store_true", help="演练：填好输入框但不按发送")
    ap.add_argument("--confirm", action="store_true", help="填好输入框并截图到 confirm.png，不发送")
    args = ap.parse_args()

    if args.file:
        message = open(args.file, encoding="utf-8").read()
    elif args.message:
        message = args.message
    else:
        print("需要 --message 或 --file")
        return 2

    if args.confirm:
        from pathlib import Path

        shot = str(Path(__file__).resolve().parent / "confirm.png")
        return 0 if fill_and_screenshot(args.to, message, shot) else 1
    return send(args.to, message, do_send=not args.no_send)


if __name__ == "__main__":
    sys.exit(main())
