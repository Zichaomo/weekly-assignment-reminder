# -*- coding: utf-8 -*-
"""
本地 Web 控制台后端（data/server.py）

只做 I/O 接缝：
  - 静态托管 console.html 与字体文件
  - 把 hub.py / manual.py 的既有函数包成 HTTP JSON API
  - 长任务（检索/发布/取正文/刷新课程表）放后台线程，前端轮询进度
  - 列表与正文以 SQLite 缓存为主路径；冷打开不访问学校平台

用法：双击 console.bat（或手动 `python server.py`），浏览器打开 http://127.0.0.1:8277
端口被占用时直接退出（视为已有实例在跑，console.bat 会继续打开浏览器）。
"""
import argparse
import hashlib
import io
import json
import os
import re
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
from urllib.parse import urlparse, parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import hub      # noqa: E402  控制台后端：列表/发布/黑名单/正文/课程表
import manual   # noqa: E402  手动条目增删改查（cmd_* 函数直接复用）
import cache_store  # noqa: E402
import content_pipeline  # noqa: E402

PORT = int(os.environ.get("HOMEWORK_CONSOLE_PORT", "8277"))
PAGE_FILE = ROOT / "console.html"

# Playwright 会话不能并发（共享 storage_state / 课程上下文），长任务串行执行
EXEC = ThreadPoolExecutor(max_workers=1)
TASKS = {}          # id -> {status, op, started, finished, result, error}
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


# ---------------------------------------------------------------- 缓存状态
def cache_state():
    """Return local coverage/counts without touching the platform."""
    try:
        import scrape
        index = scrape.load_course_index()
        blocked = scrape.blacklisted_lids()
        expected = [str(x) for x in index if str(x) not in blocked]
        state = cache_store.latest_state()
        tests_requested = bool((state.get("scan") or {}).get("tests_requested", True))
        cov = cache_store.coverage(
            expected_lids=expected, tests_requested=tests_requested,
            tests_blocked_lids=scrape.test_blacklisted_lids(),
            current_index_version=cache_store.version_hash(index) if index else "",
            max_age_sec=cache_store.cache_settings()["publish_fresh_sec"])
        items = cache_store.load_items()
        return {"available": bool(items), "item_count": len(items),
                "content_ready": sum(1 for x in items if x.get("content_ready")),
                "content_failed": sum(1 for x in items if x.get("content_error")),
                "coverage": cov}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "item_count": 0,
                "error": f"{type(exc).__name__}: {exc}"}


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


def run_task(op, params):
    """长任务统一入口，全部走 hub.py 既有函数，串行执行。"""
    if op == "fetch":
        days = 9999 if params.get("all") else int(params.get("days", 7))
        return capture_json(
            hub.run_list, days, True, False, False, False,
            no_tests=not params.get("tests", True),
            include_expired=bool(params.get("all")), lids=params.get("lids") or None,
            scan_id=params.get("_task_id"))

    if op in ("preview", "publish"):
        days = int(params.get("days", 7))
        return hub.run_publish(days, op == "preview", None)
    if op == "content":
        return hub.run_content(
            str(params.get("hwtid", "")),
            item_keys=params.get("item_keys") or None,
            hwtids=params.get("hwtids") or None,
            force=bool(params.get("force")),
            limit=params.get("limit"))
    if op == "refresh-courses":
        return hub.run_refresh_courses()
    return {"ok": False, "error": f"未知任务：{op}"}


def _task_signature(op, params):
    norm = {}
    if op == "fetch":
        norm = {"days": int(params.get("days", 7)), "all": bool(params.get("all")),
                "tests": bool(params.get("tests", True)),
                "lids": sorted(str(x) for x in (params.get("lids") or []))}
    elif op in ("preview", "publish"):
        norm = {"days": int(params.get("days", 7))}
    elif op == "content":
        norm = {"item_keys": sorted(str(x) for x in (params.get("item_keys") or [])),
                "hwtids": sorted(str(x) for x in (params.get("hwtids") or [])),
                "hwtid": str(params.get("hwtid") or ""),
                "force": bool(params.get("force"))}
    else:
        norm = {str(k): v for k, v in params.items() if not str(k).startswith("_")}
    return f"{cache_store.account_scope()}|{op}|{cache_store.version_hash(norm)}"


def _task_status(result):
    if not isinstance(result, dict):
        return "error"
    if result.get("ok"):
        cov = result.get("coverage") or {}
        if (cov and not cov.get("publishable")) or result.get("failed") or result.get("partial"):
            return "partial"
        return "done"
    if result.get("failed") or result.get("cache_blocked"):
        return "partial" if result.get("failed") else "error"
    return "error"


