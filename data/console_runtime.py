"""Local source version and bounded, idle-only backend restart support."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def disk_version(root=ROOT):
    digest = hashlib.sha256()
    files = sorted(root.glob('*.py')) + sorted(root.glob('*.js')) + [root / 'console.html']
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


RUNNING_VERSION = disk_version()


def status():
    disk = disk_version()
    return {'running_version':RUNNING_VERSION, 'disk_version':disk,
            'needs_update':disk != RUNNING_VERSION, 'pid':os.getpid()}


def launch_restart(port):
    # Refuse syntactically broken local updates while the old backend still works.
    for path in ROOT.glob('*.py'):
        compile(path.read_bytes(), str(path), 'exec')
    python = Path(sys.executable)
    hidden = python.with_name('pythonw.exe')
    subprocess.Popen([str(hidden if hidden.exists() else python),
        str(ROOT / 'restart_backend.py'), '--parent',str(os.getpid()), '--port',str(port)],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=0x08000000 if os.name == 'nt' else 0,
        close_fds=True)
