# -*- coding: utf-8 -*-
"""Strict cross-process lock backed by an OS file lock.

The lock lives on one stable file.  Windows uses ``msvcrt.locking`` and Unix
uses ``flock``; both are released by the kernel when the process exits, so a
crash cannot leave a claimed lock behind.  There is deliberately no
age-based deletion and no directory rename/reclaim race.

All platform-facing entry points share the same default path.  A nested lock
in the same thread fails immediately instead of deadlocking.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_LOCK = Path(os.environ.get("HOMEWORK_LOCK_PATH")
                    or (ROOT / "platform.lock"))

_HELD = threading.local()

if os.name == "nt":
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class OVERLAPPED(_ctypes.Structure):
        _fields_ = [
            ("Internal", _ctypes.c_void_p),
            ("InternalHigh", _ctypes.c_void_p),
            ("Offset", _wintypes.DWORD),
            ("OffsetHigh", _wintypes.DWORD),
            ("hEvent", _wintypes.HANDLE),
        ]
else:
    OVERLAPPED = None


class LockTimeout(RuntimeError):
    pass


def _win_handles():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32


def process_info(pid: int) -> tuple[str, int | None]:
    """Return ('alive'|'dead'|'unknown', process_start_stamp)."""
    if not pid:
        return ("unknown", None)
    if os.name == "nt":
        try:
            ctypes, wintypes, kernel32 = _win_handles()
            handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
            if not handle:
                return ("alive", None) if ctypes.get_last_error() == 5 else ("dead", None)
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return ("unknown", None)
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                stamp = None
                if kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                            ctypes.byref(exited),
                                            ctypes.byref(kernel),
                                            ctypes.byref(user)):
                    stamp = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                return ("alive" if int(code.value) == 259 else "dead", stamp)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return ("unknown", None)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return ("dead", None)
    except PermissionError:
        return ("alive", None)
    except OSError:
        return ("unknown", None)
    stamp = None
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        stamp = int(stat.split()[21])
    except Exception:  # noqa: BLE001
        pass
    return ("alive", stamp)


def _lock_fd(fd: int):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
        ]
        kernel32.LockFileEx.restype = wintypes.BOOL
        overlapped = OVERLAPPED()
        handle = msvcrt.get_osfhandle(fd)
        flags = 0x00000001 | 0x00000002  # FAIL_IMMEDIATELY | EXCLUSIVE
        if not kernel32.LockFileEx(handle, flags, 0, 1, 0,
                                   ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error in (33, 158):  # LOCK_VIOLATION, WAIT_TIMEOUT
                raise BlockingIOError(error, "lock is held")
            raise OSError(error, "LockFileEx failed")
        return overlapped
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return None


def _unlock_fd(fd: int, state=None) -> None:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.UnlockFileEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
        ]
        kernel32.UnlockFileEx.restype = wintypes.BOOL
        overlapped = state or OVERLAPPED()
        if not kernel32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0,
                                     ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error != 158:  # ERROR_NOT_LOCKED is harmless on cleanup
                raise OSError(error, "UnlockFileEx failed")
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_owner(fd: int, owner: dict) -> None:
    payload = json.dumps(owner, ensure_ascii=False, indent=2).encode("utf-8")
    old_size = os.fstat(fd).st_size
    os.lseek(fd, 1, os.SEEK_SET)
    os.write(fd, payload)
    # Do not truncate while the first byte is locked: Windows can treat file
    # truncation as a lock boundary change.  Overwrite stale bytes with spaces.
    if old_size > 1 + len(payload):
        os.write(fd, b" " * (old_size - 1 - len(payload)))


def _read_owner(fd: int) -> dict:
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        chunks = []
        while True:
            block = os.read(fd, 4096)
            if not block:
                break
            chunks.append(block)
        return json.loads(b"".join(chunks).decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001
        return {}


class FetchLock:
    """``with FetchLock(wait_sec=120): ...``; raises LockTimeout instead of proceeding."""

    def __init__(self, path: Path | str | None = None, wait_sec: float = 120.0,
                 poll_sec: float = 0.15):
        self.path = Path(path or DEFAULT_LOCK).resolve()
        self.wait_sec = max(0.0, float(wait_sec))
        self.poll_sec = max(0.05, float(poll_sec))
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._fd: int | None = None
        self._lock_state = None

    def _same_thread_holds(self) -> bool:
        held = getattr(_HELD, "paths", None) or set()
        return str(self.path) in held

    def _mark_thread(self, add: bool) -> None:
        held = getattr(_HELD, "paths", None)
        if held is None:
            held = set()
            _HELD.paths = held
        key = str(self.path)
        if add:
            held.add(key)
        else:
            held.discard(key)

    def __enter__(self):
        if self._same_thread_holds():
            raise LockTimeout("同一线程重复申请平台锁；入口不得嵌套加锁")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + self.wait_sec
        warned = False
        while True:
            try:
                self._lock_state = _lock_fd(fd)
                break
            except (BlockingIOError, OSError) as exc:
                if not isinstance(exc, BlockingIOError) and os.name != "nt":
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    owner = _read_owner(fd)
                    os.close(fd)
                    raise LockTimeout(
                        f"平台正被另一个任务占用（PID {owner.get('pid', '?')}），"
                        f"等待 {self.wait_sec:.0f} 秒仍未释放；本次未执行")
                if not warned:
                    warned = True
                    print(f"注意：平台正被另一个任务占用，等待中（最多 {self.wait_sec:.0f} 秒）...",
                          file=sys.stderr)
                time.sleep(self.poll_sec)
        owner = {
            "token": self.token,
            "pid": os.getpid(),
            "process_started": process_info(os.getpid())[1],
            "created_at": time.time(),
            "host": os.environ.get("COMPUTERNAME") or "",
        }
        _write_owner(fd, owner)
        self._fd = fd
        self.acquired = True
        self._mark_thread(True)
        return self

    def __exit__(self, *exc):
        if not self.acquired:
            return False
        try:
            if self._fd is not None:
                _unlock_fd(self._fd, self._lock_state)
        finally:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd = None
            self._lock_state = None
            self.acquired = False
            self._mark_thread(False)
        return False


def platform_lock(wait_sec: float = 120.0) -> FetchLock:
    return FetchLock(wait_sec=wait_sec)
