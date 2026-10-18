# -*- coding: utf-8 -*-
"""读取本机 Edge 已保存的北化账号密码（只在内存里解密，绝不落盘）。

背景（2026-10-18 平台改版实测）：
  登录从 `portal.buct.edu.cn` 的 iframe 改成了 `sidportal.buct.edu.cn` 的 Angular 单页。
  Edge 的自动填充绑定在**旧的 origin** 上（Login Data 里只有
  portal.buct.edu.cn / experimental-auth-endpoint.buct.edu.cn 两条），
  新域名上永远填不上；旧登录页也已经 404。
  所以自动登录必须自己把账号密码写进表单 —— 凭据从 Edge 自己的密码库取，
  这样不需要用户再输入一次，也不用把密码存到项目里。

实现（两段解密，都是 Windows 原生能力）：
  1. `Local State` 里的 `os_crypt.encrypted_key`（base64，前缀 DPAPI）
     -> CryptUnprotectData（当前用户 DPAPI）得到 32 字节 AES 密钥；
  2. `Login Data` 里 `password_value` 是 `v10` + 12B nonce + 密文 + 16B tag，
     用上面的密钥走 AES-256-GCM 解密（bcrypt/CNG，ctypes 调用，无第三方依赖）。

拿不到就返回 (None, None)，由 login.py 回退到「打开窗口让人工登录」。
"""
import base64
import ctypes
import json
import shutil
import sqlite3
import tempfile
from ctypes import wintypes
from pathlib import Path

PROFILE_DIR = Path.home() / ".workbuddy" / "profiles" / "buct-course-edge"
LOCAL_STATE = PROFILE_DIR / "Local State"
LOGIN_DB = PROFILE_DIR / "Default" / "Login Data"


def _candidate_profiles() -> list:
    """可能存着北化密码的 Edge 用户目录，按优先级排列。

    为什么不止一个：自动登录用的是专用 profile（buct-course-edge），
    但你很可能在**日常 Edge** 里手动登录过一次 —— 那种情况下密码存在
    默认用户目录里。两个地方都找，才不会出现「明明存了却读不到」。
    """
    out = [PROFILE_DIR]
    for base in (Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
                 Path.home() / "AppData" / "Local" / "Microsoft" / "Edge Dev" / "User Data"):
        if not base.exists():
            continue
        out.append(base)                       # Default
        try:
            out += sorted(p for p in base.iterdir()
                          if p.is_dir() and p.name.startswith("Profile"))
        except OSError:
            pass
    return out

# 优先级：新域名 > 旧门户 > 其它含 buct 的条目
ORIGIN_PREFERENCE = ("sidportal.buct.edu.cn", "portal.buct.edu.cn",
                     "experimental-auth-endpoint.buct.edu.cn",
                     "course.buct.edu.cn", "buct.edu.cn")

try:
    import win32crypt
except Exception:  # noqa: BLE001
    win32crypt = None


# ---------------------------------------------------------------- AES-256-GCM
_BCRYPT = ctypes.WinDLL("bcrypt") if hasattr(ctypes, "WinDLL") else None
_GCM_INFO_VERSION = 1


