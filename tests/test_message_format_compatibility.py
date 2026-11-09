"""Lock the group-message format to the user's specified baseline commit."""
import io
import subprocess
from datetime import datetime
from unittest.mock import patch

from helpers import IsolatedCase, ROOT
import cache_store
import image_delivery
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

    def test_class_and_personal_text_match_697cf04_exactly(self):
        with patch.object(weekly, 'datetime', FixedDateTime):
            for title in [None, '📚 我的作业提醒（含选修课）']:
                expected = self.reference['format_message'](self.items, 7, title=title)
                self.assertEqual(weekly.format_message(self.items, 7, title=title), expected)
                self.assertTrue(expected.startswith(title or '📚 作业提醒'))

    def test_wecom_inline_payload_keeps_baseline_line_breaks_and_image_position(self):
        reference_images = baseline_module('image_delivery.py')
        with patch.object(weekly, 'datetime', FixedDateTime):
            message = weekly.format_message(self.items, 7)
        plan = [{'item_key': 'fixture-picture', 'asset_id': 'a' * 64,
                 'title': self.items[1]['title']}]
        raw = b'fixture-original-bytes'
        reference_images['original_bytes'] = lambda entry: raw
        with patch.object(image_delivery, 'delivery_bytes', return_value=raw), \
             patch.object(cache_store, 'asset_record', return_value={
                 'source_url': 'https://course.buct.edu.cn/fixture.png'}), \
             patch.object(image_delivery.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: io.BytesIO(raw)):
            expected = reference_images['inline_markdown'](message, plan)
            actual = image_delivery.inline_markdown(message, plan)
        self.assertEqual(actual, expected)
        joined = ''.join(actual)
        self.assertLess(joined.index('26-27第九周作业'), joined.index('![作业原图]'))
        self.assertLess(joined.index('![作业原图]'), joined.index('课堂练习'))
