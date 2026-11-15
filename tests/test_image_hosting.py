"""New screenshots and crops follow the same native-message delivery path."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import io
import json
import sys
import urllib.error
from unittest.mock import patch

from helpers import IsolatedCase
from browser_fixture_server import png_bytes
import assignment_supplements
import cache_store
import hub
import image_delivery
import notify
import weekly


class ImageHostingTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.use_explicit_account('hosting-owner')
        cache_store.begin_scan('hosting', mode='test', expected_lids=['1'],
            tests_requested=False, course_index_version='index')
        cache_store.commit_course_scan('hosting', lid='1', course='复变函数与积分变换（A）',
            homeworks=[{'hwtid': '1', 'title': '图片作业',
                'deadline': (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')}],
            tests=[], homework_status='ok', tests_status='disabled', index_version='index')
        self.key = cache_store.load_items()[0]['item_key']
        self.raw = png_bytes(40, 20)
        self.save_screenshot(self.raw)
        self.cfg = {'wecom_webhook': 'https://unit.invalid/class', 'dedup_window_sec': 300,
            'personal': {'enabled': True, 'wecom_webhook': 'https://unit.invalid/personal'},
            'image_hosting': {'provider': 'freeimage.host'}, '_delivery_errors': []}
        self.uploads = []
        self.public_images = {}
        self.upload_failure = False
        self.wrong_bytes = False
        self.on_upload = lambda: None
        self.request_patch = patch.object(notify, '_open_delivery_request', side_effect=self.request)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        ledger = patch.object(notify, 'PART_LOG', self.root / 'parts.jsonl')
        ledger.start()
        self.addCleanup(ledger.stop)
        sleep = patch.object(notify.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def save_screenshot(self, raw):
        current = cache_store.get_item(self.key)['supplement']
        return assignment_supplements.save(self.key, {
            'expected_revision': current['revision'],
            'expected_identity_generation': current['identity_generation'],
            'text': '', 'images': [{'data': base64.b64encode(raw).decode()}]})

    def message(self):
        item = cache_store.get_item(self.key)
        self.cfg['_image_manifest'] = image_delivery.collect([item])
        return weekly.format_message([item], 7)

    def request(self, request, timeout):
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
        url = request.full_url
        if request.get_method() == 'POST':
            self.assertEqual(url, 'https://freeimage.host/api/1/upload')
            self.assertIn(b'name="source"; filename="homework.', request.data)
            self.assertNotIn(self.key.encode(), request.data)
            self.uploads.append(request.data)
            if self.upload_failure:
                raise urllib.error.URLError('simulated hosting outage')
            raw = request.data.split(b'Content-Type: image/', 1)[1].split(b'\r\n\r\n', 1)[1]
            raw = raw.rsplit(b'\r\n--', 1)[0]
            link = 'https://iili.io/unit-' + hashlib.sha256(raw).hexdigest() + '.png'
            self.public_images[link] = raw
            self.on_upload()
            return io.BytesIO(json.dumps({'status_code': 200, 'image': {'url': link}}).encode())
        if url == 'https://freeimage.host/api':
            return io.BytesIO(b'<h2>API Key</h2><input value="unit-upload-parameter" readonly><h2>API call</h2>')
        if url in self.public_images:
            return io.BytesIO(b'wrong image' if self.wrong_bytes else self.public_images[url])
        raise urllib.error.URLError('simulated expired original link')

    def test_new_screenshot_is_uploaded_and_sent_in_one_native_message(self):
        text = self.message()
        with patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertTrue(notify.dispatch(text, 'wecom', self.cfg))
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs, {'markdown': True})
        body = send.call_args.args[0]
        self.assertIn('\u3000\u3000⏰', body)
        self.assertIn('\n\n![作业原图](https://iili.io/unit-', body)
        self.assertEqual(len(self.uploads), 1)
        aid = self.cfg['_image_manifest'][0]['asset_id']
        self.assertIn(cache_store.asset_record(aid)['source_url'], self.public_images)

    def test_saved_link_is_reused_for_personal_delivery_and_after_reload(self):
        text = self.message()
        with patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertTrue(notify.dispatch(text, 'wecom', self.cfg))
            cache_store.init_db()
            self.assertTrue(notify.dispatch(self.message(), 'wecom', self.cfg, audience='personal'))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(send.call_args.args[1]['_wecom_webhook_override'], 'https://unit.invalid/personal')

    def test_replacing_screenshot_uploads_new_bytes_and_uses_new_link(self):
        with patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertTrue(notify.dispatch(self.message(), 'wecom', self.cfg))
            first = send.call_args.args[0]
            self.save_screenshot(png_bytes(41, 21))
            self.assertTrue(notify.dispatch(self.message(), 'wecom', self.cfg))
        self.assertEqual(len(self.uploads), 2)
        self.assertNotEqual(first, send.call_args.args[0])

    def test_selected_crop_bytes_get_their_own_public_links(self):
        asset = self.image_asset()
        cache_store.save_content(self.key, {'status': 'ready', 'source_version': 'image-v1',
            'blocks': [{'id': 'p', 'type': 'paragraph', 'text': '', 'segments': [
                {'type': 'image', 'asset_id': asset['asset_id'], 'status': 'ready'}]}],
            'asset_ids': [asset['asset_id']], 'assets': [asset]})
        cache_store.save_view(self.key, expected_version='image-v1', expected_view_revision='none',
            regions=[{'asset_id': asset['asset_id'], 'box': [0, 0, 0.5, 1]},
                     {'asset_id': asset['asset_id'], 'box': [0, 0, 1, 0.5]}])
        with patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertTrue(notify.dispatch(self.message(), 'wecom', self.cfg))
        plan = self.cfg['_image_manifest']
        self.assertEqual(len(plan), 3)
        self.assertEqual(len(self.uploads), 3)
        body = send.call_args.args[0]
        for entry in plan:
            aid = entry.get('delivery_asset_id') or entry['asset_id']
            record = cache_store.asset_record(aid)
            self.assertIn(record['source_url'], body)
            self.assertEqual(self.public_images[record['source_url']], image_delivery.delivery_bytes(entry))
        self.assertIsNone(cache_store.asset_record(asset['asset_id'])['source_url'])

    def test_upload_failure_can_be_retried_without_sending_partial_text(self):
        text = self.message()
        self.upload_failure = True
        with patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertFalse(notify.dispatch(text, 'wecom', self.cfg))
            send.assert_not_called()
            self.assertIn('图片托管', self.cfg['_delivery_errors'][-1])
            self.upload_failure = False
            self.assertTrue(notify.dispatch(text, 'wecom', self.cfg))
        send.assert_called_once()
        self.assertEqual(len(self.uploads), 2)

    def test_webhook_failure_retries_using_the_saved_hosted_image(self):
        text = self.message()
        with patch.object(notify, 'send_wecom', side_effect=[False, True]) as send:
            self.assertFalse(notify.dispatch(text, 'wecom', self.cfg))
            self.assertTrue(notify.dispatch(text, 'wecom', self.cfg))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.uploads), 1)

    def test_mismatched_hosted_bytes_block_delivery_and_link_persistence(self):
        self.wrong_bytes = True
        with patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(self.message(), 'wecom', self.cfg))
        send.assert_not_called()
        self.assertEqual(len(self.uploads), 1)
        aid = self.cfg['_image_manifest'][0]['asset_id']
        self.assertIsNone(cache_store.asset_record(aid)['source_url'])

    def test_expired_source_link_is_rehosted(self):
        text = self.message()
        aid = self.cfg['_image_manifest'][0]['asset_id']
        with cache_store.connection(write=True) as con:
            con.execute('UPDATE assets SET source_url=? WHERE asset_id=?',
                ('https://expired.unit.invalid/old.png', aid))
        with patch.object(notify, 'send_wecom', return_value=True):
            self.assertTrue(notify.dispatch(text, 'wecom', self.cfg))
        self.assertEqual(len(self.uploads), 1)

    def test_supplement_edited_during_upload_is_rejected_before_send(self):
        text = self.message()
        self.on_upload = lambda: self.save_screenshot(png_bytes(42, 22))
        with patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(text, 'wecom', self.cfg))
        send.assert_not_called()
        self.assertEqual(len(self.uploads), 1)

    def test_obsolete_manifest_is_rejected_before_upload(self):
        text = self.message()
        self.save_screenshot(png_bytes(42, 22))
        with patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(text, 'wecom', self.cfg))
        send.assert_not_called()
        self.assertEqual(self.uploads, [])

    def test_local_preview_and_text_only_channels_never_upload(self):
        self.write_config(self.cfg)
        with patch.object(notify, 'send_wecom') as send, patch.object(notify, 'log_push'):
            self.assertTrue(hub.run_publish(7, True, None)['ok'])
            self.assertTrue(notify.dispatch(self.message(), 'none', self.cfg))
        send.assert_not_called()
        self.assertEqual(self.uploads, [])

    def test_console_publish_hosts_once_for_both_audiences(self):
        item = cache_store.get_item(self.key)
        coverage = {'publishable': True, 'age_sec': 0}
        with patch.object(weekly, 'load_fresh_publish_cache',
                return_value=({'homeworks': [item]}, coverage)), \
             patch.object(weekly, 'load_manual', return_value=[]), \
             patch.object(weekly, '_elective_lids_from_disk', return_value=set()), \
             patch.object(notify, 'load_config', return_value=self.cfg), \
             patch.object(notify, 'log_push'), patch.object(hub.fetch_log, 'log_fetch'), \
             patch.object(hub, 'ROOT', self.root), \
             patch.object(notify, 'send_wecom', return_value=True) as send:
            result = hub.run_publish(7, False, 'wecom')
        self.assertTrue(result['ok'])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.uploads), 1)
        self.assertTrue(all(call.kwargs == {'markdown': True} for call in send.call_args_list))

    def test_scheduled_notify_cli_loads_hosting_configuration(self):
        text = self.message()
        self.write_config(self.cfg)
        message_file = self.root / 'message.txt'
        manifest_file = self.root / 'images.json'
        message_file.write_text(text, encoding='utf-8')
        manifest_file.write_text(json.dumps(self.cfg['_image_manifest']), encoding='utf-8')
        args = ['notify.py', '--file', str(message_file), '--image-manifest', str(manifest_file),
                '--channel', 'wecom', '--source', 'weekly']
        with patch.object(sys, 'argv', args), patch.object(notify, 'log_push'), \
             patch.object(notify, 'send_wecom', return_value=True) as send:
            self.assertEqual(notify.main(), 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs, {'markdown': True})
        self.assertEqual(len(self.uploads), 1)

    def test_hosting_requires_the_provider_to_be_enabled(self):
        self.cfg.pop('image_hosting')
        with patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(self.message(), 'wecom', self.cfg))
        send.assert_not_called()
        self.assertEqual(self.uploads, [])

    def test_concurrent_preparations_reuse_the_same_persisted_link(self):
        import image_hosting
        self.message()
        entry = self.cfg['_image_manifest'][0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(image_hosting.image_url, entry, self.raw, self.cfg['image_hosting'])
                    for _ in range(2)]
            urls = [job.result(timeout=10) for job in jobs]
        self.assertEqual(urls[0], urls[1])
        self.assertEqual(len(self.uploads), 1)
