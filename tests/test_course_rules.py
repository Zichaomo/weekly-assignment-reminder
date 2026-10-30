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
        self.assertEqual(rules.resolve('其他课')['mode'], 'auto')
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
