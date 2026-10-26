# -*- coding: utf-8 -*-
"""检索台账（fetch_log.jsonl）—— 记录每次「检索/抓取」的真实结果。

为什么需要（用户要求，且我自己踩过）：
  · 排查「数据从哪来」时只能靠猜：这次抓的？复用的缓存？缓存多旧？
  · 曾出现「候选突然消失」，因为分不清是抓取失败还是数据被覆盖
  · 推送时报「平台 0 条」却看不出是抓取失败还是真的没作业

推送已有 push_log.jsonl；这里补上「抓取侧」的账，两者互补：
  fetch_log.jsonl  这次抓了什么（什么时候、多少条、多久、失败几门、是否复用缓存）
  push_log.jsonl   这次发了什么（发到哪、成功与否、内容哈希）

查看方式：python fetch_log_show.py [条数]

设计要点：
  · 只追加，一行一条 JSON；写入失败不影响主流程（仅告警）
  · 只记排查所需的最小集，不落大 payload（避免文件膨胀）
  · 本文件是【模块】，供 hub.py / weekly.py 调用；查看用 fetch_log_show.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FETCH_LOG = ROOT / "fetch_log.jsonl"


def log_fetch(source: str, days, *, mode: str = "", rows: int = None,
              rows_total: int = None, platform: int = None, tests: int = None,
              manual: int = None, elapsed: float = None, cached: bool = False,
              data_age: float = None, ok: bool = True, error: str = "",
              failed_courses: list = None, notes: str = "",
              cache_hit: bool = None, first_screen_ms: float = None,
              content_requested: int = None, content_ok: int = None,
              content_failed: int = None, task_id: str = "") -> None:
    """追加一条检索记录。

    source      调用来源：hub-list（检索按钮）/ hub-preview / hub-publish / weekly
    days        统计窗口天数（9999 表示"全部"）
    mode        取数口径，如 deadline-only / pool
    rows        进入最终窗口的条数；rows_total 平台原始条数
    cached      本次是否复用了缓存（True 时 elapsed 通常很小）
    data_age    复用的数据距现在多少秒
    """
    rec = {
        "ts": round(time.time(), 3),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "unknown",
        "days": days,
        "mode": mode,
        "ok": bool(ok),
        "error": (error or "")[:300],
        "platform": platform,
        "tests": tests,
        "manual": manual,
        "rows": rows,
        "rows_total": rows_total,
        "elapsed_sec": (round(elapsed, 1) if isinstance(elapsed, (int, float)) else None),
        "cached": bool(cached),
        "data_age_sec": (round(data_age, 1) if isinstance(data_age, (int, float)) else None),
        "cache_hit": (bool(cached) if cache_hit is None else bool(cache_hit)),
        "first_screen_ms": (round(first_screen_ms, 1)
                            if isinstance(first_screen_ms, (int, float)) else None),
        "content_requested": content_requested,
        "content_ok": content_ok,
        "content_failed": content_failed,
        "task_id": (task_id or "")[:32],
        "failed_count": (len(failed_courses) if failed_courses else 0),
        "failed_courses": (list(failed_courses)[:12] if failed_courses else []),
        "notes": (notes or "")[:200],
        "pid": os.getpid(),
    }
    try:
        with FETCH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写检索台账失败 {e}", file=sys.stderr)


def recent(n: int = 10) -> list:
    """读最近 n 条（供控制台/命令行查看）。"""
    if not FETCH_LOG.exists():
        return []
    rows = []
    for line in FETCH_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows[-n:]
