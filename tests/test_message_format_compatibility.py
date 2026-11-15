"""Preserve the group-message baseline with visible detail indentation."""
import io
import json
import re
import subprocess
from datetime import datetime
from unittest.mock import patch

from helpers import IsolatedCase, ROOT
import cache_store
import image_delivery
import notify
import weekly


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 8, 10, 0)
        return value if tz is None else value.astimezone(tz)


def baseline_module(filename):
    result = subprocess.run(
        ['git', 'show', '697cf04:data/' + filename], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', check=True, timeout=10)
    namespace = {'__name__': 'message_format_baseline', '__file__': str(ROOT / 'data' / filename)}
    exec(compile(result.stdout, '697cf04:data/' + filename, 'exec'), namespace)
    return namespace


def visible_indentation(message):
    return re.sub(r'^ {3}', '\u3000\u3000', message, flags=re.M)


class MessageFormatCompatibility(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.reference = baseline_module('weekly.py')
        self.reference['datetime'] = FixedDateTime
        self.items = [
            {'source': 'manual', 'course': '英语', 'title': 'U校园 新视野U5',
             'deadline': '2026-11-08 23:59', '_deadline_dt': datetime(2026, 11, 8, 23, 59)},
            {'course': '复变函数与积分变换（A）', 'title': '26-27第九周作业',
             'deadline': '2026-11-09 23:59', '_deadline_dt': datetime(2026, 11, 9, 23, 59),
             'kind': 'homework', 'content_ready': True, 'source_version': 'fixture',
             'content_blocks': [{'type': 'image', 'asset_id': 'a' * 64, 'status': 'ready'}]},
            {'course': '课堂练习', 'title': '稍后截止', 'deadline': '2026-11-13 20:00',
             '_deadline_dt': datetime(2026, 11, 13, 20, 0)},
        ]

    def test_class_and_personal_text_preserve_baseline_with_visible_indentation(self):
        with patch.object(weekly, 'datetime', FixedDateTime):
            for title in [None, '📚 我的作业提醒（含选修课）']:
                expected = self.reference['format_message'](self.items, 7, title=title)
                self.assertEqual(weekly.format_message(self.items, 7, title=title), visible_indentation(expected))
                self.assertTrue(expected.startswith(title or '📚 作业提醒'))

    def test_wecom_inline_payload_keeps_baseline_line_breaks_and_image_position(self):
        reference_images = baseline_module('image_delivery.py')
        with patch.object(weekly, 'datetime', FixedDateTime):
            message = weekly.format_message(self.items, 7)
            baseline_message = self.reference['format_message'](self.items, 7)
        plan = [{'item_key': 'fixture-picture', 'asset_id': 'a' * 64,
                 'title': self.items[1]['title']}]
        raw = b'fixture-original-bytes'
        reference_images['original_bytes'] = lambda entry: raw
        with patch.object(image_delivery, 'delivery_bytes', return_value=raw), \
             patch.object(cache_store, 'asset_record', return_value={
                 'source_url': 'https://course.buct.edu.cn/fixture.png'}), \
             patch.object(image_delivery.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: io.BytesIO(raw)):
            expected = reference_images['inline_markdown'](baseline_message, plan)
            actual = image_delivery.inline_markdown(message, plan)
        self.assertEqual(actual, [visible_indentation(chunk) for chunk in expected])
        joined = ''.join(actual)
        self.assertLess(joined.index('26-27第九周作业'), joined.index('![作业原图]'))
        self.assertLess(joined.index('![作业原图]'), joined.index('课堂练习'))

    def test_all_deadline_variants_use_two_ideographic_spaces(self):
        deadlines = [None, datetime(2026, 11, 7, 20), datetime(2026, 11, 8, 23),
                     datetime(2026, 11, 9, 23), datetime(2026, 11, 13, 20)]
        with patch.object(weekly, 'datetime', FixedDateTime):
            for deadline in deadlines:
                item = {'course': '课程', 'title': '作业', '_deadline_dt': deadline}
                message = weekly.format_message([item], 7)
                line = next(line for line in message.splitlines() if '⏰' in line)
                self.assertTrue(line.startswith('\u3000\u3000⏰'), repr(line))

    def test_requirements_notes_supplements_and_continuations_share_indent(self):
        items = [
            {'course': '电路', 'title': '任务', 'content_ready': True, 'source_version': 'v1',
             'content_review': {'source_version': 'v1', 'mode': 'include', 'text': '要求甲\n要求乙\n要求丙\n要求丁'},
             'supplement': {'text': '补充第一行\n补充第二行', 'images': [{'asset_id': 'a'}]}},
            {'course': '英语', 'title': '练习', 'source': 'manual', 'note': '要求第一行\n要求第二行'},
            {'course': '大学物理实验', 'title': '报告'},
        ]
        message = weekly.format_message(items, 7)
        details = [line for line in message.splitlines() if line.strip() and not
                   line.startswith(('📚', '⚠️', '🗓', '• ', '——'))]
        self.assertGreater(len(details), 10)
        self.assertTrue(all(line.startswith('\u3000\u3000') for line in details), details)
        self.assertIn('\u3000\u3000详细要求见北化在线', message)
        self.assertIn('\u3000\u3000……（详情参见北化在线）', message)
        self.assertIn('\u3000\u3000补充第二行', message)
        self.assertIn('\u3000\u3000要求第二行', message)
        self.assertTrue(all(not line.startswith('\u3000') for line in message.splitlines() if '｜' in line))

    def test_plain_wecom_payload_preserves_visible_indentation(self):
        with patch.object(weekly, 'datetime', FixedDateTime):
            message = weekly.format_message(self.items[:1], 7)
        requests = []
        def response(req, **kwargs):
            requests.append(json.loads(req.data))
            return io.BytesIO(b'{"errcode":0}')
        with patch.object(notify.urllib.request, 'urlopen', side_effect=response):
            self.assertTrue(notify.send_wecom(message, {'_wecom_webhook_override': 'https://unit.invalid/indent'}))
        self.assertIn('\n\u3000\u3000⏰', requests[0]['text']['content'])

    def test_original_ascii_indented_image_marker_is_still_supported(self):
        message = self.reference['format_message'](self.items, 7)
        plan = [{'item_key': 'fixture-picture', 'asset_id': 'a' * 64, 'title': self.items[1]['title']}]
        with patch.object(image_delivery, 'delivery_bytes', return_value=b'fixture'), \
             patch.object(cache_store, 'asset_record', return_value={'source_url': 'https://course.buct.edu.cn/fixture.png'}), \
             patch.object(image_delivery.urllib.request, 'urlopen', return_value=io.BytesIO(b'fixture')):
            chunks = image_delivery.inline_markdown(message, plan)
        self.assertIsNotNone(chunks)
        self.assertIn('![作业原图]', ''.join(chunks))
