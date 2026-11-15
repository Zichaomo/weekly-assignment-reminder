"""Keep reminder text selectable and embed every picture with Markdown V2."""
import io
import sys
from unittest.mock import Mock, patch

from helpers import IsolatedCase
from browser_fixture_server import png_bytes
import image_delivery
import notify


class NativeInlineMessageTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.images = [
            {'item_key': 'b', 'course': '乙课', 'title': '第一周作业', 'asset_id': 'b', 'supplement': True},
            {'item_key': 'a', 'course': '甲课', 'title': '第一周作业', 'asset_id': 'a'},
            {'item_key': 'a', 'course': '甲课', 'title': '第一周作业', 'asset_id': 'c', 'crop': True,
             'delivery_asset_id': 'cropped', 'supplement': False},
        ]
        self.text = ('📚 作业提醒\n\n• 甲课｜第一周作业\n\u3000\u3000⏰ 今天 23:59 截止\n'
                     '\u3000\u3000作业图片随提醒发送（2 张）。\n'
                     '• 乙课｜第一周作业\n\u3000\u3000⏰ 明天 23:59 截止\n'
                     '\u3000\u3000补充图片随提醒发送（1 张）。\n\n—— 共 2 项')
        self.raw = png_bytes(40, 20)
        self.cfg = {'wecom_webhook': 'https://unit.invalid/test', '_image_manifest': self.images,
                    '_delivery_errors': [], 'dedup_window_sec': 300}
        self.delivery = patch.object(image_delivery, 'delivery_bytes', return_value=self.raw)
        self.delivery.start()
        self.addCleanup(self.delivery.stop)
        self.asset = patch.object(image_delivery.cache_store, 'asset_record',
            side_effect=lambda aid: {'source_url': f'https://images.unit.invalid/{aid}.png'})
        self.asset.start()
        self.addCleanup(self.asset.stop)
        renderer = Mock()
        renderer.render.return_value = self.raw
        self.renderer = patch.dict(sys.modules, {'message_image': renderer})
        self.renderer.start()
        self.addCleanup(self.renderer.stop)

    def test_multiple_crop_regions_have_distinct_inline_links(self):
        images = [dict(self.images[2], delivery_asset_id='left'), dict(self.images[2], delivery_asset_id='right')]
        text = '• 甲课｜第一周作业\n\u3000\u3000作业图片随提醒发送（2 张）。'
        with patch.object(notify.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(self.raw)):
            result = image_delivery.inline_markdown(text, images)
        self.assertIn('/left.png', result[0])
        self.assertIn('/right.png', result[0])

    def test_supplement_and_crop_links_are_embedded_in_one_native_message(self):
        with patch.object(notify.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(self.raw)), \
             patch.object(notify, 'send_wecom', return_value=True) as send, \
             patch.object(notify, 'PART_LOG', self.root / 'parts.jsonl'), patch.object(notify.time, 'sleep'):
            self.assertTrue(notify.dispatch(self.text, 'wecom', self.cfg))
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs, {'markdown': True})
        body = send.call_args.args[0]
        self.assertIn('\u3000\u3000⏰', body)
        self.assertIn('![作业原图](https://images.unit.invalid/a.png)', body)
        self.assertIn('https://images.unit.invalid/cropped.png', body)
        self.assertIn('https://images.unit.invalid/b.png', body)
        self.assertLess(body.index('/a.png'), body.index('• 乙课'))
        self.assertLess(body.index('• 乙课'), body.index('/b.png'))
        self.assertTrue(all(not line.startswith((' ', '\u3000')) for line in body.splitlines() if line.startswith('![')))

    def test_missing_link_reports_dependency_without_sending_a_long_image(self):
        legacy_renderer = Mock()
        legacy_renderer.render.return_value = self.raw
        with patch.object(image_delivery.cache_store, 'asset_record', return_value={'source_url': None}), \
             patch.dict(sys.modules, {'message_image': legacy_renderer}), \
             patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(self.text, 'wecom', self.cfg))
        send.assert_not_called()
        legacy_renderer.render.assert_not_called()
        self.assertIn('图片链接', self.cfg['_delivery_errors'][-1])

    def test_different_remote_bytes_fail_before_any_message_is_sent(self):
        with patch.object(notify.urllib.request, 'urlopen', return_value=io.BytesIO(b'other picture')), \
             patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(self.text, 'wecom', self.cfg))
        send.assert_not_called()

    def test_identical_native_message_is_deduplicated(self):
        with patch.object(notify.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(self.raw)), \
             patch.object(notify, 'send_wecom', return_value=True) as send, \
             patch.object(notify, 'PART_LOG', self.root / 'parts.jsonl'), patch.object(notify.time, 'sleep'):
            self.assertTrue(notify.dispatch(self.text, 'wecom', self.cfg))
            self.assertTrue(notify.dispatch(self.text, 'wecom', self.cfg))
        send.assert_called_once()

    def test_native_message_over_single_message_capacity_does_not_change_to_image(self):
        with patch.object(image_delivery, 'inline_markdown', return_value=['first', 'second']), \
             patch.object(notify, 'send_wecom') as send:
            self.assertFalse(notify.dispatch(self.text, 'wecom', self.cfg))
        send.assert_not_called()
        self.assertIn('单条', self.cfg['_delivery_errors'][-1])
