"""Detached restart worker. Wait for the old listener, then verify the new one."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from platform_lock import process_info

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--parent',type=int,required=True)
    ap.add_argument('--port',type=int,required=True)
    args = ap.parse_args()
    if not 1024 <= args.port <= 65535:
        return 1
    deadline = time.monotonic()+25
    while time.monotonic() < deadline:
        if process_info(args.parent)[0] == 'dead':
            break
        time.sleep(.2)
    else:
        return 2
    env = dict(os.environ, HOMEWORK_CONSOLE_PORT=str(args.port))
    with (ROOT / 'server_console.log').open('ab') as log:
        child = subprocess.Popen([sys.executable,'-u',str(ROOT/'server.py')], cwd=str(ROOT),
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=0x08000000 if os.name=='nt' else 0,close_fds=True)
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        if child.poll() is not None:
            return 3
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/runtime',timeout=1) as r:
                state=json.load(r)
            if state.get('pid')!=args.parent and not state.get('needs_update'):
                return 0
        except (OSError,ValueError):
            pass
        time.sleep(.3)
    return 4


if __name__=='__main__':
    raise SystemExit(main())
