# -*- coding: utf-8 -*-
"""
手动维护的作业条目 —— 用于老师发在微信群 / 其他企业微信群里、平台抓不到的作业。

数据存在 manual.json，weekly.py 汇总时会把它和平台抓取结果合并。

用法:
    python manual.py add                      # 交互式录入（也可在 console.bat 里手动添加）
    python manual.py add --course "数字电子技术" --title "第3周作业" --deadline "2026-09-17 23:59"
    python manual.py list                     # 查看全部
    python manual.py done 3                   # 标记 id=3 已完成（不再提醒）
    python manual.py rm 3                     # 删除 id=3
    python manual.py clean                    # 清掉 7 天前就已过期的条目

截止时间支持的写法:
    2026-09-17 23:59 | 2026-09-17 | 2026/9/17 23:59 | 9-17 23:59 | 9/17 | 9.10 | 9月10日
    只给日期时，默认当天 23:59 截止。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANUAL_FILE = ROOT / "manual.json"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

DEFAULT_TIME = "23:59"


def load() -> list:
    if not MANUAL_FILE.exists():
        return []
    try:
        data = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def save(items: list) -> None:
    MANUAL_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_deadline(s: str):
    """解析用户输入的截止时间，返回 datetime；解析不了返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    # 短格式（仅月日）转成 %m-%d 再走标准解析，避免重复写一堆月日格式
    # 支持：9.10 / 9.10 23:59 / 9月10日 / 9月10 23:59 / 9月10 / 9/10 / 9-10
    short = s.replace("日", " ").strip()
    short = re.sub(r"(\d{1,2})月(\d{1,2})", r"\1-\2", short).strip()
    short = re.sub(r"^(\d{1,2})\.(\d{1,2})(\s|$)", r"\1-\2\3", short)
    if short != s:
        return parse_deadline(short)
    fmts = (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        "%Y-%m-%d", "%Y/%m/%d",
        "%m-%d %H:%M", "%m/%d %H:%M",
        "%m-%d", "%m/%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        # 只给日期时补 23:59
        if fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
            dt = dt.replace(hour=23, minute=59)
        # 只给月日时补年份；跨年了算明年
        if fmt in ("%m-%d %H:%M", "%m/%d %H:%M", "%m-%d", "%m/%d"):
            now = datetime.now()
            dt = dt.replace(year=now.year)
            if dt < now - timedelta(days=180):
                dt = dt.replace(year=now.year + 1)
        return dt
    return None


def next_id(items: list) -> int:
    return max((int(i.get("id", 0)) for i in items), default=0) + 1


def fmt_left(dt: datetime) -> str:
    delta = dt - datetime.now()
    if delta.total_seconds() < 0:
        return "已逾期"
    d, h = delta.days, delta.seconds // 3600
    return f"还剩 {d} 天 {h} 小时"


# ------------------------------------------------------------------ 子命令
def cmd_add(args) -> int:
    items = load()
    if args.course and args.title and args.deadline:
        course, title, raw_dl = args.course, args.title, args.deadline
        note = args.note or ""
    else:
        print("手动录入作业（直接回车可随时中止）")
        try:
            course = input("科目名：").strip()
            if not course:
                print("已取消。")
                return 1
            title = input("作业内容：").strip()
            if not title:
                print("已取消。")
                return 1
            raw_dl = input(
                f"截止时间（如 2026-09-17 23:59，只填日期默认 {DEFAULT_TIME}）："
            ).strip()
            note = input("备注（可留空）：").strip()
        except EOFError:
            # 没有可交互的 stdin（例如被管道/重定向启动，或双击窗口异常）时优雅退出
            print("已取消（未读取到输入）。")
            return 1

    dt = parse_deadline(raw_dl)
    if dt is None:
        print(f"ERROR: 看不懂的截止时间：{raw_dl}", file=sys.stderr)
        print("支持：2026-09-17 23:59 / 2026-09-17 / 9-17 23:59 / 9/17", file=sys.stderr)
        return 1

    item = {
        "id": next_id(items),
        "course": course,
        "title": title,
        "deadline": dt.strftime("%Y-%m-%d %H:%M"),
        "note": note,
        "done": False,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    items.append(item)
    save(items)
    print(f"已添加 [id={item['id']}] {course} | {title} | 截止 {item['deadline']}")
    return 0


def cmd_list(_args) -> int:
    items = load()
    if not items:
        print("manual.json 里还没有手动作业。用 `python manual.py add` 添加。")
        return 0
    items.sort(key=lambda x: x.get("deadline", ""))
    print(f"共 {len(items)} 条：")
    for it in items:
        dt = parse_deadline(it.get("deadline", ""))
        if it.get("done"):
            status = "已完成"
            tail = ""
        elif dt is None:
            status = "时间无效"
            tail = ""
        else:
            status = fmt_left(dt)
            tail = " [已过期]" if dt < datetime.now() else ""
        note = f"  ({it['note']})" if it.get("note") else ""
        print(
            f"[{it['id']}] {it['course']} | {it['title']} | "
            f"截止 {it.get('deadline', '?')} | {status}{tail}{note}"
        )
    return 0


def cmd_done(args) -> int:
    items = load()
    for it in items:
        if int(it.get("id", -1)) == args.id:
            it["done"] = True
            save(items)
            print(f"已标记完成：{it['course']} | {it['title']}")
            return 0
    print(f"ERROR: 找不到 id={args.id}", file=sys.stderr)
    return 1


def cmd_rm(args) -> int:
    items = load()
    rest = [i for i in items if int(i.get("id", -1)) != args.id]
    if len(rest) == len(items):
        print(f"ERROR: 找不到 id={args.id}", file=sys.stderr)
        return 1
    save(rest)
    print(f"已删除 id={args.id}")
    return 0


def cmd_edit(args) -> int:
    items = load()
    target = None
    for it in items:
        if int(it.get("id", -1)) == args.id:
            target = it
            break
    if target is None:
        print(f"ERROR: 找不到 id={args.id}", file=sys.stderr)
        return 1
    if args.course is not None:
        target["course"] = args.course
    if args.title is not None:
        target["title"] = args.title
    if args.deadline is not None:
        if not args.deadline.strip():
            target["deadline"] = ""
        else:
            dt = parse_deadline(args.deadline)
            if dt is None:
                print(f"ERROR: 看不懂的截止时间：{args.deadline}", file=sys.stderr)
                print("支持：2026-09-17 23:59 / 2026-09-17 / 9-17 23:59 / 9/17", file=sys.stderr)
                return 1
            target["deadline"] = dt.strftime("%Y-%m-%d %H:%M")
    if args.note is not None:
        target["note"] = args.note
    save(items)
    print(f"已更新 [id={target['id']}] {target['course']} | {target['title']} | 截止 {target.get('deadline', '')}")
    return 0


def cmd_clean(_args) -> int:
    items = load()
    cutoff = datetime.now() - timedelta(days=7)
    kept = []
    removed = 0
    for it in items:
        dt = parse_deadline(it.get("deadline", ""))
        if dt is not None and dt < cutoff:
            removed += 1
            continue
        kept.append(it)
    save(kept)
    print(f"已清理 {removed} 条过期作业，剩余 {len(kept)} 条。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="添加一条作业")
    p_add.add_argument("--course")
    p_add.add_argument("--title")
    p_add.add_argument("--deadline")
    p_add.add_argument("--note")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="列出全部")
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="标记某条已完成")
    p_done.add_argument("id", type=int)
    p_done.set_defaults(func=cmd_done)

    p_rm = sub.add_parser("rm", help="删除某条")
    p_rm.add_argument("id", type=int)
    p_rm.set_defaults(func=cmd_rm)

    p_edit = sub.add_parser("edit", help="修改某条（传哪些字段改哪些）")
    p_edit.add_argument("id", type=int)
    p_edit.add_argument("--course")
    p_edit.add_argument("--title")
    p_edit.add_argument("--deadline", help="留空字符串可清除截止时间")
    p_edit.add_argument("--note")
    p_edit.set_defaults(func=cmd_edit)

    p_clean = sub.add_parser("clean", help="清理过期条目")
    p_clean.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
