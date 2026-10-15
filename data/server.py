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
import argparse
import io
import json
import os
import socket
import subprocess
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
_CACHE_SIDE = {}    # id -> 该任务的原始抓取数据（内部用，不出网）
_LAST_HEARTBEAT = [0.0]   # 页面最后一次心跳时间（列表以便闭包内修改）
HEARTBEAT_GAP = 30.0      # 超过这么久没有心跳 = 页面已关
STARTUP_GRACE = 180.0     # 页面从未连上时的宽限期（避免刚启动就误判）
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
def sanitize_result(obj):
    """剥掉内部字段（下划线开头的键）与不可 JSON 序列化的值。

    背景：_payload 里含 datetime 对象，直接返回给 HTTP 会让 json.dumps 抛
    TypeError，连接被关闭，前端轮询永远失败。内部字段一律不出网。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            out[k] = sanitize_result(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize_result(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # datetime / date / set 等：转成字符串，保证可序列化
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _cache_from_result(res, cache_days):
    """统一写缓存：fetch / preview / publish 三条路径共用。

    缓存天数由各路径给出：
      · fetch（检索）    -> 用户选的窗口天数
      · preview/publish  -> 9999（它们内部是 --deadline-only 全量抓取，
                             覆盖任意窗口，否则用户改天数就会重抓）

    注意：健康检查必须基于 **payload 本身**，不能依赖调用方返回值的字段。
    run_list 返回 platform_count / sweep / warnings，而 run_publish 不返回 ——
    曾经因此把 preview/publish 的结果一律判成「平台 0 条」而拒绝缓存，
    导致「预览后点发布」永远重新抓取。
    """
    payload = res.get("_payload") if isinstance(res, dict) else None
    if payload:
        # 用 payload 的真实数据构造一份等价视图供守卫判断
        view = {
            "ok": True,
            "platform_count": len(payload.get("homeworks") or []) + len(payload.get("tests") or []),
            "sweep": payload.get("sweep") or {},
            "warnings": payload.get("warnings") or {},
        }
    else:
        view = res
    reason = cache_reject_reason(view)
    if payload and not reason:
        cache_put(payload, cache_days)
        res["cached_for_reuse"] = True
        res["cache_days"] = cache_days
    else:
        if not payload:
            # 本次是"复用"而非新抓，不清掉已有缓存
            res["cached_for_reuse"] = False
            res["cache_reject_reason"] = reason or "本次未抓取（复用了缓存）"
            return
        _FETCH_CACHE.update(payload=None, at=0.0, days=0)
        res["cached_for_reuse"] = False
        res["cache_reject_reason"] = reason or "没有可复用的数据"


# 供自检：查看当前缓存状态
def cache_state():
    p, at, days = _FETCH_CACHE["payload"], _FETCH_CACHE["at"], _FETCH_CACHE["days"]
    return {
        "has_payload": bool(p),
        "age_sec": (round(time.time() - at, 1) if at else None),
        "cache_days": days,
        "ttl": CACHE_TTL,
        "homeworks": (len(p.get("homeworks", [])) if p else 0),
        "tests": (len(p.get("tests", [])) if p else 0),
    }


def run_task(op, params):
    """长任务统一入口，全部走 hub.py 既有函数，串行执行。"""
    if op == "fetch":
        days = 9999 if params.get("all") else int(params.get("days", 7))
        res = capture_json(hub.run_list, days, True, False, False, False,
                           no_tests=not params.get("tests", True))
        # 只有「真的抓到数据」才允许复用。
        # 教训（2026-10-13 实测）：一次全量扫描失败（0 条 + 18 门课全失败）曾被缓存，
        # 之后点「预览」会飞速复用它 —— 把一次失败伪装成成功，比慢更糟。
        _cache_from_result(res, days)
        return res

    if op in ("preview", "publish"):
        days = int(params.get("days", 7))
        payload, age = cache_get(days)
        res = hub.run_publish(days, op == "preview", None,
                              prefetched=payload, prefetched_age_sec=age)
        # 关键修复：预览/发布自己抓的数据也要入缓存，
        # 否则「预览 -> 5 分钟内发布」会再抓一遍。
        # 天数传 9999：run_publish 内部是全量抓取，可覆盖任意窗口。
        _cache_from_result(res, 9999)
        return res
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
    # 把内部抓取数据留在服务端（供缓存用），不随 result 出网
    if isinstance(result, dict) and "_payload" in result:
        _CACHE_SIDE[task_id] = result.get("_payload")
        result = {k: v for k, v in result.items() if k != "_payload"}
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
        if raw is not None:
            data = raw
        else:
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as e:
                # 兜底：绝不因为序列化失败而让连接悬空（否则前端会一直轮询不到结果）
                code = 500
                data = json.dumps({"ok": False, "error": f"响应序列化失败：{e}",
                                   "detail": sanitize_result(body)},
                                  ensure_ascii=False, default=str).encode("utf-8")
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

    # ---- 异常日志（定位"连接被关闭"这类问题）
    LOG_FILE = ROOT / "server_error.log"

    def _log_exc(self, where):
        import traceback
        try:
            with open(self.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {where} "
                        f"{self.path} =====\n")
                traceback.print_exc(file=f)
        except OSError:
            pass

    # ---- GET
    def do_GET(self):
        try:
            return self._do_GET()
        except Exception:  # noqa: BLE001
            self._log_exc("GET")

    def _do_GET(self):
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

        if path == "/api/heartbeat":
            _LAST_HEARTBEAT[0] = time.time()
            return self._send(200, {"ok": True})

        if path == "/api/cache":
            return self._send(200, {"ok": True, **cache_state()})

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
            # 内部字段（_payload 等）必须剥掉：它含 datetime，直接序列化会抛错断连
            return self._send(200, {"ok": True,
                                    **{k: v for k, v in t.items() if k != "result"},
                                    "result": sanitize_result(t.get("result"))})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---- POST
    def do_POST(self):
        try:
            return self._do_POST()
        except Exception:  # noqa: BLE001
            self._log_exc("POST")

    def _do_POST(self):
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

        if u.path == "/api/heartbeat":
            _LAST_HEARTBEAT[0] = time.time()
            return self._send(200, {"ok": True})

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


def _pid_alive(pid: int) -> bool:
    """进程是否仍存在（Windows 用 tasklist，跨平台兜底 os.kill）。"""
    if not pid:
        return True
    if os.name == "nt":
        try:
            r = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/nh"],
                               capture_output=True, text=True, timeout=10)
            return str(pid) in (r.stdout or "")
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _watch_browser(pid: int, interval: float = 3.0):
    """守护控制台：浏览器进程消失 **且** 页面心跳也停了，才退出。

    为什么需要两个条件（实测踩过）：
      当 Edge 已有实例在运行时，`--app` 会被交给那个实例处理，
      我们启动的进程随即退出 —— 只看进程号会导致服务刚起就自杀，
      页面打开就是「拒绝连接」。所以必须再确认"页面确实没人用了"。

    心跳由 console.html 每 5 秒上报一次（POST /api/heartbeat）。
    """
    started = time.time()
    print(f"守护控制台：浏览器进程 PID={pid}，并等待页面心跳。")
    while True:
        time.sleep(interval)
        alive = _pid_alive(pid)
        hb = _LAST_HEARTBEAT[0]
        if hb == 0.0:
            # 页面还没连上过：给宽限期。期间即使进程号没了也不退出
            # （因为进程号可能被转交给了已有 Edge 实例）
            if not alive and (time.time() - started) > STARTUP_GRACE:
                print("浏览器进程消失且页面始终未连上，控制台服务退出。")
                os._exit(0)
            continue
        if not alive and (time.time() - hb) > HEARTBEAT_GAP:
            print("浏览器窗口已关闭（且页面心跳停止），控制台服务退出。")
            os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-pid", type=int, default=0,
                    help="守护该进程：它退出时本服务自动退出（传 Edge 的 PID）")
    args = ap.parse_args()

    # 端口已被占用 = 已有实例在跑，安静退出（console.bat 会继续打开浏览器）
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"已有控制台服务在 http://127.0.0.1:{PORT} ，本实例退出。")
            return

    if args.watch_pid:
        threading.Thread(target=_watch_browser, args=(args.watch_pid,),
                         daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"作业提醒 Web 控制台已启动： http://127.0.0.1:{PORT}")
    if args.watch_pid:
        print("（无独立窗口；关闭浏览器窗口即停止服务）")
    else:
        print("（这个窗口可以最小化；关掉它控制台服务就停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
