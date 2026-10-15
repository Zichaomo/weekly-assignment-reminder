# -*- coding: utf-8 -*-
"""控制台启动提示（中文）—— 由 console.bat 调用。

为什么单独放这里：
  cmd 按系统 OEM 代码页（中文系统是 GBK）解析批处理文件，
  若 console.bat 里直接写 UTF-8 中文，会被误读成命令并打印
  「'...' 不是内部或外部命令」。所以批处理保持纯 ASCII，
  中文提示交给 Python 输出（它天然按 UTF-8 处理）。

用法：python launcher_msg.py <key>
  ok / reuse / no_edge / reuse_no_edge / no_pid
"""
import sys

MESSAGES = {
    "ok": [
        "",
        "  控制台已启动：http://127.0.0.1:8277",
        "  关闭浏览器窗口后服务会自动停止，本窗口即将自动关闭。",
    ],
    "reuse": [
        "",
        "  控制台已在运行，已把已有窗口置于前台（未新建窗口）。",
    ],
    "reuse_nofocus": [
        "",
        "  控制台服务已在运行，但没有找到它的窗口（可能已被最小化或关闭）。",
        "  请手动打开：http://127.0.0.1:8277",
    ],
    "no_edge": [
        "",
        "  未找到 Edge —— 这是唯一会用【默认浏览器】的情况。",
        "  已用默认浏览器打开：http://127.0.0.1:8277",
        "  服务将常驻运行；可点页面底部「停止控制台服务」停止。",
    ],
    "no_pid": [
        "",
        "  未能取得 Edge 进程号，因此【没有打开新的浏览器窗口】。",
        "  服务已在后台启动，请手动访问：http://127.0.0.1:8277",
        "  （这样做是为避免用默认浏览器另开一个标签页，导致窗口形态不一致）",
        "  可点页面底部「停止控制台服务」停止服务。",
    ],
}


def _fix_console_encoding() -> None:
    """让输出匹配【控制台】的代码页。

    中文 Windows 的控制台默认是 GBK(936)，而 Python 默认按 UTF-8 写 stdout，
    于是中文在 cmd 窗口里变成乱码。这里按控制台实际代码页重新配置 stdout；
    控制台不可用时（如被重定向到管道）退回 UTF-8 并容错。
    """
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp:
            sys.stdout.reconfigure(encoding=f"cp{cp}", errors="replace")
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _fix_console_encoding()
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    for line in MESSAGES.get(key, []):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
