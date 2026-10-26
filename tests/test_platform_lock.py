# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

from helpers import DATA, ROOT, IsolatedCase

from platform_lock import FetchLock, LockTimeout


HOLDER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from platform_lock import FetchLock
with FetchLock(sys.argv[2], wait_sec=2):
    print('LOCKED', flush=True)
    time.sleep(float(sys.argv[3]))
"""

CRASHER = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from platform_lock import FetchLock
with FetchLock(sys.argv[2], wait_sec=2):
    print('LOCKED', flush=True)
    os._exit(7)
"""


class PlatformLockTests(IsolatedCase):
    def _start(self, script, lock_path, sleep="1.5"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(DATA)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(DATA), str(lock_path), sleep],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        line = proc.stdout.readline().strip()
        if line != "LOCKED":
            self.fail(proc.stderr.read())
        return proc

    def _stop(self, proc):
        # Windows venv python.exe can be a launcher with a child interpreter.
        # Let the bounded holder exit so its lock is released before cleanup.
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    def test_two_processes_mutual_exclusion_timeout_and_release(self):
        lock_path = self.root / "cross.lock"
        proc = self._start(HOLDER, lock_path, "1.2")
        try:
            with self.assertRaises(LockTimeout):
                with FetchLock(lock_path, wait_sec=0.25, poll_sec=0.05):
                    pass
        finally:
            self._stop(proc)
        with FetchLock(lock_path, wait_sec=1.0):
            pass

    def test_crash_releases_kernel_lock(self):
        lock_path = self.root / "crash.lock"
        proc = self._start(CRASHER, lock_path)
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 7)
        self._stop(proc)
        started = time.monotonic()
        with FetchLock(lock_path, wait_sec=1.0, poll_sec=0.05):
            pass
        self.assertLess(time.monotonic() - started, 0.9)

    def test_contention_message_never_pollutes_stdout_json(self):
        lock_path = self.root / "quiet.lock"
        proc = self._start(HOLDER, lock_path, "0.8")
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(LockTimeout):
                    with FetchLock(lock_path, wait_sec=0.15, poll_sec=0.05):
                        pass
        finally:
            self._stop(proc)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("占用", err.getvalue())

    def test_nested_lock_same_thread_fails_fast(self):
        lock_path = self.root / "nested.lock"
        with FetchLock(lock_path, wait_sec=1):
            with self.assertRaises(LockTimeout):
                with FetchLock(lock_path, wait_sec=0.1):
                    pass
