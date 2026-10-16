# -*- coding: utf-8 -*-
"""
推送层：把汇总文本发到指定渠道。

支持渠道（在 config.json 里配置 channel 字段）:
  clipboard  默认。复制到剪贴板 + 弹 Windows 通知 + 落盘。零配置，零风险。
  pushplus   公众号推送。需要 config.json 里填 pushplus_token。
  wecom      企业微信群机器人。需要 config.json 里填 wecom_webhook。
  wechat     PC 微信客户端 UI 自动化（不稳定，微信 4.x 常失效，不推荐）。
  none       只落盘，不推送。

用法:
  python notify.py --file last_message.txt            # 按 config.json 的 channel 推送
  python notify.py --file last_message.txt --channel pushplus
  python notify.py --self-test                        # 检查各渠道配置是否就绪
"""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 下子进程 stdout 默认按 GBK(cp936) 编码，父进程用 utf-8 读会 UnicodeDecodeError。
# 强制本进程以 utf-8 输出，保证被 weekly.py 调用时解码一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
PY = sys.executable

DEFAULT_CONFIG = {
    "channel": "clipboard",
    "pushplus_token": "",
    "pushplus_topic": "",
    "wecom_webhook": "",
    "wechat_target": "文件传输助手",
    "notify_title": "北化在线作业提醒",
    # 去重窗口（秒）：窗口内若已成功推送过「完全相同」的内容，则跳过并标记 duplicate。
    # 用于防止手滑连点两次「提前发布」造成重复打扰；设为 0 即关闭。
    "dedup_window_sec": 300,
}

PUSH_LOG = ROOT / "push_log.jsonl"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) buct-homework-bot/1.0"


# ---------------------------------------------------------------- 推送台账 / 去重
def _msg_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tail_log(minutes: int = 30) -> list:
    """读最近 N 分钟的推送台账（jsonl 每行一条）。"""
    if not PUSH_LOG.exists():
        return []
    cutoff = time.time() - minutes * 60
    out = []
    try:
        for line in PUSH_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if float(rec.get("ts", 0)) >= cutoff:
                out.append(rec)
    except Exception:  # noqa: BLE001
        return []
    return out


def dedup_target(channel: str, cfg: dict) -> str:
    """去重作用域：只有「同一个目标」上的重复才跳过。

    否则会出现这种误伤：往测试群发过一次，班级群同内容就被判成重复而不发。
    """
    ch = (channel or "").lower()
    if ch == "wecom":
        return _msg_hash(str(cfg.get("wecom_webhook", "")))
    if ch == "pushplus":
        return _msg_hash(str(cfg.get("pushplus_token", "")))
    if ch == "wechat":
        return "wechat:" + str(cfg.get("wechat_target", ""))
    return ch or "default"


def log_push(source: str, channel: str, text: str, ok: bool,
             note: str = "", target: str = "") -> None:
    """往 push_log.jsonl 追一条记录。

    目的：以后再出现「莫名多一条提醒」，能一眼看出是谁、什么时候、发到哪、发了什么。
    """
    rec = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "unknown",     # weekly / hub / notify-cli / test
        "channel": channel,
        "target": target or dedup_target(channel, {}),
        "ok": bool(ok),
        "note": note,
        "msg_hash": _msg_hash(text),
        "bytes": len(text.encode("utf-8")),
        "items": text.count("\n• ") + (1 if text.lstrip().startswith("• ") else 0),
        "first_line": text.splitlines()[0][:60] if text.strip() else "",
        "pid": os.getpid(),
    }
    try:
        with PUSH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 写推送台账失败 {e}", file=sys.stderr)