def _task_worker(task_id, op, params):
    try:
        with TASKS_LOCK:
            if task_id in TASKS:
                TASKS[task_id]["status"] = "running"
        cache_store.update_task(task_id, "running",
                                scan_id=(task_id if op == "fetch" else None))
        result = run_task(op, params)
        status = _task_status(result)
        error = (result.get("error") if isinstance(result, dict) else None)
    except Exception as e:  # noqa: BLE001
        result, status, error = None, "error", f"{type(e).__name__}: {e}"
    scan_id = None
    if isinstance(result, dict):
        scan_id = result.get("scan_id")
        if not scan_id:
            scan_id = ((result.get("coverage") or {}).get("scan") or {}).get("scan_id")
    try:
        cache_store.update_task(task_id, status,
                                result_summary={"scan_id": scan_id, "status": status},
                                scan_id=scan_id)
    except Exception:  # noqa: BLE001
        pass
    with TASKS_LOCK:
        TASKS[task_id].update(status=status, result=result, error=error,
                              finished=time.time(), scan_id=scan_id)


def submit_task(op, params):
    task_id = uuid.uuid4().hex[:12]
    params = dict(params or {})
    params["_task_id"] = task_id
    signature = _task_signature(op, params)
    task_id, deduped = cache_store.register_task(task_id, signature, op, params)
    if deduped:
        return task_id, True
    with TASKS_LOCK:
        TASKS[task_id] = {"status": "queued", "op": op, "params": params,
                          "signature": signature,
                          "scan_id": task_id if op == "fetch" else None,
                          "started": time.time(), "finished": None,
                          "result": None, "error": None}
    EXEC.submit(_task_worker, task_id, op, params)
    return task_id, False


