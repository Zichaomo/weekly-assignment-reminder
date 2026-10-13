# -*- coding: utf-8 -*-
"""
本地 Web 控制台后端（data/server.py）

只做 I/O 接缝：
  - 静态托管 console.html 与字体文件
  - 把 hub.py / manual.py 的既有函数包成 HTTP JSON API
  - 长任务（检索/发布/取正文/刷新课程表）放后台线程，前端轮询进度

红线：scrape.py / weekly.py / notify.py / manual.py 的业务逻辑一行不改；
抓取参数组装仍由 hub.py 完成，本文件只传界面上的开关（天数/含全部/抓不抓测试）。

用法：双击 console.bat（或手动 `python server.py`），浏览器打开 http://127.0.0.1:8277
端口被占用时直接退出（视为已有实例在跑，console.bat 会继续打开浏览器）。
"""
import io
import json
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import hub      # noqa: E402  控制台后端：列表/发布/黑名单/正文/课程表
import manual   # noqa: E402  手动条目增删改查（cmd_* 函数直接复用）

PORT = 8277
PAGE_FILE = ROOT / "console.html"

# Playwright 会话不能并发（共享 storage_state / 课程上下文），长任务串行执行
EXEC = ThreadPoolExecutor(max_workers=1)
TASKS = {}          # id -> {status, op, started, finished, result, error}
TASKS_LOCK = threading.Lock()

# ---------------------------------------------------------------- 字体
# 界面字体来自本机字体包，best-effort：找不到就由前端 CSS 回退系统字体。
def _font_dirs():
    """可能存放字体包的目录（不写死用户名/盘符，换机器也不会崩）。"""
    dirs = [
        Path.home() / "dsh-raw-html-v2" / "assets" / "fonts",
        Path.home() / "字体",
        Path.home() / "Downloads" / "字体",
    ]
    for drive in ("C:", "D:", "E:"):
        d = Path(drive + "/")
        if d.exists():
            dirs += [d / "字体", d / "字体" / "阿里巴巴普惠体",
                     d / "Windows" / "Fonts"]
    return [d for d in dirs if d.exists()]


def _font_candidates(filename, *fallback_names):
    """在可能目录里找字体文件；找不到返回空列表（前端会回退系统字体）。"""
    names = (filename,) + tuple(fallback_names)
    out = []
    for d in _font_dirs():
        for n in names:
            p = d / n
            if p.exists():
                out.append(p)
    return out


FONT_CANDIDATES = {
    # 界面字体来自本机字体包，best-effort：找不到就由前端 CSS 回退系统字体
    "yuwei.woff2": _font_candidates("Lanxi-鱼尾行书.woff2"),
    "puhui-regular.ttf": _font_candidates("AlibabaPuHuiTi-2-55-Regular.ttf",
                                          "AlibabaPuHuiTi-3-55-Regular.ttf"),
    "puhui-medium.ttf": _font_candidates("AlibabaPuHuiTi-2-65-Medium.ttf",
                                         "AlibabaPuHuiTi-3-65-Medium.ttf"),
    "puhui-bold.ttf": _font_candidates("AlibabaPuHuiTi-2-85-Bold.ttf",
                                       "AlibabaPuHuiTi-3-85-Bold.ttf"),
}
FONT_MIME = {".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf"}


def find_font(key):
    for p in FONT_CANDIDATES.get(key, []):
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------- 工具
def capture_json(fn, *args, **kwargs):
    """调用一个「print JSON」的函数（如 hub.run_list），返回解析后的 dict。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = fn(*args, **kwargs)
    if isinstance(ret, dict):
        return ret
    txt = buf.getvalue().strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        return json.loads(txt[i:j + 1])
    return {"ok": False, "error": f"后端没有输出 JSON：{txt[:200]}"}


def capture_text(fn, *args, **kwargs):
    """调用一个「print 文本并返回退出码」的函数（manual.cmd_*），返回 (code, text)。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn(*args, **kwargs)
    return (code or 0), buf.getvalue().strip()


def load_channel():
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return cfg.get("channel", "未知")
    except Exception:  # noqa: BLE001
        return "未知"


# ---------------------------------------------------------------- 检索结果缓存
# 为什么需要：检索一次全量扫描要 1-2 分钟。若「预览/提前发布」再独立抓一遍，
# 用户就要为同一份数据等两次。所以把最近一次检索的原始结果缓存下来复用。
CACHE_TTL = 300          # 秒；5 分钟内直接复用
_FETCH_CACHE = {"payload": None, "at": 0.0, "days": 0}