def recent_duplicate(text: str, window_sec: int, target: str = ""):
    """窗口内、同一目标上，是否有「内容完全相同且成功」的推送；有则返回那条记录。"""
    if window_sec <= 0:
        return None
    h = _msg_hash(text)
    cutoff = time.time() - window_sec
    for rec in reversed(_tail_log(minutes=max(1, window_sec // 60 + 2))):
        if rec.get("msg_hash") != h or not rec.get("ok"):
            continue
        if target and rec.get("target") and rec.get("target") != target:
            continue                      # 发到别的群不算「已经发过」
        if float(rec.get("ts", 0)) < cutoff:
            continue
        if str(rec.get("note", "")).startswith("duplicate"):
            continue                      # 被去重跳过的记录不算「发过了」
        return rec
    return None



def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"WARN: config.json 解析失败({e})，用默认配置", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------- 目标群解析
# 本项目的 webhook 有两个来源，很容易搞混，所以解析集中在这一个函数里：
#   wecom_webhook        主用（按 README 的约定，日常测试时放测试群）
#   wecom_webhook_prod   正式班级群（发之前把上面那条换成它）
#   wecom_webhook_test   测试群（新增：与 prod 对称，避免「主用到底是哪个」说不清）
#   BUCT_WECOM_WEBHOOK   环境变量覆盖（定时任务/一次性演练用，改的是进程而不是文件）
WECOM_ENV = "BUCT_WECOM_WEBHOOK"
CHANNEL_ENV = "BUCT_NOTIFY_CHANNEL"


def resolve_webhook(when: str = "config", cfg: dict = None, strict: bool = False) -> tuple:
    """决定往哪个企业微信机器人发。返回 (webhook, 说明)。

    when: config（按 config.json 的 wecom_webhook）/ test / prod
    环境变量 BUCT_WECOM_WEBHOOK 优先级最高 —— 定时任务用它在不修改 config.json 的
    前提下把这次推送定向到测试群，避免「为了测试改了配置文件、忘了改回来」。

    strict=True（定时任务用）：when=test 而 wecom_webhook_test 为空时**返回空**
    而不是退回 wecom_webhook。理由：无人看管的定时任务绝不能因为"测试群没配"
    就退回到主用那条 —— 主用那条随时可能被你换成正式班级群，那样一次误发就是
    全班同学都收到。宁可任务报错停止（会被告警看到），也不要发错群。
    """
    cfg = cfg or load_config()
    env = (os.environ.get(WECOM_ENV) or "").strip()
    if env:
        return env, f"环境变量 {WECOM_ENV} 覆盖"
    if when == "test":
        t = (cfg.get("wecom_webhook_test") or "").strip()
        if t:
            return t, "config.wecom_webhook_test（测试群）"
        if strict:
            return "", ("wecom_webhook_test 未配置 —— 定时任务拒绝运行，"
                        "以免退回主用那条（可能是班级群）")
        # 非严格场景（人工操作）才退回，并把这件事说清楚（不静默）
        main = (cfg.get("wecom_webhook") or "").strip()
        return main, "未配 wecom_webhook_test，退回 wecom_webhook（主用那条）"
    if when == "prod":
        p = (cfg.get("wecom_webhook_prod") or "").strip()
        if p:
            return p, "config.wecom_webhook_prod（正式班级群）"
        main = (cfg.get("wecom_webhook") or "").strip()
        return main, "未配 wecom_webhook_prod，退回 wecom_webhook（主用那条）"
    main = (cfg.get("wecom_webhook") or "").strip()
    return main, "config.wecom_webhook（主用那条）"


# ---------------------------------------------------------------- clipboard
def _set_clipboard(text: str) -> None:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32

    # 64 位下必须显式声明指针类型，否则句柄被截断成 int，静默失败
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.restype = ctypes.c_int
    u32.EmptyClipboard.restype = ctypes.c_int
    u32.CloseClipboard.restype = ctypes.c_int
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.restype = ctypes.c_int

    if not u32.OpenClipboard(None):
        raise RuntimeError("OpenClipboard 失败（剪贴板被占用？）")
    u32.EmptyClipboard()
    nbytes = (len(text) + 1) * 2
    h = k32.GlobalAlloc(GMEM_MOVEABLE, nbytes)
    if not h:
        u32.CloseClipboard()
        raise RuntimeError("GlobalAlloc 失败")
    addr = k32.GlobalLock(h)
    if not addr:
        u32.CloseClipboard()
        raise RuntimeError("GlobalLock 失败")
    ctypes.memmove(addr, text.encode("utf-16-le"), len(text) * 2)
    k32.GlobalUnlock(h)
    if not u32.SetClipboardData(CF_UNICODETEXT, h):
        u32.CloseClipboard()
        raise RuntimeError("SetClipboardData 失败")
    u32.CloseClipboard()


def _toast(title: str, body: str) -> None:
    """Windows 10/11 toast 通知（无第三方依赖，走 PowerShell）。"""
    # 单引号会破坏 PowerShell 字符串，先剔除；换行压平
    safe_title = title.replace("'", "").replace("\n", " ")[:60]
    safe_body = body.replace("'", "").replace("\n", " ")[:120]
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastText02"><text id="1"></text><text id="2"></text></binding></visual></toast>')
$xml.GetElementsByTagName('text').Item(0).AppendChild($xml.CreateTextNode('{safe_title}')) | Out-Null
$xml.GetElementsByTagName('text').Item(1).AppendChild($xml.CreateTextNode('{safe_body}')) | Out-Null
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('BUCT Homework').Show($toast)
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=25,
        )
    except Exception as e:  # noqa: BLE001
        print(f"WARN: toast 失败 {e}", file=sys.stderr)


def send_clipboard(text: str, cfg: dict) -> bool:
    try:
        _set_clipboard(text)
    except Exception as e:  # noqa: BLE001
        # 兜底：PowerShell Set-Clipboard
        print(f"WARN: 剪贴板 API 失败({e})，改用 PowerShell", file=sys.stderr)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$input | Set-Clipboard"],
            input=text, text=True, encoding="utf-8",
            capture_output=True, timeout=25,
        )
        if r.returncode != 0:
            print(f"ERROR: 剪贴板写入失败 {r.stderr}", file=sys.stderr)
            return False
    first = next((l for l in text.splitlines() if l.strip()), "")
    _toast(cfg.get("notify_title", "作业提醒"), first)
    print("已复制到剪贴板，并弹出系统通知。直接 Ctrl+V 到任意微信会话即可。", file=sys.stderr)
    return True


# ---------------------------------------------------------------- pushplus
def send_pushplus(text: str, cfg: dict) -> bool:
    token = (cfg.get("pushplus_token") or "").strip()
    if not token:
        print("ERROR: pushplus_token 为空，请在 config.json 填写。", file=sys.stderr)
        return False
    payload = {
        "token": token,
        "title": cfg.get("notify_title", "作业提醒"),
        "content": text.replace("\n", "<br>"),
        "template": "html",
    }
    if cfg.get("pushplus_topic"):
        payload["topic"] = cfg["pushplus_topic"]
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: pushplus 请求失败 {e}", file=sys.stderr)
        return False
    if res.get("code") == 200:
        print("pushplus 发送成功。", file=sys.stderr)
        return True
    print(f"ERROR: pushplus 返回 {res}", file=sys.stderr)
    return False


# ---------------------------------------------------------------- wecom
def send_wecom(text: str, cfg: dict) -> bool:
    hook = (os.environ.get(WECOM_ENV) or "").strip() or (cfg.get("wecom_webhook") or "").strip()
    if not hook:
        print("ERROR: wecom_webhook 为空，请在 config.json 填写。", file=sys.stderr)
        return False
    payload = {"msgtype": "text", "text": {"content": text}}
    req = urllib.request.Request(
        hook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: 企业微信请求失败 {e}", file=sys.stderr)
        return False
    if res.get("errcode") == 0:
        # 成功提示一律走 stderr：stdout 要保持纯 JSON，避免调用方（console.ps1）解析失败
        print("企业微信机器人发送成功。", file=sys.stderr)
        return True
    print(f"ERROR: 企业微信返回 {res}", file=sys.stderr)
    return False


# ---------------------------------------------------------------- wechat ui
def send_wechat(text: str, cfg: dict) -> bool:
    tmp = ROOT / "_notify_payload.txt"
    tmp.write_text(text, encoding="utf-8")
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [PY, str(ROOT / "wechat_send.py"), "--to", cfg.get("wechat_target", "文件传输助手"),
         "--file", str(tmp)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    print(r.stdout or "", end="", file=sys.stderr)
    if r.returncode != 0:
        print(r.stderr or "", file=sys.stderr)
    return r.returncode == 0


SENDERS = {
    "clipboard": send_clipboard,
    "pushplus": send_pushplus,
    "wecom": send_wecom,
    "wechat": send_wechat,
    "none": lambda t, c: True,
}


def dispatch(text: str, channel: str, cfg: dict) -> bool:
    fn = SENDERS.get((channel or "").lower())
    if not fn:
        print(f"ERROR: 未知渠道 {channel}，可选: {', '.join(SENDERS)}", file=sys.stderr)
        return False
    return fn(text, cfg)


def self_test(cfg: dict) -> int:
    print(f"配置文件: {CONFIG_FILE} {'(存在)' if CONFIG_FILE.exists() else '(不存在，用默认)'}")
    print(f"当前渠道: {cfg['channel']}")
    print("-" * 40)
    ok = True
    checks = [
        ("clipboard", "零配置", True),
        ("pushplus", "需 pushplus_token", bool((cfg.get("pushplus_token") or "").strip())),
        ("wecom", "需 wecom_webhook", bool((cfg.get("wecom_webhook") or "").strip())),
        ("wechat", "需 PC 微信已登录（UI 自动化，不稳定）", None),
    ]
    for name, need, ready in checks:
        if ready is True:
            flag = "[可用]"
        elif ready is False:
            flag = "[缺配置]"
        else:
            flag = "[未检测]"
        print(f"{flag:10s} {name:10s} {need}")
        if ready is False and name == cfg["channel"]:
            ok = False
    print("-" * 40)
    if not ok:
        print(f"当前渠道 {cfg['channel']} 缺配置，推送会失败。")
        return 1
    print("当前渠道配置就绪。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="待推送文本文件（utf-8）")
    ap.add_argument("--message", help="直接给文本")
    ap.add_argument("--channel", help="覆盖 config.json 的 channel")
    ap.add_argument("--self-test", action="store_true", help="检查渠道配置")
    ap.add_argument("--source", default="notify-cli",
                    help="标记谁在推（weekly / hub / notify-cli），写进推送台账")
    ap.add_argument("--no-dedup", action="store_true",
                    help="跳过去重检查，强制发送（确实需要重复发同一条时用）")
    args = ap.parse_args()

    cfg = load_config()

    if args.self_test:
        return self_test(cfg)

    if args.message:
        text = args.message
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        print("ERROR: 需要 --file 或 --message", file=sys.stderr)
        return 1

    # 渠道优先级：--channel > 环境变量 > config.json
    # 环境变量这条是给定时任务用的：一次演练（--dry-run）靠它把渠道切成 none，
    # 而不必修改 config.json —— 改文件再忘改回来是本项目记过的风险。
    channel = (args.channel or os.environ.get(CHANNEL_ENV) or ""
               or cfg.get("channel", "clipboard"))
    target = dedup_target(channel, cfg)

    # 去重：同一目标上、窗口内推过完全相同的内容就跳过（防止连点两次造成重复打扰）
    if not args.no_dedup:
        window = int(cfg.get("dedup_window_sec", 300) or 0)
        dup = recent_duplicate(text, window, target)
        if dup:
            print(f"检测到 {window} 秒内已向同一目标推送过完全相同的内容"
                  f"（{dup.get('time')} by {dup.get('source')}），本次跳过。", file=sys.stderr)
            log_push(args.source, channel, text, True,
                     note="duplicate-skipped", target=target)
            print(f"推送渠道: {channel}（已去重跳过）")
            return 0

    print(f"推送渠道: {channel}")
    ok = dispatch(text, channel, cfg)
    log_push(args.source, channel, text, ok,
             note="" if ok else "send-failed", target=target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