def task_progress(task: dict) -> dict | None:
    """Progress may only come from this task's own scan id."""
    scan_id = task.get("scan_id")
    task_id = (task.get('params') or {}).get('_task_id')
    if task_id and task.get('status') in ('queued', 'running'):
        record = cache_store.task_record(task_id)
        if record and record.get('scan_id'):
            scan_id = record['scan_id']
    if not scan_id:
        return None
    progress = cache_store.scan_progress(scan_id)
    return None if progress.get("status") == "none" else progress


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
    def _send(self, code, body, mime="application/json; charset=utf-8", raw=None,
              headers=None):
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
        for key, value in (headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        if n > 2_000_000:
            raise ValueError("请求体过大")
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
        except Exception as exc:  # noqa: BLE001
            self._log_exc("GET")
            try:
                self._send(500, {"ok": False, "error": f"服务端错误：{type(exc).__name__}"})
            except Exception:  # noqa: BLE001
                pass

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
                                    "port": PORT,
                                    "content_ttl_sec": cache_store.cache_settings()["content_ttl_sec"],
                                    "publish_fresh_sec": cache_store.cache_settings()["publish_fresh_sec"]})

        if path == "/api/list":
            # Local-only: this route must never touch the platform, even cold.
            try:
                days = max(1, min(9999, int(q.get("days", ["7"])[0])))
            except (TypeError, ValueError):
                days = 7
            include_expired = (q.get("all", ["0"])[0] == "1"
                               or q.get("include_expired", ["0"])[0] == "1")
            t0 = time.perf_counter()
            res = capture_json(hub.run_list, days, False,
                               include_expired=include_expired)
            res["first_screen_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return self._send(200, res)

        if path.startswith("/api/assignments/") and path.endswith("/content"):
            key = unquote(path[len("/api/assignments/"):-len("/content")])
            item = cache_store.get_item(key)
            if not item:
                return self._send(404, {"ok": False, "error": "作业条目不存在"})
            return self._send(200, {
                "ok": True, "item_key": item["item_key"],
                "status": item.get("content_status") or "not_ready",
                "ready": bool(item.get("content_ready")),
                "stale": bool(item.get("content_stale")),
                "error": item.get("content_error"),
                "checked_at": item.get("content_checked_at"),
                "source_version": item.get("source_version"),
                "revision": item.get("content_revision"),
                "blocks": item.get("content_blocks") or [],
                "asset_ids": item.get("asset_ids") or [],
                "text": item.get("content_text") or "",
                "summary": item.get("content_summary"),
                "review_stale": bool(item.get("content_review_stale")),
                "view": item.get("view"), "stale_view": item.get("stale_view"),
            })

        if path.startswith("/api/assets/"):
            asset_id = path.rsplit("/", 1)[-1].lower()
            if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
                return self._send(400, {"ok": False, "error": "非法资产 ID"})
            found = content_pipeline.asset_path(asset_id)
            if not found:
                return self._send(404, {"ok": False, "error": "资产不存在"})
            fp, record = found
            mime = str(record.get("mime") or "")
            if mime not in content_pipeline.IMAGE_MIME:
                return self._send(415, {"ok": False, "error": "资产类型不允许"})
            return self._send(200, None, mime, raw=fp.read_bytes(), headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            })

        if path == "/api/views/export":
            return self._send(200, cache_store.export_views())

        if path == "/api/courses":
            # 课程目录：选修课联想补全 / 全部课程清单 / 黑名单候选都读它
            return self._send(200, hub.run_courses())

        if path == "/api/blacklist":
            kind = q.get("kind", ["course"])[0]
            if kind == "course":
                res = hub.run_blacklist_list()
            elif kind == "test":
                res = hub.run_test_blacklist_list()
            else:
                # kind=elective：选修课独立列表（照抓、只发个人小群）
                res = hub.run_elective_list()
            return self._send(200, res)

        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                t = dict(TASKS[task_id]) if task_id in TASKS else None
            if not t:
                rec = cache_store.task_record(task_id)
                if not rec:
                    return self._send(404, {"ok": False, "error": "任务不存在"})
                t = {"status": rec["status"], "op": rec["op"],
                     "started": rec["created_at"], "finished": rec["finished_at"],
                     "scan_id": rec.get("scan_id"),
                     "result": None, "error": rec.get("result_summary")}
            progress = task_progress(t)
            # 内部字段（_payload 等）必须剥掉：它含 datetime，直接序列化会抛错断连
            return self._send(200, {"ok": True,
                                    **{k: v for k, v in t.items() if k != "result"},
                                    "result": sanitize_result(t.get("result")),
                                    "progress": progress})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---- POST
    def do_POST(self):
        try:
            return self._do_POST()
        except Exception as exc:  # noqa: BLE001
            self._log_exc("POST")
            try:
                self._send(400 if isinstance(exc, ValueError) else 500,
                           {"ok": False, "error": str(exc) if isinstance(exc, ValueError)
                            else f"服务端错误：{type(exc).__name__}"})
            except Exception:  # noqa: BLE001
                pass

    def _do_POST(self):
        u = urlparse(self.path)
        p = self._body()

        if u.path == "/api/tasks":
            op = p.get("op", "")
            if op not in ("fetch", "preview", "publish", "content", "refresh-courses"):
                return self._send(400, {"ok": False, "error": f"未知任务：{op}"})
            keys = p.get("item_keys") or []
            ids = p.get("hwtids") or []
            if not isinstance(keys, list) or not isinstance(ids, list) \
                    or len(keys) > 100 or len(ids) > 100:
                return self._send(400, {"ok": False, "error": "批量正文参数过多或格式错误"})
            task_id, deduped = submit_task(op, p)
            return self._send(200, {"ok": True, "task_id": task_id,
                                    "deduped": deduped,
                                    "status": "already-active" if deduped else "queued"})

        if u.path.startswith("/api/assignments/") and u.path.endswith("/review"):
            key = unquote(u.path[len("/api/assignments/"):-len("/review")])
            try:
                cache_store.save_content_review(key,
                    expected_version=p.get('expected_version'), mode=p.get('mode'),
                    text=p.get('text', ''))
            except cache_store.VersionConflict as exc:
                return self._send(409, {'ok': False, 'error': str(exc), 'code': 'version_conflict'})
            return self._send(200, {'ok': True})

        if u.path.startswith("/api/assignments/") and u.path.endswith("/view"):
            key = unquote(u.path[len("/api/assignments/"):-len("/view")])
            expected = str(p.get("expected_version") or "")
            if not expected:
                return self._send(400, {"ok": False, "error": "缺少 expected_version"})
            try:
                res = cache_store.save_view(
                    key, expected_version=expected,
                    regions=p.get("regions") or [],
                    folded_block_ids=p.get("folded_block_ids") or [],
                    summary=str(p.get("summary") or ""),
                    review_status=str(p.get("review_status") or "ok"))
            except cache_store.VersionConflict as exc:
                return self._send(409, {"ok": False, "error": str(exc),
                                        "code": "version_conflict"})
            except ValueError as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            return self._send(200, {"ok": True, **res})

        if u.path == "/api/manual":
            return self._send(200, manual_action(p.get("action", ""), p))

        if u.path == "/api/blacklist":
            kind = p.get("kind", "course")
            action = p.get("action", "")
            lids = [str(x) for x in (p.get("lids") or [])]
            if kind == "elective":
                # 选修课独立入口：apply=加入（可给 lid 或课程名片段）/ remove=取消标记
                if action == "apply":
                    return self._send(200, hub.run_blacklist_elective(lids))
                return self._send(200, hub.run_elective_remove(lids))
            if kind == "course" and action == "elective":
                # 兼容旧调用（选修课原本挂在课程黑名单下）
                return self._send(200, hub.run_blacklist_elective(lids))
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
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") \
                    or not content_pipeline.host_allowed(url):
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

    try:
        cache_store.init_db()
        cache_store.recover_orphan_tasks()
    except Exception as exc:  # noqa: BLE001
        print(f"警告：任务注册表恢复失败：{type(exc).__name__}: {exc}")
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
