import json
from helpers import IsolatedCase
import course_rules as rules
import content_summary
import weekly


class CourseRulesTests(IsolatedCase):
    def save(self, rule, **extra):
        return rules.change({'action': 'save', 'expected_revision': rules.snapshot()['revision'],
                             'rule': rule, **extra})

    def test_defaults_and_precedence(self):
        self.assertEqual(rules.resolve('大学物理实验(II)')['mode'], 'reference')
        self.assertEqual(rules.resolve('普通物理(Ⅱ)')['mode'], 'physics')
        self.assertEqual(rules.resolve('其他课')['mode'], 'full')
        self.save(dict(id='specific', course='普通物理(Ⅱ)', match='exact', mode='full', lines=2))
        self.assertEqual(rules.resolve('普通物理(Ⅱ)')['mode'], 'full')
        self.assertEqual(rules.resolve('普通物理(I)')['mode'], 'physics')

    def test_change_conflict_reset_and_validation_preserve_previous(self):
        initial = rules.snapshot()
        rule = dict(initial['rules'][0], mode='auto', lines=5)
        self.save(rule)
        with self.assertRaises(rules.Conflict):
            self.save(rule, expected_revision=initial['revision'])
        for bad in (dict(rule, lines=0), dict(rule, lines=True), dict(rule, mode='wrong')):
            with self.assertRaises(ValueError):
                self.save(bad)
        self.assertEqual(rules.resolve('大学物理实验')['mode'], 'auto')
        rules.change({'action':'reset', 'id':'lab', 'expected_revision':rules.snapshot()['revision']})
        self.assertEqual(rules.resolve('大学物理实验')['mode'], 'reference')
        self.assertEqual(json.loads(rules.path().read_text(encoding='utf-8')), [])

    def test_preview_does_not_save_and_push_uses_saved_line_limit(self):
        rule = dict(id='new', course='课程', match='exact', mode='full', lines=2)
        payload = {'rule':rule, 'text':'任务一\n任务二\n任务三'}
        preview = rules.preview(payload)
        self.assertIn('任务二', preview['push_text'])
        self.assertNotIn('任务三', preview['push_text'])
        self.assertFalse(rules.path().exists())
        self.save(rule)
        item = dict(course='课程', content_ready=True, content_text=payload['text'])
        summary = content_summary.summarize(item)
        self.assertIn('任务三', summary['text'])
        message = weekly.format_message([item], 7)
        self.assertIn('任务二', message)
        self.assertNotIn('任务三', message)
        self.assertIn('详情参见北化在线', message)
        item['content_stale'] = True
        self.assertEqual(content_summary.summarize(item)['status'], 'unavailable')

    def test_duplicate_rule_and_other_account_sample_rejected(self):
        with self.assertRaises(ValueError):
            self.save(dict(id='duplicate',course='普通物理',match='prefix',mode='auto',lines=3))
        with self.assertRaises(ValueError):
            rules.preview({'rule':rules.DEFAULTS[0], 'item_key':'missing-private-item'})

    def test_omit_mode_can_be_saved_previewed_and_loaded(self):
        rule = dict(id='digital', course='数字电子技术', match='exact', mode='omit', lines=3)
        state = self.save(rule)
        self.assertEqual(state['modes']['omit'], '去除全部作业正文')
        self.assertEqual(rules.resolve('数字电子技术')['mode'], 'omit')
        preview = rules.preview({'rule': rule, 'text': '第一周作业提交这里'})
        self.assertEqual(preview['status'], 'omitted')
        self.assertEqual(preview['push_text'], '')
        self.assertFalse(preview['needs_rule'])
        summary = content_summary.summarize({'course': '数字电子技术',
            'content_review': {'source_version': 'v1', 'mode': 'include', 'text': '旧人工确认'},
            'source_version': 'v1', 'content_ready': True, 'content_text': '平台正文'})
        self.assertEqual(summary['status'], 'omitted')
        self.assertEqual(summary['text'], '')

    def test_physics_preview_lists_exact_removed_quotes(self):
        rule = dict(id='example', course='课程', match='exact', mode='physics', lines=3)
        preview = rules.preview({'rule': rule,
            'text': '第一周作业提交这里\n提交图片\n一页作为一张照片，正放，不要倒置。\n习题：11-10、11-11。'})
        self.assertEqual(preview['push_text'], '习题：11-10、11-11。')
        self.assertEqual([entry['text'] for entry in preview['removed_excerpts']], [
            '第一周作业提交这里', '提交图片', '一页作为一张照片，正放，不要倒置。'])
        for entry in preview['removed_excerpts']:
            source = '第一周作业提交这里\n提交图片\n一页作为一张照片，正放，不要倒置。\n习题：11-10、11-11。'
            self.assertEqual(source[entry['start']:entry['end']].strip(), entry['text'])
        preview = rules.preview({'rule': rule, 'text': '注意本次额外提交测量图片。'})
        self.assertEqual(preview['removed_excerpts'], [])
        self.assertEqual(preview['push_text'], '注意本次额外提交测量图片。')

    def test_omit_mode_suppresses_unavailable_and_stale_body_fallback(self):
        rule = dict(id='omit', course='课程', match='exact', mode='omit', lines=3)
        self.save(rule)
        for extra in ({}, {'content_stale': True}, {'content_error': 'failed'},
                      {'content_identity_stale': True}, {'content_review_stale': True}):
            item = dict(course='课程', title='作业', deadline='2099-01-01 23:59', **extra)
            summary = content_summary.summarize(item)
            self.assertEqual(summary['status'], 'omitted')
            message = weekly.format_message([item], 7)
            self.assertIn('课程｜作业', message)
            self.assertIn('⏰', message)
            self.assertNotIn('详细要求见北化在线', message)

    def test_lab_submission_phrase_is_removed_but_specific_requirements_remain(self):
        rule = dict(id='electronics-lab', course='电子技术实验（Ⅰ)', match='exact', mode='physics', lines=3)
        self.save(rule)
        original = '按要求提交。\n画出放大电路的频率响应曲线。\n按要求提交测量记录。'
        preview = rules.preview({'rule': rule, 'text': original})
        self.assertEqual([entry['text'] for entry in preview['removed_excerpts']], ['按要求提交。'])
        self.assertEqual(preview['push_text'], '画出放大电路的频率响应曲线。\n按要求提交测量记录。')

    def test_generic_only_lab_body_can_be_filtered_after_an_old_review_expires(self):
        rule = dict(id='electronics-lab', course='电子技术实验（Ⅰ)', match='exact', mode='physics', lines=3)
        self.save(rule)
        item = dict(course=rule['course'], title='实验二', deadline='2099-01-02 23:59',
                    content_ready=True, content_review_stale=True, content_text='按要求提交')
        summary = content_summary.summarize(item)
        self.assertEqual(summary['status'], 'generic_only')
        self.assertEqual([entry['text'] for entry in summary['removed_excerpts']], ['按要求提交'])
        message = weekly.format_message([item], 7)
        self.assertIn('实验二', message)
        self.assertIn('⏰', message)
        self.assertNotIn('按要求提交', message)
        self.assertNotIn('详细要求见北化在线', message)
        item['content_text'] += '。\n画出放大电路的频率响应曲线。'
        self.assertEqual(content_summary.summarize(item)['status'], 'needs_review')