def cache_put(payload, days):
    _FETCH_CACHE.update(payload=payload, at=time.time(), days=int(days or 0))


def cache_reject_reason(res):
    """返回「不允许缓存」的原因；可以缓存则返回 None。

    只有各项都健康才允许把结果留在缓存里给「预览/提前发布」用：
      · ok 为真
      · 平台条数 > 0
      · 失败课程数不到总数的一半
    """
    if not isinstance(res, dict):
        return "返回格式异常"
    if not res.get("ok"):
        return f"抓取未成功：{res.get('error') or '未知错误'}"
    if int(res.get("platform_count") or 0) <= 0:
        return "平台 0 条（可能是登录态失效或平台异常），不缓存以免把失败伪装成成功"
    sweep = res.get("sweep") or {}
    total = int(sweep.get("courses_total") or 0)
    failed = len(res.get("warnings", {}).get("failed_courses") or [])
    if total and failed >= max(1, total // 2):
        return f"{failed}/{total} 门课抓取失败（大面积失败），不缓存"
    return None


def cache_get(need_days):
    """命中则返回 (payload, age_sec)，否则 (None, None)。

    两个条件都要满足：
      · 距上次检索不超过 CACHE_TTL
      · 上次检索覆盖的天数 >= 本次需要的天数（否则可能漏掉较远的作业）
    """
    p, at, days = _FETCH_CACHE["payload"], _FETCH_CACHE["at"], _FETCH_CACHE["days"]
    if not p or not at:
        return None, None
    age = time.time() - at
    if age > CACHE_TTL:
        return None, None
    if days < int(need_days or 0):
        return None, None
    return p, age


# ---------------------------------------------------------------- 长任务
def run_task(op, params):
    """长任务统一入口，全部走 hub.py 既有函数，串行执行。"""
    if op == "fetch":
        days = 9999 if params.get("all") else int(params.get("days", 7))
        res = capture_json(hub.run_list, days, True, False, False, False,
                           no_tests=not params.get("tests", True))
        # 只有「真的抓到数据」才允许复用。
        # 教训（2026-10-13 实测）：一次全量扫描失败（0 条 + 18 门课全失败）曾被缓存，
        # 之后点「预览」会飞速复用它 —— 把一次失败伪装成成功，比慢更糟。
        payload = res.get("_payload") if isinstance(res, dict) else None
        reason = cache_reject_reason(res)
        if payload and not reason:
            cache_put(payload, days)
            res["cached_for_reuse"] = True
        else:
            _FETCH_CACHE.update(payload=None, at=0.0, days=0)
            res["cached_for_reuse"] = False
            res["cache_reject_reason"] = reason or "没有可复用的数据"
        return res

    if op in ("preview", "publish"):
        days = int(params.get("days", 7))
        payload, age = cache_get(days)
        return hub.run_publish(days, op == "preview", None,
                               prefetched=payload, prefetched_age_sec=age)
    if op == "content":
        return hub.run_content(str(params.get("hwtid", "")))
    if op == "refresh-courses":
        return hub.run_refresh_courses()
    return {"ok": False, "error": f"未知任务：{op}"}


def _task_worker(task_id, op, params):
    try:
        result = run_task(op, params)
        status = "done"
        error = None
    except Exception as e:  # noqa: BLE001
        result, status, error = None, "error", f"{type(e).__name__}: {e}"
    with TASKS_LOCK:
        TASKS[task_id].update(status=status, result=result, error=error,
                              finished=time.time())


def submit_task(op, params):
    task_id = uuid.uuid4().hex[:8]
    with TASKS_LOCK:
        TASKS[task_id] = {"status": "running", "op": op,
                          "started": time.time(), "finished": None,
                          "result": None, "error": None}
    EXEC.submit(_task_worker, task_id, op, params)
    return task_id


# ---------------------------------------------------------------- 手动条目
def manual_action(action, p):
    """直接复用 manual.py 的 cmd_*（传全字段，不走交互输入）。"""
    if action == "add":
        # 缺字段时 cmd_add 会退回交互式 input()，在服务器进程里会永久卡住，先拦住
        if not (p.get("course", "").strip() and p.get("title", "").strip()
                and p.get("deadline", "").strip()):
            return {"ok": False, "error": "科目、内容、截止时间 都要填。"}
        ns = SimpleNamespace(course=p.get("course", "").strip(),
                             title=p.get("title", "").strip(),
                             deadline=p.get("deadline", "").strip(),
                             note=p.get("note", "").strip())
        code, out = capture_text(manual.cmd_add, ns)
    elif action == "edit":
        ns = SimpleNamespace(id=int(p["id"]), course=p.get("course"),
                             title=p.get("title"), deadline=p.get("deadline"),
                             note=p.get("note"))
        code, out = capture_text(manual.cmd_edit, ns)
    elif action == "done":
        code, out = capture_text(manual.cmd_done, SimpleNamespace(id=int(p["id"])))
    elif action == "rm":
        code, out = capture_text(manual.cmd_rm, SimpleNamespace(id=int(p["id"])))
    elif action == "clean":
        code, out = capture_text(manual.cmd_clean, SimpleNamespace())
    else:
        return {"ok": False, "error": f"未知操作：{action}"}
    # cmd_add 解析失败时把原因打到 stderr，这里捕获不到；用退出码兜底提示
    if code != 0 and not out:
        out = "操作失败（多半是截止时间格式不认识，支持 2026-09-17 23:59 / 9-17 / 9.10 / 9月10日）"
    return {"ok": code == 0, "output": out}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "HomeworkConsole/2.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---- 响应辅助
    def _send(self, code, body, mime="application/json; charset=utf-8", raw=None):
        data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/console.html"):
            if not PAGE_FILE.exists():
                return self._send(404, {"ok": False, "error": "console.html 不存在"})
            return self._send(200, None, "text/html; charset=utf-8",
                              raw=PAGE_FILE.read_bytes())

        if path.startswith("/fonts/"):
            key = path.rsplit("/", 1)[-1]
            fp = find_font(key)
            if not fp:
                return self._send(404, {"ok": False, "error": "font not found"})
            mime = FONT_MIME.get(fp.suffix.lower(), "application/octet-stream")
            return self._send(200, None, mime, raw=fp.read_bytes())

        if path == "/api/config":
            return self._send(200, {"ok": True, "channel": load_channel(),
                                    "port": PORT})

        if path == "/api/list":
            # 不抓平台，只按当前窗口出手动条目（对应旧控制台的 Refresh-View）
            days = 9999 if q.get("all", ["0"])[0] == "1" else int(q.get("days", ["7"])[0])
            return self._send(200, capture_json(hub.run_list, days, False))

        if path == "/api/blacklist":
            kind = q.get("kind", ["course"])[0]
            res = (hub.run_blacklist_list() if kind == "course"
                   else hub.run_test_blacklist_list())
            return self._send(200, res)

        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                t = TASKS.get(task_id)
            if not t:
                return self._send(404, {"ok": False, "error": "任务不存在"})
            return self._send(200, {"ok": True, **t})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = self._body()

        if u.path == "/api/tasks":
            op = p.get("op", "")
            if op not in ("fetch", "preview", "publish", "content", "refresh-courses"):
                return self._send(400, {"ok": False, "error": f"未知任务：{op}"})
            return self._send(200, {"ok": True, "task_id": submit_task(op, p)})

        if u.path == "/api/manual":
            return self._send(200, manual_action(p.get("action", ""), p))

        if u.path == "/api/blacklist":
            kind = p.get("kind", "course")
            action = p.get("action", "")
            lids = [str(x) for x in (p.get("lids") or [])]
            if kind == "course":
                fn = hub.run_blacklist_apply if action == "apply" else hub.run_blacklist_remove
            else:
                fn = hub.run_test_blacklist_apply if action == "apply" else hub.run_test_blacklist_remove
            return self._send(200, fn(lids))

        if u.path == "/api/open":
            url = str(p.get("url", ""))
            if not url.startswith("http"):
                return self._send(400, {"ok": False, "error": "非法链接"})
            os.startfile(url)  # noqa: S606 本机控制台，用系统默认浏览器打开平台页
            return self._send(200, {"ok": True})

        if u.path == "/api/shutdown":
            self._send(200, {"ok": True})
            # 线程池的执行线程是非守护线程，server.shutdown() 之后进程仍退不掉；
            # 延时硬退出即可——本地工具进程，没有需要收尾的状态
            threading.Timer(0.6, os._exit, args=(0,)).start()
            return

        return self._send(404, {"ok": False, "error": "not found"})


def main():
    # 端口已被占用 = 已有实例在跑，安静退出（console.bat 会继续打开浏览器）
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"已有控制台服务在 http://127.0.0.1:{PORT} ，本实例退出。")
            return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"作业提醒 Web 控制台已启动： http://127.0.0.1:{PORT}")
    print("（这个窗口可以最小化；关掉它控制台服务就停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