class _GCM_INFO(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO（x64 下 sizeof = 88）。"""
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def _u8(data: bytes):
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


def _bcrypt_ok(status: int, what: str) -> None:
    if status != 0:
        raise OSError(f"bcrypt {what} 失败，NTSTATUS=0x{status & 0xFFFFFFFF:08X}")


def aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """解密 `v10` 格式的 Chromium/Edge 密码。"""
    if not blob.startswith(b"v10") and not blob.startswith(b"v11"):
        raise ValueError("不是 v10/v11 格式（可能是 v20 app-bound，本模块不支持）")
    payload = blob[3:]
    if len(payload) < 12 + 16:
        raise ValueError("密文长度异常")
    nonce, tag, ct = payload[:12], payload[-16:], payload[12:-16]

    h_alg = ctypes.c_void_p()
    _bcrypt_ok(_BCRYPT.BCryptOpenAlgorithmProvider(
        ctypes.byref(h_alg), ctypes.c_wchar_p("AES"), None, 0), "OpenAlgorithm")

    try:
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        _bcrypt_ok(_BCRYPT.BCryptSetProperty(
            h_alg, ctypes.c_wchar_p("ChainingMode"),
            (ctypes.c_ubyte * len(mode)).from_buffer_copy(mode),
            len(mode), 0), "SetProperty")

        # 取密钥对象所需缓冲区大小
        obj_len = wintypes.ULONG()
        got = wintypes.ULONG()
        _bcrypt_ok(_BCRYPT.BCryptGetProperty(
            h_alg, ctypes.c_wchar_p("ObjectLength"),
            ctypes.byref(obj_len), ctypes.sizeof(obj_len),
            ctypes.byref(got), 0), "GetProperty")

        obj_buf = (ctypes.c_ubyte * obj_len.value)()
        h_key = ctypes.c_void_p()
        _bcrypt_ok(_BCRYPT.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), obj_buf, obj_len.value,
            (ctypes.c_ubyte * len(key)).from_buffer_copy(key), len(key), 0),
            "GenerateSymmetricKey")
        try:
            tag_buf = _u8(tag)
            nonce_buf = _u8(nonce)
            mac_buf = (ctypes.c_ubyte * 16)()
            info = _GCM_INFO()
            info.cbSize = ctypes.sizeof(_GCM_INFO)
            info.dwInfoVersion = _GCM_INFO_VERSION
            info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
            info.cbNonce = len(nonce)
            info.pbAuthData = None
            info.cbAuthData = 0
            info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
            info.cbTag = 16
            info.pbMacContext = ctypes.cast(mac_buf, ctypes.POINTER(ctypes.c_ubyte))
            info.cbMacContext = 16
            info.cbAAD = 0
            info.cbData = len(ct)
            info.dwFlags = 0

            out_len = wintypes.ULONG()
            out_buf = (ctypes.c_ubyte * (len(ct) + 16))()
            ct_buf = _u8(ct) if ct else (ctypes.c_ubyte * 1)()
            _bcrypt_ok(_BCRYPT.BCryptDecrypt(
                h_key, ctypes.cast(ct_buf, ctypes.POINTER(ctypes.c_ubyte)), len(ct),
                ctypes.byref(info), None, 0,
                ctypes.cast(out_buf, ctypes.POINTER(ctypes.c_ubyte)),
                len(out_buf), ctypes.byref(out_len), 0), "Decrypt")
            return bytes(bytearray(out_buf)[:out_len.value])
        finally:
            _BCRYPT.BCryptDestroyKey(h_key)
    finally:
        _BCRYPT.BCryptCloseAlgorithmProvider(h_alg, 0)


# ---------------------------------------------------------------- 取凭据
def _master_key(local_state: Path) -> bytes:
    if win32crypt is None:
        raise RuntimeError("缺少 pywin32（win32crypt），无法解密 Edge 密码库")
    state = json.loads(local_state.read_text(encoding="utf-8"))
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc.startswith(b"DPAPI"):
        enc = enc[5:]
    ok, key = win32crypt.CryptUnprotectData(enc, None, None, None, 0)
    if not ok:
        raise RuntimeError("DPAPI 解出主密钥失败")
    return key


def _read_logins(login_db: Path) -> list:
    """复制一份 Login Data 再读（Edge 运行时数据库被占用）。"""
    if not login_db.exists():
        return []
    tmp = Path(tempfile.gettempdir()) / f"_buct_login_data_{abs(hash(str(login_db))) % 10**8}.db"
    try:
        shutil.copy2(login_db, tmp)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    try:
        con = sqlite3.connect(str(tmp))
        for origin, user, blob in con.execute(
                "SELECT origin_url, username_value, password_value FROM logins"):
            rows.append((origin or "", user or "", blob))
        con.close()
    except Exception:  # noqa: BLE001
        return []
    finally:
        tmp.unlink(missing_ok=True)
    return rows


def _profile_login_dbs() -> list:
    """-> [(login_db 路径, local_state 路径)]，按优先级。"""
    out = []
    for prof in _candidate_profiles():
        ls = prof / "Local State"
        for db in (prof / "Default" / "Login Data", prof / "Login Data"):
            if db.exists():
                if not ls.exists():
                    ls = prof.parent / "Local State"
                if ls.exists():
                    out.append((db, ls))
                break
    return out


def _rank(origin: str) -> int:
    for i, frag in enumerate(ORIGIN_PREFERENCE):
        if frag in origin:
            return i
    return len(ORIGIN_PREFERENCE)


def get_saved_credentials(verbose: bool = False):
    """返回 (username, password)；拿不到返回 (None, None)。

    依次看每个 Edge 用户目录里北化相关的条目，取 origin 优先级最高的一条。
    """
    best = None                     # (rank, origin, user, pwd)
    for login_db, local_state in _profile_login_dbs():
        rows = [r for r in _read_logins(login_db) if r[1] and r[2]]
        rows = [r for r in rows if _rank(r[0]) < len(ORIGIN_PREFERENCE)]
        if not rows:
            continue
        rows.sort(key=lambda r: (_rank(r[0]), r[1]))
        origin, user, blob = rows[0]
        try:
            pwd = aes_gcm_decrypt(_master_key(local_state), blob).decode("utf-8")
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  凭据：解密失败（{login_db.parent.parent.name} / "
                      f"{type(e).__name__}: {e}）")
            continue
        cand = (_rank(origin), origin, user, pwd)
        if best is None or cand[0] < best[0]:
            best = cand
    if best is None:
        if verbose:
            print("  凭据：所有 Edge 用户目录里都没有可用的北化账号条目")
        return None, None
    _, origin, user, pwd = best
    if verbose:
        print(f"  凭据：从 Edge 密码库取到账号 {user}（origin={origin}，"
              f"密码 {len(pwd)} 位，只在内存中使用）")
    return user, pwd


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    print("GCM_INFO sizeof =", ctypes.sizeof(_GCM_INFO), "（x64 应为 88）")
    u, p = get_saved_credentials(verbose=True)
    print("username =", u)
    print("password =", ("*" * len(p)) if p else None)
