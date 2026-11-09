import io
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from helpers import IsolatedCase
import cache_store


class RunHistoryTests(IsolatedCase):
    def scan_with_counts(self, scan_id, *, done, failed=0, status='complete', lids=None):
        lids = lids or [str(number) for number in range(1, 11)]
        cache_store.begin_scan(scan_id, mode='test', expected_lids=lids,
                               tests_requested=True, course_index_version='idx')
        for number, lid in enumerate(lids[:done+failed]):
            cache_store.commit_course_scan(
                scan_id, lid=lid, course='课程'+lid, homeworks=[], tests=[],
                homework_status='empty' if number < done else 'error',
                tests_status='empty', index_version='idx')
        cache_store.update_scan(scan_id, status=status, finished_at=time.time())

    def test_completed_history_keeps_10_of_10_after_a_new_scan(self):
        import server
        self.use_explicit_account()
        self.scan_with_counts('history-full', done=10)
        cache_store.register_task('old-full', cache_store.account_scope()+'|fetch|old-full', 'fetch', {})
        cache_store.update_task('old-full', 'done', scan_id='history-full')
        self.scan_with_counts('new-full', done=10)
        with patch.dict(server.TASKS, {}, clear=True):
            history = server.run_history_snapshot()['history'][0]
        self.assertEqual(history['progress']['done'], 10)
        self.assertEqual(history['progress']['total'], 10)
        self.assertEqual(history['progress']['failed'], 0)

    def test_partial_history_keeps_actual_success_and_failure_counts(self):
        self.use_explicit_account()
        self.scan_with_counts('history-partial', done=6, failed=3, status='partial')
        self.scan_with_counts('new-after-partial', done=10)
        progress = cache_store.scan_progress('history-partial')
        self.assertEqual((progress['done'], progress['failed'], progress['total']), (6, 3, 10))

    def test_interrupted_history_survives_subset_retry(self):
        self.use_explicit_account()
        self.scan_with_counts('history-interrupted', done=3, failed=1, status='error')
        self.scan_with_counts('subset-retry', done=1, lids=['1'])
        progress = cache_store.scan_progress('history-interrupted')
        self.assertEqual((progress['done'], progress['failed'], progress['total']), (3, 1, 10))
        current = cache_store.scan_progress('subset-retry')
        self.assertEqual((current['done'], current['failed'], current['total']), (1, 0, 1))

    def test_redaction_removes_bearer_credentials_and_url_userinfo(self):
        import run_history
        message = run_history.redact('Authorization: Bearer fixture-credential https://name:password@example.com/path?key=fixture-query')
        self.assertNotIn('fixture-credential', message)
        self.assertNotIn('name:password', message)
        self.assertNotIn('fixture-query', message)

    def test_retry_keeps_original_start_time(self):
        cache_store.register_task('retry', cache_store.account_scope()+'|fetch|r', 'fetch', {})
        cache_store.update_task('retry', 'running')
        first = cache_store.task_record('retry')['started_at']
        cache_store.update_task('retry', 'running', scan_id='new-scan')
        self.assertEqual(cache_store.task_record('retry')['started_at'], first)

    def test_child_scraper_stderr_reaches_history_before_exit(self):
        import run_history
        import weekly
        self.use_explicit_account()
        cache_store.register_task('child', cache_store.account_scope()+'|fetch|c', 'fetch', {})
        script = self.root / 'scrape.py'
        script.write_text('''import os, sys, time
import run_history
stream = run_history.TaskStream(sys.stderr, os.environ[run_history.TASK_ENV])
stream.write('正在读取模拟课程\\n')
stream.flush()
time.sleep(.4)
print('{"ok":true}')
''', encoding='utf-8')
        ready = threading.Event()
        def child():
            token = run_history.current_task.set('child')
            try:
                weekly.fetch()
            finally:
                run_history.current_task.reset(token)
                ready.set()
        env = {**weekly.UTF8_ENV, **self.env, 'PYTHONPATH': str(Path(run_history.__file__).parent)}
        with patch.object(weekly, 'ROOT', self.root), patch.object(weekly, 'UTF8_ENV', env):
            worker = threading.Thread(target=child)
            worker.start()
            deadline = time.monotonic()+3
            while not run_history.events('child') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(run_history.events('child')[0]['message'], '正在读取模拟课程')
            self.assertFalse(ready.is_set(), 'progress must be available before the child exits')
            worker.join(4)
            self.assertTrue(ready.is_set())

    def test_history_survives_memory_reset_and_is_account_scoped(self):
        import server
        scope = self.use_explicit_account()
        cache_store.register_task('old', scope + '|fetch|old', 'fetch', {})
        cache_store.update_task('old', 'error', result_summary={'error': '连接超时'})
        cache_store.register_task('current', scope + '|content|now', 'content', {})
        cache_store.update_task('current', 'running')
        cache_store.register_task('other', 'other-account|fetch|x', 'fetch', {})
        with patch.dict(server.TASKS, {}, clear=True):
            result = server.run_history_snapshot(limit=1)
        self.assertEqual([x['task_id'] for x in result['active']], ['current'])
        self.assertEqual([x['task_id'] for x in result['history']], ['old'])
        self.assertEqual(result['history'][0]['summary']['error'], '连接超时')

    def test_worker_output_is_live_persistent_and_redacted(self):
        import server
        import run_history
        self.use_explicit_account()
        started, finish = threading.Event(), threading.Event()
        def work(op, params):
            run_history.emit('正在获取课程列表 key=private-key https://example.com/a?token=secret')
            started.set()
            finish.wait(3)
            return {'ok': False, 'error': '课程加载失败'}
        with patch.object(server, 'run_task', side_effect=work):
            task_id, _ = server.submit_task('fetch', {})
            try:
                self.assertTrue(started.wait(3))
                running = server.run_history_snapshot()['active'][0]
                self.assertEqual(running['status'], 'running')
                self.assertIn('正在获取课程列表', running['events'][-1]['message'])
                self.assertNotIn('private-key', json.dumps(running))
                self.assertNotIn('token=secret', json.dumps(running))
            finally:
                finish.set()
            deadline = time.monotonic() + 4
            while cache_store.task_record(task_id)['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.02)
        with patch.dict(server.TASKS, {}, clear=True):
            history = server.run_history_snapshot()['history'][0]
        self.assertEqual(history['summary']['error'], '课程加载失败')
        self.assertGreaterEqual(history['elapsed_sec'], 0)
        self.assertGreaterEqual(len(history['events']), 2)

    def test_stderr_tee_preserves_console_and_handles_split_lines(self):
        import run_history
        self.use_explicit_account()
        cache_store.register_task('stream', cache_store.account_scope()+'|fetch|s', 'fetch', {})
        target = io.StringIO()
        stream = run_history.TaskStream(target, 'stream')
        stream.write('开始'); stream.write('检索\n下一门')
        stream.flush()
        self.assertEqual(target.getvalue(), '开始检索\n下一门')
        messages = [e['message'] for e in run_history.events('stream')]
        self.assertEqual(messages, ['开始检索', '下一门'])

    def test_history_pagination_keeps_active_tasks(self):
        import server
        self.use_explicit_account()
        for number in range(3):
            task_id = str(number)
            cache_store.register_task(task_id, cache_store.account_scope()+'|preview|'+task_id, 'preview', {})
            cache_store.update_task(task_id, 'done')
        first = server.run_history_snapshot(limit=2)
        second = server.run_history_snapshot(limit=2, before=first['next_cursor'])
        self.assertEqual(len(first['history']), 2)
        self.assertEqual(len(second['history']), 1)
        self.assertEqual(len({x['task_id'] for x in first['history']+second['history']}), 3)
