# -*- coding: utf-8 -*-
"""
端到端入口：抓取北化在线作业 -> 合并手动录入 -> 汇总 -> 推送。

用法:
    python weekly.py                       # 抓取 + 汇总 + 推送（未来 7 天）
    python weekly.py --days 3              # 只看未来 3 天
    python weekly.py --no-send             # 演练：只打印/落盘，不推送
    python weekly.py --channel pushplus    # 临时指定推送渠道（覆盖 config.json）
    python weekly.py --check-freshness     # 只检查「该跑没跑」（定时任务的前置检查）
    python weekly.py --check-freshness --alert-if-stale   # 陈旧则发一条告警

手动补录（老师发在微信群/其他企业微信群、平台抓不到的）：
    见 manual.py —— python manual.py add

失败告警：见 alert.py。默认走本机通知（不发任何群），配置在 config.json 的 alert 段。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import cache_store

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# 子进程统一按 utf-8 输出，避免 Windows 下 GBK 中文把父进程解码搞崩。
UTF8_ENV = os.environ.copy()
UTF8_ENV["PYTHONIOENCODING"] = "utf-8"

# 连续多少比例的门课抓取失败算「覆盖严重不完整」（与 server.py 的缓存守卫同一口径）
MAX_FAIL_RATIO = 0.5


def fetch(extra_args: list = None) -> dict:
    """调用 scrape.py 抓取，返回结构化 dict。

    extra_args: 追加给 scrape.py 的命令行参数，例如 ["--with-content"] 表示连作业正文一起抓。
    抓取耗时写进返回值的 _elapsed_sec，方便上层显示「本次抓取用时」。
    """
    cmd = [PY, str(ROOT / "scrape.py")] + list(extra_args or [])
    t0 = time.time()
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=UTF8_ENV,
        cwd=str(ROOT),
    )
    elapsed = time.time() - t0
    if out.returncode != 0:
        raise RuntimeError(f"抓取失败（耗时 {elapsed:.0f} 秒）:\n{out.stdout}\n{out.stderr}")
    if not out.stdout:
        raise RuntimeError(f"抓取无输出（耗时 {elapsed:.0f} 秒）:\n{out.stderr}")
    data = json.loads(out.stdout)
    if not data.get("ok"):
        # scrape.py 在「未登录」等情况下会输出 {"ok": false} 且退出码为 0。
        # 不在这里拦住，它就会被当成「成功的空结果」——那正是漏报的开始。
        raise RuntimeError("抓取未成功：" + (out.stderr or "").strip()[-400:])
    data["_elapsed_sec"] = round(elapsed, 1)
    data["_stderr_tail"] = (out.stderr or "")[-4000:]
    return data


def load_manual() -> list:
    """读取手动录入的作业（老师发在微信群等平台抓不到的）。"""
    path = ROOT / "manual.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        if it.get("done"):
            continue  # 已完成的不再提醒
        out.append({
            "course": it.get("course", "未命名课程"),
            "title": it.get("title", "作业"),
            "deadline": it.get("deadline", ""),
            "note": it.get("note", ""),
            "source": "manual",
        })
    return out


def parse_deadline(s: str):
    """解析截止时间字符串，返回 datetime；解析不了返回 None。

    支持：2026-09-17 23:59 / 2026-09-17 / 2026/9/17 23:59 / 9-17 23:59 / 9/17 / 9.10 / 9月10日
    只给日期时默认当天 23:59。
    """
    s = (s or "").strip()
    if not s:
        return None
    # 短格式（仅月日）转成 %m-%d 再走标准解析，避免重复写一堆月日格式
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
        if fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
            dt = dt.replace(hour=23, minute=59)
        if fmt in ("%m-%d %H:%M", "%m/%d %H:%M", "%m-%d", "%m/%d"):
            now = datetime.now()
            dt = dt.replace(year=now.year)
            if dt < now - timedelta(days=180):
                dt = dt.replace(year=now.year + 1)
        return dt
    return None


def merge_and_filter(data: dict, manual: list, days: int):
    """合并平台抓取（作业 + 在线测试）与手动录入，返回截止时间窗口内的条目。"""
    all_hw = list(data.get("homeworks", []))
    for t in data.get("tests", []) or []:      # 在线测试与作业同一套截止规则
        t.setdefault("kind", "test")
        all_hw.append(t)
    for m in manual:
        m.setdefault("source", "manual")
        m.setdefault("kind", "manual")
        all_hw.append(m)

    now = datetime.now()
    end = now + timedelta(days=days)
    picked = []
    for hw in all_hw:
        dt = parse_deadline(hw.get("deadline", ""))
        hw["_deadline_dt"] = dt
        if dt is None:
            # 截止时间无法解析：手动条目保留（标「未标注」），平台条目丢弃
            if hw.get("source") == "manual":
                picked.append(hw)
            continue
        # 平台作业和手动作业一视同仁：只保留时间窗口内的，过期自动掉出
        if now <= dt <= end:
            picked.append(hw)
    return picked


def format_message(items: list, days: int, title: str = None,
                   elective_lids=None) -> str:
    """推送文案（B 方案：紧迫感分级）。

    - 头部一行：覆盖窗口，不带「生成时间」（对同学是噪音）
    - 按「近两天 / 本周稍后」分组，组为空则整组消失
    - 倒计时精度：今天/明天直接说「今天/明天 HH:mm 截止」；
      2 天内带小时，超过 2 天只报天数（没有「0 小时」「5 天 10 小时」这类精度表演）

    title / elective_lids 是 2026-10-18 加的：同一次运行要产出两份文案 ——
      · 班级群那条：去掉选修课（标题不变）
      · 个人小群那条：完整清单，选修课带 🎓 标记，标题「我的作业提醒（含选修课）」
    """
    now = datetime.now()
    end = now + timedelta(days=days)
    head = title or "📚 作业提醒"
    lines = [f"{head} · 未来 {days} 天（至 {end:%m-%d}）", ""]
    if not items:
        lines.append("✅ 这段时间没有待交作业，安心休息。")
        return "\n".join(lines)

    elec = {str(x) for x in (elective_lids or set())}

    def mark_of(it) -> str:
        src = it.get("source") or "platform"
        kind = it.get("kind") or ("manual" if src == "manual" else "homework")
        if src == "manual":
            return " 📌手动"
        if str(it.get("lid") or "") in elec:
            # 只会在个人那条里出现（班级那条已经把选修课删掉了）
            return " 🎓选修" + (" 📝在线测试" if kind == "test" else "")
        return " 📝在线测试" if kind == "test" else ""

    def deadline_line(it) -> str:
        dt = it.get("_deadline_dt")
        if not dt:
            return f"   ⏰ 截止 {it.get('deadline') or '未标注'}"
        delta = dt - now
        if delta.total_seconds() < 0:
            return f"   ⏰ {dt:%m-%d %H:%M} 截止（已逾期）"
        if dt.date() == now.date():
            return f"   ⏰ 今天 {dt:%H:%M} 截止"
        if dt.date() == (now + timedelta(days=1)).date():
            return f"   ⏰ 明天 {dt:%H:%M} 截止"
        d, h = delta.days, delta.seconds // 3600
        left = f"还剩 {d} 天" if (d > 2 or h == 0) else f"还剩 {d} 天 {h} 小时"
        return f"   ⏰ {dt:%m-%d %H:%M} 截止（{left}）"

    def block(it) -> list:
        blk = [f"• {it.get('course', '未命名课程')}{mark_of(it)}｜{it.get('title', '作业')}",
               deadline_line(it)]
        if it.get("note"):
            blk.append(f"   备注：{it['note']}")
        return blk

    picked = sorted(items, key=lambda x: (x.get("_deadline_dt") or datetime.max))
    near, later = [], []
    for it in picked:
        dt = it.get("_deadline_dt")
        (near if (dt is not None and (dt - now).days <= 2) else later).append(it)

    if near:
        lines.append("⚠️ 近两天截止")
        for it in near:
            lines.extend(block(it))
        lines.append("")
    if later:
        lines.append("🗓 本周稍后")
        for it in later:
            lines.extend(block(it))
        lines.append("")
    lines.append(f"—— 共 {len(picked)} 项，详情以「北化在线」为准")
    return "\n".join(lines).rstrip()


def ensure_login() -> bool:
    """抓取前保证有有效登录态。

    1. 先快速检查 storage_state；
    2. 失效 -> 调 `login.py --no-wait` 自动登录（Edge 自动填充 + 自动点登录，最多等 45 秒，
       不转人工，免得后台挂死）。实测本机 IP 不弹滑块，所以这一步通常能自愈；
    3. 仍失败 -> 返回 False，由调用方发告警让人工双击 login.bat。
    """
    r = subprocess.run(
        [PY, str(ROOT / "login.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV, cwd=str(ROOT),
    )
    if "LOGGED_IN" in (r.stdout or ""):
        return True

    print("登录态失效，尝试自动登录（最多 45 秒）...", file=sys.stderr)
    r2 = subprocess.run(
        [PY, str(ROOT / "login.py"), "--no-wait"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=UTF8_ENV, cwd=str(ROOT),
    )
    for line in (r2.stdout or "").splitlines():
        print("  " + line, file=sys.stderr)
    if r2.returncode == 0:
        print("自动登录成功，继续抓取。", file=sys.stderr)
        return True

    print("自动登录未成功，需人工双击 login.bat 重新登录。", file=sys.stderr)
    return False


def load_fresh_publish_cache():
    """Return (payload, coverage) only for a strict, complete 5-minute cache."""
    try:
        import scrape
        index = scrape.load_course_index()
        blocked = scrape.blacklisted_lids()
        expected = [str(lid) for lid in index if str(lid) not in blocked]
        index_version = cache_store.version_hash(index) if index else ""
        tests_blocked = scrape.test_blacklisted_lids()
        settings = cache_store.cache_settings()
        cov = cache_store.coverage(
            expected_lids=expected, tests_requested=True,
            tests_blocked_lids=tests_blocked,
            current_index_version=index_version,
            max_age_sec=settings["publish_fresh_sec"])
        if not cov.get("publishable"):
            return None, cov
        items = cache_store.load_items(
            exclude_lids=blocked,
            exclude_test_lids=scrape.test_blacklisted_lids())
        homeworks = [x for x in items if x.get("kind") != "test"]
        tests = [x for x in items if x.get("kind") == "test"]
        payload = {
            "ok": True, "homeworks": homeworks, "tests": tests,
            "per_course": [],
            "warnings": {"failed_courses": list(cov.get("failed_courses") or []),
                         "zero_homework_courses": []},
            "sweep": {"courses_total": cov.get("expected_courses", 0)},
            "_from_cache": True, "_cache_age_sec": cov.get("age_sec") or 0,
            "_cache_updated_at": cov.get("updated_at"),
            "_elapsed_sec": 0,
        }
        return payload, cov
    except Exception as exc:  # noqa: BLE001
        return None, {"publishable": False, "publish_block_reasons": [
            f"读取缓存失败：{type(exc).__name__}: {exc}"]}


# ---------------------------------------------------------------- 失败告警
def send_alert(kind: str, title: str, detail: str, level: str = "error",
               no_dedup: bool = False) -> bool:
    """发一条失败告警（实现在 alert.py：渠道选择、冷却去重、台账都在那边）。

    为什么不再是「直接调 notify.py 发 wecom」：
      作业提醒是发给**全班**的，告警是发给**自己**的 —— 两者必须分开配。
      旧实现会把技术性文字（"自动登录失败"/"抓取异常"）直接发到班级群。
      现在默认走本机通知，要用企业微信必须显式配 alert.channel=wecom_test。

    告警是旁路：它自己出任何问题都不该影响主流程，所以这里一律兜住异常。
    """
    try:
        import alert  # 延迟导入：没有告警需求时不影响启动
        return alert.send(kind, title, detail, level=level, no_dedup=no_dedup)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 告警发送失败（已忽略）{type(e).__name__}: {e}", file=sys.stderr)
        return False


def check_freshness() -> int:
    """检查「自动任务该跑没跑」，返回 0=正常 / 2=陈旧 / 1=检查本身失败。

    存在的理由：本工具的失败是**静默**的 —— 群里没消息与「这周没作业」长得一样。
    定时机制落地后，这条检查可以挂在计划任务或「登录时补跑」上，
    也可以手动跑它来确认自动化到底有没有在工作。

    只认 source=weekly 且成功的台账记录：手动点「提前发布」写的是 hub-*，
    把它算成功就永远查不出「定时任务其实从没跑过」。
    """
    try:
        import alert
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: 无法导入 alert.py：{e}", file=sys.stderr)
        return 1
    cfg = alert.load_cfg()
    fresh, why, ev = alert.check_freshness(cfg)
    print(("⚠️ " if fresh else "✅ ") + why)
    print("证据：" + json.dumps(ev, ensure_ascii=False))
    print("（只认 source=weekly 且成功的台账；手动点「提前发布」不算）")
    if fresh:
        print(r"提示：定时任务尚未生效时，可手动跑 python data\weekly.py --days 7 后发布。")
    return 2 if fresh else 0


def split_items(items: list, elective_lids) -> tuple:
    """把待办切成两份。

    班级群那条：**删掉选修课**（那门课班里多数人没上，进群就是噪音）
    个人那条　：**完整清单**（含选修课）—— 你自己要看全部，一条不漏

    只按 lid 判断（课程名会重名，不可靠）。手动条目没有 lid，一律进班级群。
    """
    elec = {str(x) for x in (elective_lids or set())}
    class_items, personal_items = [], []
    for it in items:
        personal_items.append(it)
        if str(it.get("lid") or "") in elec:
            continue
        class_items.append(it)
    return class_items, personal_items


def _elective_lids_from_disk(data: dict) -> set:
    """读 electives.json；读不到就退回抓取结果里带的 electives 字段。"""
    path = ROOT / "electives.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return {str(k) for k in (d.get("courses") or {})}
    except Exception:  # noqa: BLE001
        return {str(x) for x in (data.get("electives") or [])}


def send_one(text: str, audience: str, source: str = "weekly",
             channel: str = None) -> tuple:
    """调用 notify.py 发一条。返回 (退出码, stdout, stderr)。

    audience=class  → 班级群（全部课程 − 选修课）
    audience=personal → 个人小群（完整清单）；没配通道时 notify 会跳过并退 0
    """
    path = ROOT / ("last_message_personal.txt" if audience == "personal"
                   else "last_message.txt")
    path.write_text(text, encoding="utf-8")
    cmd = [PY, str(ROOT / "notify.py"), "--file", str(path),
           "--source", source, "--audience", audience]
    if channel:
        cmd += ["--channel", channel]
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=UTF8_ENV)
    return r.returncode, r.stdout or "", r.stderr or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="统计未来 N 天（默认 7）")
    ap.add_argument("--no-send", action="store_true", help="不推送，只打印")
    ap.add_argument("--channel", help="覆盖 config.json 的推送渠道")
    ap.add_argument("--pool", action="store_true",
                    help="旧行为：只用首页「待提交作业」视图的课程（快，但谁交了哪门课，"
                         "全班就收不到那门课的提醒）。默认关闭")
    ap.add_argument("--check-freshness", action="store_true",
                    help="只检查「自动任务该跑没跑」，不抓取不推送（定时任务的前置检查）")
    ap.add_argument("--alert-if-stale", action="store_true",
                    help="配合 --check-freshness：发现陈旧时发一条告警")
    # 两条消息各自的开关：定时任务按「今天这条发过没有」精确重试，
    # 避免一条失败被另一条的成功掩盖（账是按 audience 分开记的）。
    ap.add_argument("--skip-class", action="store_true",
                    help="跳过班级群那条（今天已发过时由 task_run 传入）")
    ap.add_argument("--skip-personal", action="store_true",
                    help="跳过个人小群那条（今天已发过时由 task_run 传入）")
    ap.add_argument("--no-personal", action="store_true",
                    help="完全不发个人小群那条（只想发班级群时用）")
    args = ap.parse_args()

    # ---- 前置检查：定时任务到底有没有在工作？（不抓取，秒回）----
    if args.check_freshness:
        code = check_freshness()
        if code == 2 and args.alert_if_stale:
            try:
                import alert
                cfg = alert.load_cfg()
                _, why, _ = alert.check_freshness(cfg)
                alert.send("missed-run", "作业提醒自动任务该跑没跑", why, level="error")
            except Exception as e:  # noqa: BLE001
                print(f"WARN: 告警发送失败 {e}", file=sys.stderr)
        return code

    # 默认按「截止日期」取作业（全量扫描所有课程，不看是否已提交）——
    # 这份提醒是发给全班同学的，谁交没交不该决定某门课要不要提醒。
    extra = [] if args.pool else ["--deadline-only"]

    cached_payload = None
    cache_coverage = None
    if not args.pool:
        cached_payload, cache_coverage = load_fresh_publish_cache()
    if cached_payload:
        data = cached_payload
        print(f"复用完整新鲜缓存（{cache_coverage.get('age_sec', 0):.0f} 秒前，"
              f"{cache_coverage.get('expected_courses', 0)} 门课），不再访问平台。")
    else:
        if cache_coverage and cache_coverage.get("publish_block_reasons"):
            print("缓存不可直接发布：" + "；".join(cache_coverage["publish_block_reasons"]),
                  file=sys.stderr)
        print("抓取中..." + ("（全量扫描所有课程，约 1-3 分钟）" if not args.pool else ""))
        if not ensure_login():
            text = ("北化在线登录态已失效，且自动登录未成功"
                    "（可能平台要求验证码，或 Edge 里存的密码已过期）。\n"
                    f"处理：双击 {ROOT.parent / 'login.bat'} 手动登录一次，再重跑作业提醒。\n"
                    "影响：本次没有生成提醒，群里不会收到消息。")
            print(text)
            ok = send_alert("login-failed", "北化在线登录失效，自动登录也失败", text)
            print("已发送登录失效告警。" if ok else "告警发送失败（见 stderr）。", file=sys.stderr)
            return 1

        # ---- 抓取：这里的失败必须告警，否则就是静默漏报 ----
        try:
            data = fetch(extra or None)
        except Exception as e:  # noqa: BLE001
            text = (f"抓取阶段直接失败：{type(e).__name__}\n{e}\n\n"
                    "可能原因：登录态失效、平台改版、网络或浏览器异常。\n"
                    r"排查：看 data\server_console.log 与 data\fetch_log.jsonl；"
                    r"必要时双击 login.bat 重新登录后重跑。")
            print("抓取失败：", e, file=sys.stderr)
            send_alert("fetch-crashed", "作业提醒：抓取失败", text)
            return 1

    elapsed = data.get("_elapsed_sec")
    platform = data.get("homeworks", [])
    tests_n = len(data.get("tests", []) or [])
    print(f"平台抓到 {len(platform)} 条作业记录"
          + (f" + {tests_n} 条在线测试" if tests_n else "")
          + ("（按截止日期，含我已提交的）" if not args.pool else "（首页待提交池）"))
    if elapsed is not None:
        mins = elapsed / 60
        print(f"⏱ 本次抓取用时 {mins:.1f} 分钟（{elapsed:.0f} 秒）"
              f"（其中含在线测试扫描）")

    warn = data.get("warnings") or {}
    failed = warn.get("failed_courses") or []
    per_course = data.get("per_course") or []
    total_courses = len(per_course)
    if failed:
        print(f"⚠️ {len(failed)} 门课抓取失败，覆盖不完整："
              f"{', '.join(failed[:5])}", file=sys.stderr)

    # ---- 抓到了，但结果明显不健康：必须当失败处理，不能当「这周没作业」----
    unhealthy = []
    if failed and total_courses and len(failed) >= max(1, int(total_courses * MAX_FAIL_RATIO)):
        unhealthy.append(f"{len(failed)}/{total_courses} 门课抓取失败（过半，覆盖严重不完整）")
    if not platform and total_courses:
        unhealthy.append(f"扫了 {total_courses} 门课却一条平台作业都没抓到"
                         "（课程确实全没作业的可能性极小，更像会话失效或平台改版）")
    if unhealthy:
        text = ("本次抓取结果不可信，**本周提醒很可能漏报**：\n"
                + "\n".join(f"· {x}" for x in unhealthy)
                + f"\n\n统计：课程 {total_courses} 门 / 平台作业 {len(platform)} 条 / "
                  f"在线测试 {tests_n} 条。")
        if failed:
            text += "\n失败课程：\n" + "\n".join(f"  - {c}" for c in failed[:12])
        if warn.get("zero_homework_courses"):
            text += ("\n返回 0 条的课（可能确实没作业）："
                     + "、".join(warn["zero_homework_courses"][:10]))
        text += ("\n排查：data\\fetch_log.jsonl 看本次记录；"
                 "若是平台 0 条，先双击 login.bat 重新登录再重跑。")
        print("⚠️ " + "；".join(unhealthy), file=sys.stderr)
        send_alert("fetch-empty" if not platform else "fetch-partial",
                   "作业提醒：本次抓取结果不可信", text, level="error")

    manual = load_manual()
    if manual:
        print(f"手动录入 {len(manual)} 条待办")

    picked = merge_and_filter(data, manual, args.days)
    print(f"未来 {args.days} 天内待交：{len(picked)} 条")

    # ---- 两份文案：班级群（去选修课）/ 个人小群（完整清单）----
    elec = _elective_lids_from_disk(data)
    class_items, personal_items = split_items(picked, elec)
    class_msg = format_message(class_items, args.days)
    personal_msg = format_message(personal_items, args.days,
                                  title="📚 我的作业提醒（含选修课）",
                                  elective_lids=elec)
    if elec:
        print(f"选修课 {len(elec)} 门：班级群那条剔除 "
              f"{len(picked) - len(class_items)} 条，个人那条保留全部 {len(personal_items)} 条")

    print("-" * 40)
    print("【班级群 · 已去选修课】")
    print(class_msg)
    print("-" * 40)
    print("【个人小群 · 完整清单】")
    print(personal_msg)
    print("-" * 40)

    msg_file = ROOT / "last_message.txt"
    personal_file = ROOT / "last_message_personal.txt"
    msg_file.write_text(class_msg, encoding="utf-8")
    personal_file.write_text(personal_msg, encoding="utf-8")
    print("已落盘:", msg_file)
    print("已落盘:", personal_file)

    # 检索台账：定时任务（本脚本）也要记账，与控制台的 hub-list/publish 口径一致。
    # 这样「这周的推送到底是抓到了什么、有没有失败课程、复用了哪份数据」都有据可查。
    try:
        import fetch_log
        _note = ("演练模式（未发送）" if args.no_send
                 else (f"选修课 {len(elec)} 门只发个人" if elec else ""))
        fetch_log.log_fetch(
            "weekly", args.days, mode=("pool" if args.pool else "deadline-only"),
            rows=len(picked), rows_total=len(platform),
            platform=len(platform), tests=tests_n, manual=len(manual),
            elapsed=elapsed, failed_courses=warn.get("failed_courses") or [],
            cached=bool(cached_payload),
            data_age=(cache_coverage.get("age_sec") if cached_payload else None),
            notes=_note,
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写检索台账失败 {e}", file=sys.stderr)

    if args.no_send:
        print("演练模式：未发送。")
        return 0

    rc = 0

    # ---- 第一条：班级群（全部课程 − 选修课）----
    if args.skip_class:
        print("按 --skip-class 跳过班级群那条（今天已经发过了）。")
    else:
        try:
            code, out, err = send_one(class_msg, "class", "weekly", args.channel)
        except Exception as e:  # noqa: BLE001
            text = (f"调用推送脚本时直接异常：{type(e).__name__}\n{e}\n\n"
                    r"提醒正文已生成在 data\last_message.txt，可以先手动复制到群里顶上。")
            print("推送异常：", e, file=sys.stderr)
            send_alert("push-crashed", "作业提醒：推送环节异常", text)
            return 1
        print(out, end="")
        if code != 0:
            text = ("推送失败，**班级群没有收到本周提醒**：\n"
                    f"notify.py 退出码 {code}\n"
                    f"stderr：{err.strip()[-600:] or '（无）'}\n"
                    f"stdout：{out.strip()[-300:] or '（无）'}\n\n"
                    r"提醒正文在 data\last_message.txt —— 可先手动复制到群里顶上，"
                    "再排查渠道（config.json 里的 webhook / token 是否还有效）。")
            print("推送失败:", err, file=sys.stderr)
            send_alert("push-failed", "作业提醒：推送失败，班级群没收到", text, level="error")
            rc = code

    # ---- 第二条：个人小群（完整清单，含选修课）----
    if args.no_personal:
        print("按 --no-personal 跳过个人那条。")
    elif args.skip_personal:
        print("按 --skip-personal 跳过个人那条（今天已经发过了）。")
    else:
        try:
            code2, out2, err2 = send_one(personal_msg, "personal", "weekly", None)
        except Exception as e:  # noqa: BLE001
            print(f"个人推送异常：{type(e).__name__}: {e}", file=sys.stderr)
            code2, out2, err2 = 1, "", f"{type(e).__name__}: {e}"
        print(out2, end="")
        if code2 != 0:
            send_alert("personal-push-failed", "作业提醒：个人小群那条没发出去",
                       "个人（含选修课）那条推送失败，班级群那条不受影响。\n"
                       f"notify.py 退出码 {code2}\n"
                       f"stderr：{err2.strip()[-500:] or '（无）'}\n\n"
                       r"正文在 data\last_message_personal.txt；"
                       "检查 config.json 的 personal.wecom_webhook。", level="error")
            rc = rc or code2
    return rc


if __name__ == "__main__":
    sys.exit(main())
