"""Persist task diagnostics without storing credentials or full result payloads."""
from contextvars import ContextVar
import os
import re
import time
from urllib.parse import urlsplit

import cache_store

current_task = ContextVar('console_task', default='')
TASK_ENV = 'HOMEWORK_RUN_TASK_ID'


def redact(value):
    text = str(value or '')

    def safe_url(match):
        try:
            url = urlsplit(match.group())
            return f'{url.scheme}://{url.hostname}{url.path}'
        except ValueError:
            return '[地址已隐藏]'
    text = re.sub(r'https?://[^\s<>"\']+', safe_url, text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,"\']+', 'Bearer [已隐藏]', text)
    return re.sub(
        r'(?i)\b(key|token|secret|password|passwd|authorization|cookie|api[_-]?key)\b'
        r'[\s"\']*[:=]\s*["\']?[^\s,;"\']+', r'\1=[已隐藏]', text)[:2000]


def emit(message, task_id=None):
    task_id = task_id or current_task.get() or os.environ.get(TASK_ENV)
    if not task_id or not str(message).strip():
        return
    try:
        with cache_store.connection(write=True) as con:
            con.execute('INSERT INTO task_events(task_id,at,message) VALUES(?,?,?)',
                        (task_id, time.time(), redact(message).strip()))
    except Exception:
        # Diagnostic storage must never turn a successful fetch into a failure.
        pass


def events(task_id, limit=100):
    with cache_store.connection() as con:
        rows = con.execute(
            'SELECT id,at,message FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?',
            (task_id, limit)).fetchall()
    return [{**dict(row), 'message': redact(row['message'])} for row in reversed(rows)]


class TaskStream:
    """Tee stderr inside the scraper process, preserving its stdout JSON."""
    def __init__(self, target, task_id):
        self.target = target
        self.task_id = task_id
        self.pending = ''

    def write(self, value):
        self.target.write(value)
        self.pending += value
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            emit(line, self.task_id)
        return len(value)

    def flush(self):
        self.target.flush()
        if self.pending:
            emit(self.pending, self.task_id)
            self.pending = ''

    def __getattr__(self, name):
        return getattr(self.target, name)
