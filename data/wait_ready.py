# -*- coding: utf-8 -*-
"""控制台就绪探测 —— 给 console.bat 用（批处理里写 HTTP 重试太脆，实测踩过）。

为什么需要它：
  · 启动顺序必须是「先起服务、再开浏览器」。反过来的话 Edge 请求到还没监听的
    端口，页面就是 ERR_CONNECTION_REFUSED「拒绝访问」，得手按刷新。
  · 判断「另一个实例是否在跑」也要看**监听**而非「能否连上」：
    connect_ex 对 TIME_WAIT 残连接同样返回 0，会把已退出的服务误判成在运行。

所有网络操作都用 connect_ex / settimeout，全程不起线程，保证批处理里的
调用总能快速返回、不会挂住。

用法：
    python wait_ready.py                 # 等就绪，最多 20 秒
    python wait_ready.py --timeout 5     # 自定义上限
    python wait_ready.py --probe         # 只探一次：0=已有实例在监听，3=没有

退出码：0 = 就绪 / 已有实例在监听；3 = 超时或未监听。
"""
import argparse
import socket
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HEARTBEAT = "http://127.0.0.1:8277/api/heartbeat"


def listening(port: int = 8277, host: str = "127.0.0.1") -> bool:
    """本机是否有进程在**监听**该端口。

    注意：不能直接对 127.0.0.1:port 做 connect_ex —— 主动关闭方留下的
    TIME_WAIT 残连接也会让它返回 0，于是「服务已经退出」被误判成「还在跑」，
    启动器就会走进复用分支、再也开不出窗口。
    """
    ok = False
    for family, addr in ((socket.AF_INET, (host, port)),
                         (socket.AF_INET6, ("::1", port))):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            if s.connect_ex(addr) == 0:
                ok = True
                break
        except OSError:
            continue
        finally:
            s.close()
    return ok


def http_ok(timeout: float = 1.5) -> bool:
    """服务是否真的能回答 HTTP：光端口在听还不够，要能回 /api/heartbeat。"""
    try:
        with urllib.request.urlopen(HEARTBEAT, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait(timeout: float, interval: float = 0.5) -> bool:
    """等就绪。每次 HTTP 探测的超时自动收敛到剩余时间，保证总耗时贴近 timeout。

    实测教训：不收敛时，调用方要 3 秒却等了 4.9 秒（最后一轮还在用满超时）。
    """
    deadline = time.time() + timeout
    while True:
        if listening():
            left = deadline - time.time()
            if left <= 0:
                return False
            if http_ok(timeout=min(1.5, max(0.2, left))):
                return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--probe", action="store_true",
                    help="只探一次端口监听（是否已有实例）")
    args = ap.parse_args()

    if args.probe:
        ok = listening()
        print("LISTENING" if ok else "FREE")
        return 0 if ok else 3

    if wait(args.timeout):
        print("READY")
        return 0
    print(f"NOT-READY（等待 {args.timeout:.0f} 秒后仍无法连接 {HEARTBEAT}）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
