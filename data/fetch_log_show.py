# -*- coding: utf-8 -*-
"""查看检索台账。用法：python fetch_log_show.py [条数]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_log  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
rows = fetch_log.recent(n)
if not rows:
    print("（暂无检索记录）")
    sys.exit(0)

print(f"最近 {len(rows)} 条检索记录：\n")
for d in rows:
    flag = "OK " if d.get("ok") else "FAIL"
    days = d.get("days")
    win = "全部" if days == 9999 else f"{days} 天"
    cache = f"  [复用缓存 {d.get('data_age_sec')}s 前的数据]" if d.get("cached") else ""
    print(f"[{flag}] {d.get('time')}  {d.get('source')}  窗口{win}  取数={d.get('mode')}")
    print(f"        平台 {d.get('platform')} 条（含测试 {d.get('tests')}）"
          f" · 手动 {d.get('manual')} 条 · 窗口内 {d.get('rows')} 条"
          f" · 用时 {d.get('elapsed_sec')}s{cache}")
    if d.get("error"):
        print(f"        !! 错误: {str(d['error'])[:120]}")
    if d.get("failed_count"):
        print(f"        !! {d['failed_count']} 门课抓取失败: "
              f"{', '.join(d.get('failed_courses', [])[:5])}")
    if d.get("notes"):
        print(f"        备注: {d['notes']}")
    print()
