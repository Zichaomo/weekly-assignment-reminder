"""Local-only acceptance for per-assignment text and screenshot supplements."""
import base64
import json
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import patch

from helpers import IsolatedCase
import cache_store
import hub
import image_delivery
import weekly


class AssignmentSupplementTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.scope = self.use_explicit_account()
        cache_store.begin_scan('supplement', mode='test', expected_lids=['1'],
                               requested_lids=['1'], tests_requested=False,
                               course_index_version='idx')
        cache_store.commit_course_scan('supplement', lid='1', course='数字电路',
            homeworks=[{'hwtid': '1', 'title': '第一周作业',
                        'deadline': (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')}],
            tests=[{'test_id': '2', 'title': '测试'}], homework_status='ok',
            tests_status='ok', index_version='idx')
        self.key = next(x['item_key'] for x in cache_store.load_items() if x['kind'] == 'homework')
        self.body('v1')
        import server
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.url = '/api/assignments/' + quote(self.key, safe='') + '/supplement'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def body(self, version):
        cache_store.save_content(self.key, {'status': 'ready', 'source_version': version,
            'text': '第一周作业提交这里',
            'blocks': [{'id': 'p1', 'type': 'paragraph', 'text': '第一周作业提交这里'}]})

    def request(self, payload=None, url=None):
        req = Request(self.base + (url or self.url),
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def state(self):
        status, data = self.request()
        self.assertEqual(status, 200)
        return data['supplement']

    def save(self, text='PPT 第 12 页，完成 1-3、1-5', images=None, state=None):
        current = self.state() if state is None else state
        return self.request({'expected_revision': current['revision'],
            'expected_identity_generation': current['identity_generation'],
            'text': text, 'images': images or []})

    def upload(self):
        asset = self.image_asset()
        from pathlib import Path
        return {'data': base64.b64encode(Path(asset['path']).read_bytes()).decode()}

    def test_save_edit_clear_and_survive_platform_refresh(self):
        original = self.state()
        self.assertEqual(self.save()[0], 200)
        self.body('v2')
        current = self.state()
        self.assertIn('PPT', current['text'])
        self.assertNotEqual(current['revision'], original['revision'])
        row = cache_store.get_item(self.key)
        self.assertEqual(row['content_text'], '第一周作业提交这里')
        rows = hub.build_rows([row], [], 7)
        self.assertEqual(rows[0]['supplement']['text'], current['text'])
        self.assertEqual(self.save(text='补做 1-8')[0], 200)
        message = weekly.format_message([cache_store.get_item(self.key)], 7)
        self.assertIn('补充要求', message)
        self.assertIn('补做 1-8', message)
        self.assertEqual(message.count('• 数字电路'), 1)
        self.assertEqual(self.save(text='')[0], 200)
        self.assertEqual(self.state()['text'], '')
        self.assertNotIn('补做 1-8', weekly.format_message([cache_store.get_item(self.key)], 7))

    def test_screenshot_delivery_and_stale_preview_rejection(self):
        self.assertEqual(self.save(images=[self.upload()])[0], 200)
        row = cache_store.get_item(self.key)
        plan = image_delivery.collect([row])
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]['supplement'])
        raw = image_delivery.delivery_bytes(plan[0])
        self.assertTrue(raw.startswith(b'\x89PNG'))
        message = weekly.format_message([row], 7)
        self.assertIn('补充图片随提醒发送（1 张）', message)
        with patch.object(image_delivery.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')):
            self.assertIsNone(image_delivery.inline_markdown(message, plan))
        retained = [{'asset_id': self.state()['images'][0]['asset_id']}]
        self.assertEqual(self.save(text='已修改', images=retained)[0], 200)
        with self.assertRaises(ValueError):
            image_delivery.delivery_bytes(plan[0])
        self.assertEqual(self.save(text='')[0], 200)
        self.assertEqual(image_delivery.collect([cache_store.get_item(self.key)]), [])

    def test_missing_or_expired_platform_body_keeps_supplement_usable(self):
        cache_store.record_content_error(self.key, 'fixture failure')
        self.assertEqual(self.save(images=[self.upload()])[0], 200)
        row = cache_store.get_item(self.key)
        self.assertIn('PPT 第 12 页', weekly.format_message([row], 7))
        self.assertEqual(len(image_delivery.collect([row])), 1)

    def test_concurrent_edit_and_clear_cannot_overwrite_newer_data(self):
        old = self.state()
        self.assertEqual(self.save(state=old)[0], 200)
        self.assertEqual(self.save(text='覆盖', state=old)[0], 409)
        saved = self.state()
        self.assertEqual(self.save(text='')[0], 200)
        self.assertEqual(self.save(text='旧草稿', state=saved)[0], 409)
        self.assertEqual(self.state()['text'], '')

    def test_account_switch_hides_supplement_and_rejects_old_draft(self):
        cache_store.bind_identity('first')
        self.assertEqual(self.save(images=[self.upload()])[0], 200)
        old = self.state()
        plan = image_delivery.collect([cache_store.get_item(self.key)])
        cache_store.bind_identity('second', allow_change=True)
        self.assertEqual(self.state()['text'], '')
        self.assertEqual(self.state()['images'], [])
        self.assertEqual(self.save(state=old)[0], 409)
        self.assertEqual(image_delivery.collect([cache_store.get_item(self.key)]), [])
        with self.assertRaises(ValueError):
            image_delivery.delivery_bytes(plan[0])
        with patch.dict('os.environ', {'HOMEWORK_ACCOUNT_SCOPE': 'other-account'}):
            self.assertEqual(self.request()[0], 404)

    def test_invalid_images_text_and_foreign_asset_leave_state_unchanged(self):
        current = self.state()
        for images in [[{'data': 'invalid'}], [{'data': base64.b64encode(b'fake png').decode()}],
                       [{'asset_id': []}],
                       [{'asset_id': self.image_asset()['asset_id']}], [self.upload()] * 7]:
            self.assertEqual(self.save(images=images)[0], 400)
            self.assertEqual(self.state(), current)
        self.assertEqual(self.save(text='x' * 4001)[0], 400)
        self.assertEqual(self.save(text={'html': 'invalid'})[0], 400)
        self.assertEqual(self.state(), current)

    def test_test_items_and_unknown_items_reject_supplements(self):
        test = next(x for x in cache_store.load_items() if x['kind'] == 'test')
        url = '/api/assignments/' + quote(test['item_key'], safe='') + '/supplement'
        self.assertEqual(self.request(url=url)[0], 400)
        self.assertEqual(self.request(url='/api/assignments/missing/supplement')[0], 404)

    def test_oversize_image_is_rejected(self):
        self.assertEqual(self.save(images=[{'data': base64.b64encode(b'x' * (2 * 1024 * 1024 + 1)).decode()}])[0], 400)

    def test_normal_preview_and_text_only_delivery_include_supplement(self):
        self.assertEqual(self.save(images=[self.upload()])[0], 200)
        row = cache_store.get_item(self.key)
        import notify
        with patch.object(hub, 'ROOT', self.root), \
             patch.object(weekly, 'load_manual', return_value=[]), \
             patch.object(weekly, '_elective_lids_from_disk', return_value=set()), \
             patch.object(hub.fetch_log, 'log_fetch'), \
             patch.object(hub, 'load_manual_raw', return_value=[]), \
             patch.object(notify, 'load_config', return_value={'channel': 'none'}), \
             patch.object(notify, 'dispatch', side_effect=AssertionError('sending forbidden')):
            result = hub.run_publish(7, True, None)
        self.assertIn('PPT 第 12 页', result['message'])
        self.assertEqual(len(result['images']), 1)
        self.assertEqual(len(result['images_personal']), 1)
        message = weekly.format_message([row], 7)
        with patch.dict(notify.SENDERS, {'clipboard': lambda text, cfg: text}):
            rendered = notify.dispatch(message, 'clipboard', {'_image_manifest': result['images']})
        self.assertIn('补充截图，请在作业控制台查看', rendered)
        self.assertNotIn('补充图片随提醒发送', rendered)

    def test_wecom_dispatch_embeds_hosted_supplement_in_native_message(self):
        self.assertEqual(self.save(images=[self.upload()])[0], 200)
        row = cache_store.get_item(self.key)
        import notify
        import io
        from pathlib import Path
        raw = Path(cache_store.asset_record(row['supplement']['images'][0]['asset_id'])['path']).read_bytes()
        with cache_store.connection(write=True) as con:
            con.execute('UPDATE assets SET source_url=? WHERE asset_id=?',
                        ('https://images.unit.invalid/supplement.png', row['supplement']['images'][0]['asset_id']))
        with patch.object(notify, 'send_wecom', return_value=True) as send, \
             patch.object(notify.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(raw)), \
             patch.object(notify, 'PART_LOG', self.root / 'parts.jsonl'), \
             patch.object(notify.time, 'sleep'):
            self.assertTrue(notify.dispatch(weekly.format_message([row], 7), 'wecom', {
                'wecom_webhook': 'https://unit.invalid/supplement',
                '_image_manifest': image_delivery.collect([row])}))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs, {'markdown': True})
        self.assertIn('![作业原图](https://images.unit.invalid/supplement.png)', send.call_args.args[0])

    def test_course_omit_delivers_only_supplement_images_with_heading_and_deadline(self):
        import course_rules
        self.assertEqual(self.save(text='', images=[self.upload()])[0], 200)
        asset = self.image_asset()
        cache_store.save_content(self.key, {'status': 'ready', 'source_version': 'picture-body',
            'text': '第一周作业提交这里', 'asset_ids': [asset['asset_id']], 'assets': [asset],
            'blocks': [{'id': 'p', 'type': 'paragraph', 'text': '第一周作业提交这里'},
                       {'id': 'img', 'type': 'image', 'status': 'ready', 'asset_id': asset['asset_id']}]})
        # Existing saved rules can switch modes without recreating the rule.
        rule = dict(id='digital', course='数字电路', match='exact', mode='images', lines=3)
        course_rules.change({'action': 'save', 'expected_revision': course_rules.snapshot()['revision'], 'rule': rule})
        rule['mode'] = 'omit'
        course_rules.change({'action': 'save', 'expected_revision': course_rules.snapshot()['revision'], 'rule': rule})
        for refresh in (False, True):
            if refresh:
                cache_store.record_content_error(self.key, 'fixture failure')
            item = cache_store.get_item(self.key)
            item['note'] = '旧平台说明'
            message = weekly.format_message([item], 7)
            self.assertIn('• 数字电路｜第一周作业', message)
            self.assertIn('⏰', message)
            self.assertIn('补充图片随提醒发送（1 张）', message)
            for unwanted in ('第一周作业提交这里', '旧平台说明', '详细要求见北化在线', '作业原图随提醒发送'):
                self.assertNotIn(unwanted, message)
            plan = image_delivery.collect([item])
            self.assertEqual(len(plan), 1)
            self.assertTrue(plan[0]['supplement'])
            self.assertTrue(image_delivery.delivery_bytes(plan[0]).startswith(b'\x89PNG'))
