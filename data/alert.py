# -*- coding: utf-8 -*-
"""失败告警层 —— 定时任务的前置件（2026-10-15 加）。

为什么需要（这是本项目最危险的失败模式）：
  「群里没收到提醒」和「这周真的没作业」在表面上完全一样。
  定时任务静默失败时没有任何外显信号，等你发现时可能已经漏了两周作业。
  所以定时机制之前必须先有告警 —— 否则自动化只会把静默失败放大。

设计要点：
  · **默认不碰班级群**。`channel`（作业提醒）是发给同学的，告警是发给你自己的；
    两者必须分开配置，否则一次抓取失败就会把技术性文字轰到全班群里。
    默认 channel=local：落盘 + Windows 通知，零配置、零风险。
  · **分级别**：error（漏报风险，必须人工处理）/ warn（覆盖不完整，先看台账）
    / info（提示，如「该跑没跑」）。级别只影响文案与日志，不影响送达。
  · **冷却去重**：同一个 kind 在 cooldown 秒内只发一次。连续多天失败不会变成
    每天一条骚扰 —— 但换了一类问题（kind 变了）会立刻发。
  · **可编排**：本文件既是模块（weekly.py 调 `send()`），也是命令行
    （定时任务可直接 `python alert.py --stdin`）。
  · 告警自身失败绝不影响主流程：任何异常都吞掉并打印，返回 False。

用法:
    python alert.py --title "标题" --detail "正文" --kind fetch-empty --level error
    echo "正文" | python alert.py --title "标题" --kind fetch-failed
    python alert.py --self-test          # 打印当前告警配置，不发送
    python alert.py --check-freshness    # 只检查「该跑没跑」，只输出结果不发送
    python alert.py --check-freshness --notify   # 检查并真的发告警

退出码：0 = 已（尝试）送达或无需发送；2 = 所有渠道都失败。
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import notify  # noqa: E402  复用渠道实现（剪贴板/企业微信/pushplus），不重复造

CONFIG_FILE = ROOT / "config.json"
LOG_FILE = ROOT / "alert_log.jsonl"
STATE_FILE = ROOT / "alert_state.json"

LEVELS = ("info", "warn", "error")

DEFAULT_ALERT = {
    "enabled": True,
    # local = 落盘 + Windows 通知（默认：不打扰任何人）
    # wecom_test = 企业微信【测试群】机器人（wecom_webhook）
    # wecom_prod = 企业微信【正式班级群】（wecom_webhook_prod）—— 慎用
    # clipboard / pushplus / none 同 notify.py
    "channel": "local",
    # 降级渠道：主渠道失败时再试一次（local 指本机通知，不会重复打扰群）
    "fallback_channel": "local",
    # 本机通知形式：balloon=托盘气泡（右下角，自动收回）/ dialog=消息框（模态，必须点掉）
    # / both=两个都弹 / auto=气泡失败时自动退消息框
    "local_style": "balloon",
    "cooldown_sec": 3600,
    # 「该跑没跑」的判定：距上次成功跑完超过 interval_days + grace_hours 就告警
    "interval_days": 7,
    "grace_hours": 12,
}


# ---------------------------------------------------------------- 配置
def load_cfg() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"WARN: config.json 解析失败({e})，告警用默认配置", file=sys.stderr)
    out = dict(DEFAULT_ALERT)
    out.update(cfg.get("alert") or {})
    out["_raw"] = cfg          # 主配置（webhook / token 在这里）
    return out


def resolve_channel(name: str, raw: dict):
    """把告警渠道名翻译成 notify.py 的 (channel, cfg) 调用参数。

    wecom_test / wecom_prod 是本文件专有的别名：
      · wecom_test -> 用 wecom_webhook（测试群）
      · wecom_prod -> 把 wecom_webhook_prod 临时顶到 wecom_webhook 位置上发给班级群
    local 不走 notify.py，由本文件自己实现（toast + 落盘）。
    """
    name = (name or "").strip().lower()
    if name in ("local", "none"):
        return name, raw
    if name == "wecom_test":
        return "wecom", raw
    if name == "wecom_prod":
        cfg = dict(raw)
        prod = (raw.get("wecom_webhook_prod") or "").strip()
        if not prod:
            return "wecom", raw        # 没配就退回测试群，不静默失败
        cfg["wecom_webhook"] = prod
        return "wecom", cfg
    return name, raw


# ---------------------------------------------------------------- 投递
# 本机通知的三条实测结论（2026-10-16 在这台机器上验证）：
#   1) WinRT 那套 toast（Windows.UI.Notifications + CreateToastNotifier）已经**不可用**：
#      `The process has no package identity. (HRESULT: 0x80073D54)` —— 新版 Windows
#      不再允许非打包（没有 MSIX 包标识）的进程弹 WinRT toast。
#   2) 托盘气泡（System.Windows.Forms.NotifyIcon.ShowBalloonTip）可用，右下角弹出、
#      超时自动收回，不需要包标识，也不阻塞。
#   3) 脚本一律用 **-EncodedCommand（base64 / UTF-16LE）** 传：
#      既避开引号嵌套，也避开「无 BOM 的 .ps1 被 Windows PowerShell 5.1 按 GBK 读」
#      （实测：那份 .ps1 里的中文直接让脚本语法报错）。
BALLOON_TIMEOUT = 12


def _ps_escape(s: str) -> str:
    """按 PowerShell 单引号字符串规则转义（单引号翻倍）。"""
    return (s or "").replace("'", "''")


def _run_ps(script: str, timeout: int = 30):
    """用 -EncodedCommand 跑一段 PowerShell。返回 (ok, 说明)。

    优先 pwsh（PowerShell 7）：WinRT 类型虽然没了，但 WinForms 通知更稳；
    没有 pwsh 时退回 Windows PowerShell，它会读 UTF-16 编码的命令，
    所以中文不会被代码页弄坏。
    """
    fn = shutil.which("pwsh") or shutil.which("powershell")
    if not fn:
        return False, "找不到 pwsh / powershell"
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            [fn, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", enc],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # 通知类脚本会在弹完后自己 sleep 一会儿再退出；超时不算失败
        return True, "timeout-but-likely-shown"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        return False, f"退出码 {r.returncode} {err}"
    return True, "ok"


def _ps_balloon(title: str, body: str) -> tuple:
    """托盘气泡通知：右下角弹出，几秒后自动收回。返回 (ok, 说明)。"""
    script = (
        "$ErrorActionPreference='Stop'\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$ni = New-Object System.Windows.Forms.NotifyIcon\n"
        "$ni.Icon = [System.Drawing.SystemIcons]::Warning\n"
        f"$ni.BalloonTipTitle = '{_ps_escape(title[:63])}'\n"
        f"$ni.BalloonTipText = '{_ps_escape(body[:255])}'\n"
        "$ni.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning\n"
        "$ni.Visible = $true\n"
        "$ni.ShowBalloonTip(10000)\n"
        "Start-Sleep -Seconds 8\n"
        "$ni.Visible = $false\n"
        "$ni.Dispose()\n"
        "Write-Output 'BALLOON-DONE'\n"
    )
    return _run_ps(script, timeout=BALLOON_TIMEOUT + 6)


def _ps_dialog(title: str, body: str) -> tuple:
    """Windows 消息框（模态，需要点确定）。用于「必须被看见」的场合。"""
    script = (
        "$ErrorActionPreference='Stop'\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        f"$t = '{_ps_escape(title[:120])}'\n"
        f"$b = '{_ps_escape(body[:1500])}'\n"
        "[System.Windows.Forms.MessageBox]::Show($b, $t, "
        "[System.Windows.Forms.MessageBoxButtons]::OK, "
        "[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null\n"
        "Write-Output 'DIALOG-DONE'\n"
    )
    return _run_ps(script, timeout=120)


def deliver_local(title: str, text: str, style: str = "balloon") -> bool:
    """本机告警：落盘 + 弹通知，不需要任何配置。

    返回**真实**成败：以前这里无论通知弹没弹出来都 return True，
    结果 WinRT toast 在这台机器上早就废了，而台账里一路记着 sent=true ——
    这正是我们要消灭的那种静默失败。
    """
    ok_file = True
    try:
        (ROOT / "last_alert.txt").write_text(text, encoding="utf-8")
    except OSError as e:
        ok_file = False
        print(f"WARN: 写 last_alert.txt 失败 {e}", file=sys.stderr)

    body = " ".join(text.split())[:250]
    style = (style or "balloon").lower()
    shown, why = False, "未尝试"

    if style in ("balloon", "both", "auto"):
        shown, why = _ps_balloon(title, body)
        if shown:
            print("[本地告警] 已弹出托盘通知（右下角）。", file=sys.stderr)
    if not shown and style in ("dialog", "both", "balloon", "auto"):
        # 气泡失败（或被要求用对话框）时，用消息框兜底 —— 它一定会被看见
        d_ok, d_why = _ps_dialog(title, text[:1200])
        if d_ok:
            shown, why = True, "已用消息框弹出（气泡方式失败：" + str(why) + "）"
        else:
            why = f"气泡失败({why})；消息框也失败({d_why})"

    if not shown:
        # 最后兜底：把告警文件推到台前，别让告警彻底消失
        try:
            os.startfile(str(ROOT / "last_alert.txt"))  # noqa: S606
            shown, why = True, "通知弹窗不可用，已打开告警文本"
        except Exception as e:  # noqa: BLE001
            print(f"WARN: 打开告警文本也失败 {e}", file=sys.stderr)

    print(f"[本地告警] {title}\n{text}", file=sys.stderr)
    print(f"[本地告警] 通知结果：{'成功' if shown else '失败'}（{why}）"
          + ("" if ok_file else "；但告警文本落盘失败"), file=sys.stderr)
    # 只要通知真的弹出来了就算送达成功。
    # 落盘失败不该把「已经看到告警」判成失败 —— 但落盘失败本身是异常，要让它显形，
    # 所以单独打一行 WARN（server_console.log 里能看到）。
    if shown and not ok_file:
        print("WARN: 告警文本没能落盘（data/last_alert.txt 写入被拒），"
              "但通知已弹出，本次按送达成功处理。", file=sys.stderr)
    return bool(shown)


def deliver(channel: str, raw: dict, title: str, text: str, style: str = None) -> bool:
    ch, cfg = resolve_channel(channel, raw)
    if ch == "local":
        return deliver_local(title, text, style=style or cfg.get("local_style") or "balloon")
    if ch == "none":
        print("[告警渠道 none] 未发送，仅落盘", file=sys.stderr)
        return True
    try:
        return bool(notify.dispatch(text, ch, cfg))
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 告警渠道 {ch} 异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------- 台账 / 去重
def log_alert(kind: str, level: str, title: str, text: str, sent: bool,
              channels: list, note: str = "") -> None:
    rec = {
        "ts": round(time.time(), 3),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "level": level,
        "title": title,
        "sent": bool(sent),
        "channels": channels,
        "note": note,
        "msg_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "first_line": text.splitlines()[0][:80] if text.strip() else "",
        "pid": os.getpid(),
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写告警台账失败 {e}", file=sys.stderr)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def cooled_down(kind: str, cooldown_sec: int):
    """同一 kind 是否还在冷却期内。返回 (是否可以发, 上次时间说明)。"""
    if cooldown_sec <= 0:
        return True, ""
    st = _load_state().get(kind) or {}
    last = float(st.get("ts") or 0)
    if not last:
        return True, ""
    age = time.time() - last
    if age >= cooldown_sec:
        return True, ""
    left = cooldown_sec - age
    return False, (f"上次同类告警在 {st.get('time')}（{age / 60:.0f} 分钟前），"
                   f"冷却 {cooldown_sec} 秒内不再重复，还剩 {left / 60:.0f} 分钟")


def _mark_state(kind: str) -> None:
    st = _load_state()
    st[kind] = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"WARN: 写告警冷却状态失败 {e}", file=sys.stderr)


# ---------------------------------------------------------------- 主入口
def send(kind: str, title: str, detail: str, level: str = "error",
         no_dedup: bool = False, channel: str = None, style: str = None) -> bool:
    """发一条告警。返回是否有渠道送达成功（被冷却跳过也算「无需再发」= True）。

    绝不抛异常：告警是旁路，不能因为它把主流程搞挂。
    """
    try:
        cfg = load_cfg()
        if not cfg["enabled"]:
            print("告警已关闭（alert.enabled=false），本条不发送。", file=sys.stderr)
            return True
        if style:
            cfg["local_style"] = style
        level = (level or "error").lower()
        if level not in LEVELS:
            level = "error"
        text = f"[{level.upper()}] {title}\n{detail}\n（北化在线作业提醒 · {time.strftime('%Y-%m-%d %H:%M')}）"

        if not no_dedup:
            ok, why = cooled_down(kind, int(cfg["cooldown_sec"] or 0))
            if not ok:
                print(f"告警冷却中：{why}", file=sys.stderr)
                log_alert(kind, level, title, text, False, [], note="cooldown-skipped")
                return True

        ch = channel or cfg["channel"]
        fb = cfg.get("fallback_channel")
        tried, sent = [], False
        for c in [ch] + ([fb] if fb and fb != ch else []):
            tried.append(c)
            if deliver(c, cfg["_raw"], title, text, style=style):
                sent = True
                break
        log_alert(kind, level, title, text, sent, tried,
                  note="" if sent else "all-channels-failed")
        if sent:
            _mark_state(kind)
        else:
            print(f"ERROR: 告警所有渠道都失败（{tried}）", file=sys.stderr)
        return sent
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 告警发送异常（已忽略，不影响主流程）：{type(e).__name__}: {e}",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------- 该跑没跑？
# 哪些 source 算「自动任务跑过」：
#   weekly    = 直接跑 weekly.py（手动或旧路子）
#   task-run  = 计划任务经 task_run.py 跑的（定时推送的正路）
# 手动点控制台「提前发布」写的是 hub-*，**不算** —— 把它算成功就永远查不出
# 「定时任务其实从没跑过」，那正是本项目此前的真实状态。
AUTO_SOURCES = ("weekly", "task-run")


def _last_run() -> dict:
    """最近一次「成功的自动跑」：只认 source=weekly 且 ok 的记录。

    为什么只认 weekly：手动点「提前发布」也会写台账（source=hub-*）。
    如果把手动当成功，那「定时任务其实从没跑过」就永远查不出来 ——
    这正是本项目此前的真实状态（fetch_log 里 weekly 只有一条演练记录）。
    """
    out = {"fetch": None, "push": None, "any_fetch": None, "any_push": None}

    def scan(path: Path, key: str, any_key: str):
        """扫一份台账。文件本身决定了记录类型：fetch_log 里每行都是抓取记录。

        别按「行里有没有名为 fetch 的字段」来过滤 —— 台账里没有那个字段
        （实测踩过：这样过滤会把 12 行全跳过，于是「自动任务从未运行过」
        这个结论永远为真，定时任务真在跑也会天天误报）。
        """
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(rec, dict):
                continue
            out[any_key] = out[any_key] or rec
            if rec.get("ok") and str(rec.get("source") or "").startswith(AUTO_SOURCES):
                out[key] = rec
                break

    scan(ROOT / "fetch_log.jsonl", "fetch", "any_fetch")
    scan(ROOT / "push_log.jsonl", "push", "any_push")
    return out

def check_freshness(cfg: dict = None, now=None):
    """检查「该跑没跑」。返回 (是否陈旧, 说明文本, 证据 dict)。

    判定：距最近一次**成功的 weekly 运行**超过 interval_days + grace_hours。
    从来没有成功过 -> 也算陈旧（并把最近一次其它来源的记录说出来，便于对照）。
    """
    cfg = cfg or load_cfg()
    now = now or datetime.now()
    limit_h = float(cfg["interval_days"]) * 24 + float(cfg["grace_hours"])
    last = _last_run()

    def when(rec):
        if not rec:
            return None
        try:
            return datetime.strptime(rec["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return None

    newest = max([d for d in (when(last["fetch"]), when(last["push"]))
                  if d is not None], default=None)
    # 兜底只看「抓取成功」：抓取成功本身就是定时任务在工作过的证据。
    # 若把「推送成功」设成硬性条件，演练模式（--no-send）或 channel=none 的机器
    # 会永远被判成「没在跑」，那是误报而不是发现故障。
    if newest is None:
        newest = when(last["fetch"])
    any_newest = max([d for d in (when(last["any_fetch"]), when(last["any_push"]))
                      if d is not None], default=None)

    ev = {
        "last_weekly_fetch": last["fetch"] and last["fetch"].get("time"),
        "last_weekly_push": last["push"] and last["push"].get("time"),
        "last_any_fetch": last["any_fetch"] and last["any_fetch"].get("time"),
        "last_any_push": last["any_push"] and last["any_push"].get("time"),
        "limit_hours": limit_h,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }

    if newest is None:
        tail = (f"（最近一次任何来源的检索是 {any_newest:%Y-%m-%d %H:%M}，"
                f"但那不是定时任务跑的）" if any_newest else "（台账里一条记录都没有）")
        return True, (f"自动任务从未成功运行过：台账里没有任何 source=weekly 且成功的记录{tail}。"
                      f"提醒目前只能靠手动点「检索 + 提前发布」。"), ev

    age_h = (now - newest).total_seconds() / 3600
    if age_h > limit_h:
        return True, (f"自动任务已 {age_h / 24:.1f} 天没有成功运行"
                      f"（上次成功 {newest:%Y-%m-%d %H:%M}，阈值 {limit_h:.0f} 小时）。"
                      f"本周的作业提醒可能没有发出去。"), ev
    return False, (f"自动任务正常：上次成功 {newest:%Y-%m-%d %H:%M}"
                   f"（{age_h / 24:.1f} 天前，阈值 {limit_h:.0f} 小时）。"), ev


# ---------------------------------------------------------------- CLI
def self_test(cfg: dict) -> int:
    ch = cfg["channel"]
    raw = cfg["_raw"]
    resolved, _ = resolve_channel(ch, raw)
    print(f"告警开关: {'开启' if cfg['enabled'] else '关闭（enabled=false）'}")
    print(f"告警渠道: {ch}  ->  实际使用 {resolved}")
    print(f"降级渠道: {cfg.get('fallback_channel')}")
    print(f"冷却窗口: {cfg['cooldown_sec']} 秒（同类告警该窗口内只发一次）")
    print(f"陈旧阈值: {cfg['interval_days']} 天 + {cfg['grace_hours']} 小时")
    print("-" * 46)
    if resolved == "wecom":
        hook = ("wecom_webhook_prod" if ch == "wecom_prod" else "wecom_webhook")
        has = bool((raw.get(hook) or "").strip())
        print(f"{'[就绪]' if has else '[缺配置]':10s} 企业微信群机器人（{hook}）")
        if ch == "wecom_prod":
            print("  ⚠️ wecom_prod 会把告警发到【班级群】，同学会看到技术性文字")
    elif resolved == "pushplus":
        print(f"{'[就绪]' if (raw.get('pushplus_token') or '').strip() else '[缺配置]':10s} pushplus")
    elif resolved in ("clipboard", "local"):
        print(f"{'[就绪]':10s} {resolved}（本机可见，不影响任何群）")
    else:
        print(f"{'[无需配置]':10s} {resolved}")
    fresh, why, _ = check_freshness(cfg)
    print("-" * 46)
    print(f"该跑没跑检查: {'⚠️ 陈旧' if fresh else '✅ 正常'}")
    print(f"  {why}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="失败告警：定时任务的前置件")
    ap.add_argument("--title", default="作业提醒任务失败")
    ap.add_argument("--detail", help="告警正文")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读正文")
    ap.add_argument("--kind", default="generic", help="告警类别（同时是去重键）")
    ap.add_argument("--level", default="error", choices=list(LEVELS))
    ap.add_argument("--channel", help="覆盖 config.json 的 alert.channel")
    ap.add_argument("--style", choices=["balloon", "dialog", "both", "auto"],
                    help="本机通知形式：balloon=托盘气泡（默认）/ dialog=消息框 / both / auto")
    ap.add_argument("--no-dedup", action="store_true", help="跳过冷却检查，强制发送")
    ap.add_argument("--self-test", action="store_true", help="打印告警配置，不发送")
    ap.add_argument("--check-freshness", action="store_true", help="只检查「该跑没跑」")
    ap.add_argument("--notify", action="store_true",
                    help="配合 --check-freshness：陈旧时真的发告警")
    args = ap.parse_args()

    cfg = load_cfg()

    if args.self_test:
        return self_test(cfg)

    if args.check_freshness:
        fresh, why, ev = check_freshness(cfg)
        print(("⚠️ 陈旧: " if fresh else "✅ 正常: ") + why)
        print("证据: " + json.dumps(ev, ensure_ascii=False))
        if fresh and args.notify:
            ok = send("missed-run", "作业提醒该跑没跑", why, level="error")
            return 0 if ok else 2
        return 0

    detail = args.detail
    if args.stdin and not detail:
        detail = sys.stdin.read()
    if not detail:
        print("ERROR: 需要 --detail 或 --stdin", file=sys.stderr)
        return 1
    ok = send(args.kind, args.title, detail.strip(), level=args.level,
              no_dedup=args.no_dedup, channel=args.channel, style=args.style)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
