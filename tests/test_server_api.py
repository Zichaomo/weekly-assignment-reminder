# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from helpers import IsolatedCase

import cache_store


class ServerApiTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()
        cache_store.begin_scan("api-scan", mode="test", expected_lids=["1"],
                               requested_lids=["1"], tests_requested=False,
                               course_index_version="idx", scope=self.scope)
        cache_store.commit_course_scan(
            "api-scan", lid="1", course="课程1",
            homeworks=[{"hwtid": "7001", "title": "API 作业",
                        "deadline": "2026-12-31 23:59"}],
            tests=[], homework_status="ok", tests_status="disabled",
            index_version="idx", scope=self.scope)
        cache_store.update_scan("api-scan", status="complete", finished_at=time.time())
        item = cache_store.load_items(self.scope)[0]
        self.key = item["item_key"]
        asset = self.image_asset()
        self.asset_id = asset['asset_id']
        cache_store.save_content(self.key, {
            "status": "ready", "source_url": "https://course.buct.edu.cn/x",
            "source_hash": "h", "source_version": "v1",
            "blocks": [{"id": "p1", "type": "paragraph", "text": "正文"}],
            "asset_ids": [self.asset_id], "assets": [asset], "text": "正文"}, scope=self.scope)
        import server
        self.server_mod = server
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def view(self, payload):
        return self.post(f"/api/assignments/{quote(self.key, safe='')}/view", payload)

    def test_content_api_exposes_fields_needed_by_frontend(self):
        status, data = self.get(f"/api/assignments/{quote(self.key, safe='')}/content")
        self.assertEqual(status, 200)
        self.assertTrue(data["ready"])
        self.assertEqual(data["source_version"], "v1")
        self.assertEqual(data["blocks"][0]["text"], "正文")

    def test_task_poll_exposes_current_login_activity_from_persistent_events(self):
        import run_history
        task_id = 'renewing-login'
        cache_store.register_task(task_id, self.scope+'|fetch|renewing', 'fetch', {})
        cache_store.update_task(task_id, 'running', scan_id='api-scan')
        cache_store.register_task('another-task', self.scope+'|fetch|other', 'fetch', {})
        run_history.emit('另一任务正在执行', 'another-task')
        with patch.dict(self.server_mod.TASKS, {}, clear=True):
            for message in ('登录状态已过期，正在自动续登', '自动续登成功，正在重新检索课程'):
                run_history.emit(message, task_id)
                status, data = self.get('/api/tasks/'+task_id)
                self.assertEqual(status, 200)
                self.assertEqual(data.get('current_message'), message)
            cache_store.update_task(task_id, 'error', result_summary={'error': '自动续登失败，请重新登录'})
            _, data = self.get('/api/tasks/'+task_id)
            self.assertFalse(data.get('current_message'))

    def test_task_poll_redacts_current_activity_and_supports_no_events(self):
        import run_history
        task_id = 'activity-redaction'
        cache_store.register_task(task_id, self.scope+'|fetch|redaction', 'fetch', {})
        cache_store.update_task(task_id, 'running')
        with patch.dict(self.server_mod.TASKS, {}, clear=True):
            _, data = self.get('/api/tasks/'+task_id)
            self.assertEqual(data.get('current_message'), '')
            run_history.emit('正在自动续登 token=fixture-private-value', task_id)
            _, data = self.get('/api/tasks/'+task_id)
            self.assertIn('正在自动续登', data.get('current_message', ''))
            self.assertNotIn('fixture-private-value', json.dumps(data))

    def test_runs_api_recovers_history_without_exposing_task_params(self):
        cache_store.register_task('history-api', self.scope+'|fetch|api', 'fetch', {'key':'private-fixture'})
        cache_store.update_task('history-api', 'error', result_summary={'error':'fixture timeout key=secret-fixture'})
        status, data = self.get('/api/runs?limit=1')
        self.assertEqual(status, 200)
        self.assertEqual(data['history'][0]['task_id'], 'history-api')
        self.assertNotIn('private-fixture', json.dumps(data))
        self.assertNotIn('secret-fixture', json.dumps(data))
        self.assertEqual(self.get('/api/runs?before=invalid')[0], 400)

    def test_local_mutation_result_appears_in_history(self):
        with patch.object(self.server_mod, 'manual_action', return_value={'ok':False,'error':'fixture invalid deadline'}):
            self.post('/api/manual', {'action':'add'})
        status, data = self.get('/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(data['history'][0]['op'], '手动作业')
        self.assertEqual(data['history'][0]['status'], 'error')
        self.assertEqual(data['history'][0]['summary']['error'], 'fixture invalid deadline')

    def test_connected_entry_and_local_resources_and_traversal(self):
        for path, content in [('/',b'/ui/'),('/ui/js/icons.js',b'renderLocalIcons'),
                              ('/ui/css/connected.css',b'100dvh'),('/legacy',b'tbody')]:
            with urlopen(self.base+path,timeout=5) as response:
                self.assertEqual(response.status,200)
                self.assertIn(content,response.read())
        status,_=self.get('/ui/%2e%2e/course_rules.js')
        self.assertEqual(status,404)

    def test_preview_finishes_while_platform_executor_is_occupied(self):
        release = threading.Event()
        entered = threading.Event()
        def platform_job():
            entered.set()
            release.wait(5)
        with ThreadPoolExecutor(max_workers=1) as platform, ThreadPoolExecutor(max_workers=1) as previews:
            blocked = platform.submit(platform_job)
            self.assertTrue(entered.wait(1))
            try:
                with patch.object(self.server_mod,'EXEC',platform), patch.object(self.server_mod,'PREVIEW_EXEC',previews), patch.object(self.server_mod.hub,'run_publish',return_value={'ok':True,'sent':False,'message':'local preview'}) as publish:
                    status, task = self.post('/api/tasks',{'op':'preview','days':7})
                    self.assertEqual(status,200)
                    deadline = time.monotonic()+2
                    while time.monotonic()<deadline:
                        _, result = self.get('/api/tasks/'+task['task_id'])
                        if result['status'] not in ('queued','running'):
                            break
                        time.sleep(.02)
                    self.assertEqual(result['status'],'done')
                    self.assertEqual(result['result']['message'],'local preview')
                    self.assertFalse(blocked.done(), 'preview must finish before platform work')
                    publish.assert_called_once_with(7,True,None)
            finally:
                release.set()

    def test_course_rules_api_preview_save_conflict_and_reset(self):
        status, state = self.get('/api/course-rules')
        self.assertEqual(status, 200)
        rule = dict(id='api-rule', course='课程1', match='exact', mode='full', lines=1)
        status, preview = self.post('/api/course-rules/preview', {'rule':rule, 'item_key':self.key})
        self.assertEqual(status, 200)
        self.assertEqual(preview['push_text'], '正文')
        status, saved = self.post('/api/course-rules', {'action':'save', 'rule':rule,
                                                      'expected_revision':state['revision']})
        self.assertEqual(status, 200)
        status, _ = self.post('/api/course-rules', {'action':'save', 'rule':rule,
                                                   'expected_revision':state['revision']})
        self.assertEqual(status, 409)
        status, _ = self.post('/api/course-rules', {'action':'reset', 'id':rule['id'],
                                                   'expected_revision':saved['revision']})
        self.assertEqual(status, 200)

    def test_publish_api_recovers_refused_proxy_for_text_and_supplement_images(self):
        import base64
        import errno
        import io
        from pathlib import Path
        from datetime import datetime, timedelta
        from unittest.mock import Mock
        from urllib.error import URLError
        import assignment_supplements
        import notify
        supplement = cache_store.get_item(self.key)['supplement']
        asset = cache_store.asset_record(self.asset_id)
        assignment_supplements.save(self.key, {
            'expected_revision': supplement['revision'],
            'expected_identity_generation': supplement['identity_generation'], 'text': '',
            'images': [{'data': base64.b64encode(Path(asset['path']).read_bytes()).decode()}]})
        with cache_store.connection(write=True) as con:
            con.execute('UPDATE assignments SET deadline=? WHERE item_key=?',
                        ((datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M'), self.key))
            con.execute('UPDATE assets SET source_url=? WHERE asset_id=?',
                        ('https://images.unit.invalid/supplement.png', self.asset_id))
        item = cache_store.get_item(self.key)
        config = {'channel': 'wecom', 'wecom_webhook': 'https://unit.invalid/class',
                  'personal': {'enabled': True, 'channel': 'wecom', 'wecom_webhook': 'https://unit.invalid/personal'}}
        direct = Mock()
        payloads = []
        def response(request, **kwargs):
            if request.get_method() == 'GET':
                return io.BytesIO(Path(asset['path']).read_bytes())
            payloads.append(json.loads(request.data))
            return io.BytesIO(b'{"errcode":0}')
        direct.open.side_effect = response
        client_opener = notify.urllib.request.build_opener(notify.urllib.request.ProxyHandler({}))
        hub = self.server_mod.hub
        with patch.object(hub.weekly, 'load_fresh_publish_cache', return_value=({'homeworks': [item]}, {'publishable': True})), \
             patch.object(hub.weekly, 'load_manual', return_value=[]), \
             patch.object(hub.weekly, '_elective_lids_from_disk', return_value=set()), \
             patch.object(hub, 'ROOT', self.root), patch.object(hub.fetch_log, 'log_fetch'), \
             patch.object(notify, 'load_config', return_value=config), \
             patch.object(notify, 'PUSH_LOG', self.root / 'push.jsonl'), \
             patch.object(notify, 'PART_LOG', self.root / 'parts.jsonl'), \
             patch.object(notify.urllib.request, '_opener', client_opener), \
             patch.object(notify.time, 'sleep'), patch.dict('os.environ', {notify.WECOM_ENV: ''}), \
             patch.object(notify.urllib.request, 'getproxies', return_value={'https': 'http://127.0.0.1:7890'}), \
             patch.object(notify.urllib.request, 'proxy_bypass', return_value=False), \
             patch.object(notify.urllib.request, 'urlopen', side_effect=URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'fixture proxy'))), \
             patch.object(notify.urllib.request, 'build_opener', return_value=direct):
            status, submitted = self.post('/api/tasks', {'op': 'publish', 'days': 7})
            self.assertEqual(status, 200)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, task = self.get('/api/tasks/' + submitted['task_id'])
                if task['status'] not in ('queued', 'running'):
                    break
                time.sleep(.02)
            self.assertEqual(task['status'], 'done')
            self.assertTrue(task['result']['ok'])
            self.assertTrue(task['result']['sent'])
            self.assertTrue(task['result']['personal']['sent'])
        self.assertEqual(sum(payload['msgtype'] == 'markdown_v2' for payload in payloads), 2)
        self.assertEqual(len(payloads), 2)

    def test_course_rule_samples_report_long_bodies_and_clear_after_rule_save(self):
        cache_store.save_content(self.key, {'status': 'ready', 'source_version': 'long-body',
            'blocks': [{'id': 'p1', 'type': 'paragraph', 'text': '甲\n乙\n丙\n丁'}],
            'text': '甲\n乙\n丙\n丁'}, scope=self.scope)
        _, samples = self.get('/api/course-rules/samples')
        sample = next(row for row in samples['items'] if row['item_key'] == self.key)
        self.assertTrue(sample['needs_rule'])
        self.assertEqual(sample['line_count'], 4)
        _, state = self.get('/api/course-rules')
        rule = dict(id='long-rule', course='课程1', match='exact', mode='full', lines=4)
        self.assertEqual(self.post('/api/course-rules', {'action': 'save', 'rule': rule,
            'expected_revision': state['revision']})[0], 200)
        _, samples = self.get('/api/course-rules/samples')
        self.assertFalse(next(row for row in samples['items'] if row['item_key'] == self.key)['needs_rule'])

    def test_version_conflict_is_409_and_bad_geometry_is_400(self):
        status, data = self.view({"expected_version": "old", "regions": []})
        self.assertEqual(status, 409)
        self.assertEqual(data["code"], "version_conflict")
        status, _ = self.view({"expected_version": "v1", "regions": [
            {"asset_id": self.asset_id, "box": [0.9, 0.1, 0.2, 0.8]}]})
        self.assertEqual(status, 400)

    def test_asset_from_another_version_is_400(self):
        status, _ = self.view({"expected_version": "v1", "regions": [
            {"asset_id": "d" * 64, "box": [0.1, 0.1, 0.8, 0.8]}]})
        self.assertEqual(status, 400)

    def test_valid_multiregion_crop_persists(self):
        regions = [
            {"asset_id": self.asset_id, "box": [0.1, 0.1, 0.5, 0.5]},
            {"asset_id": self.asset_id, "box": [0.2, 0.6, 0.9, 0.95]},
        ]
        status, data = self.view({"expected_version": "v1", "regions": regions})
        self.assertEqual(status, 200, data)
        status, data = self.get(f"/api/assignments/{quote(self.key, safe='')}/content")
        self.assertEqual(len(data["view"]["regions"]), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
