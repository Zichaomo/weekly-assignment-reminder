"""Daily operations and scheduled entry points, with all delivery disabled."""
import io
import json
import sys
import time
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from helpers import IsolatedCase
import cache_store
import fetch_log
import hub
import manual
import notify
import scrape
import task_run
import weekly
import server

def manual_call(payload):
    return server.manual_action(payload['action'],payload)


class DailyWorkflows(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.use_explicit_account('daily-fixture')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.index = {'1': '必修课程', '2': '选修课程', '3': '拉黑课程'}
        paths = {'COURSES_FILE': 'courses.json', 'BLACKLIST_FILE': 'course_blacklist.json',
                 'TEST_BLACKLIST_FILE': 'test_blacklist.json', 'ELECTIVES_FILE': 'electives.json',
                 'META_BACKUP_FILE': 'meta.json', 'SCAN_SNAPSHOT_FILE': 'scan.json'}
        for name, filename in paths.items():
            self.stack.enter_context(patch.object(scrape, name, self.root / filename))
        scrape.COURSES_FILE.write_text(json.dumps({'courses': self.index, 'meta': {}}), encoding='utf-8')
        for name in ('BLACKLIST_FILE', 'TEST_BLACKLIST_FILE', 'ELECTIVES_FILE'):
            getattr(scrape, name).write_text('{"courses":{}}', encoding='utf-8')
        for module in (weekly, hub, task_run):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        for module in (manual, hub):
            self.stack.enter_context(patch.object(module, 'MANUAL_FILE', self.root / 'manual.json'))
        self.stack.enter_context(patch.object(task_run, 'TASK_LOG', self.root / 'task.jsonl'))
        self.stack.enter_context(patch.object(task_run, 'CONSOLE_LOG', self.root / 'console.log'))
        self.stack.enter_context(patch.object(task_run, 'last_fetch_record', return_value={}))
        self.stack.enter_context(patch.object(notify, 'load_config', return_value={}))
        self.stack.enter_context(patch.object(fetch_log, 'log_fetch'))
        self.send = self.stack.enter_context(patch.object(weekly, 'send_one', side_effect=AssertionError('delivery forbidden')))
        self.alert = self.stack.enter_context(patch.object(weekly, 'send_alert', side_effect=AssertionError('alert forbidden')))
        self.fetch = self.stack.enter_context(patch.object(weekly, 'fetch', side_effect=AssertionError('platform forbidden')))
        self.stack.enter_context(patch.object(scrape, 'sync_playwright', side_effect=AssertionError('browser forbidden')))
        self.seed()

    def seed(self):
        version = cache_store.version_hash(self.index)
        cache_store.begin_scan('daily', mode='test', expected_lids=self.index,
                               tests_requested=True, course_index_version=version)
        for lid in self.index:
            deadline = (datetime.now() + timedelta(days=int(lid)+1)).strftime('%Y-%m-%d %H:%M')
            cache_store.commit_course_scan('daily', lid=lid, course=self.index[lid],
                homeworks=[{'hwtid':lid, 'title':'任务'+lid, 'deadline':deadline,
                            'submitted':True}], tests=[], homework_status='ok',
                tests_status='ok', index_version=version)
        cache_store.update_scan('daily', status='complete', finished_at=time.time())

    def run_weekly(self, *args):
        with patch.object(sys, 'argv', ['weekly.py', *args]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return weekly.main()

    def test_filters_lists_and_audience_preview_use_same_rules(self):
        self.assertEqual(hub.run_list(1, False)['rows'], [])
        self.assertEqual(len(hub.run_list(7, False)['rows']), 3)
        hub.run_blacklist_apply(['3'])
        hub.run_blacklist_elective(['2'])
        rows = hub.run_list(7, False)['rows']
        self.assertEqual({r['lid'] for r in rows}, {'1','2'})
        self.assertTrue(next(r for r in rows if r['lid']=='2')['elective'])
        manual.cmd_add(SimpleNamespace(course='手动课程', title='手动任务',
            deadline=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M'), note='通知'))
        preview = hub.run_publish(7, True, 'none')
        self.assertFalse(preview['sent'])
        self.assertIn('任务1', preview['message'])
        self.assertNotIn('任务2', preview['message'])
        self.assertNotIn('任务3', preview['message_personal'])
        self.assertIn('任务2', preview['message_personal'])
        self.assertIn('手动任务', preview['message'])
        manual.cmd_done(SimpleNamespace(id=1))
        self.assertNotIn('手动任务', hub.run_publish(7, True, 'none')['message'])
        hub.run_blacklist_remove(['3'])
        self.assertEqual(len(hub.run_list(7, False)['rows']), 3)
        self.fetch.assert_not_called(); self.send.assert_not_called(); self.alert.assert_not_called()

    def test_weekly_no_send_generates_both_audiences_without_delivery(self):
        hub.run_blacklist_elective(['2'])
        self.assertEqual(self.run_weekly('--no-send'), 0)
        self.assertNotIn('任务2', (self.root/'last_message.txt').read_text(encoding='utf-8'))
        self.assertIn('任务2', (self.root/'last_message_personal.txt').read_text(encoding='utf-8'))
        self.fetch.assert_not_called(); self.send.assert_not_called(); self.alert.assert_not_called()

    def test_no_send_login_failure_cannot_send_alerts(self):
        with patch.object(weekly, 'load_fresh_publish_cache', return_value=(None, {})), \
             patch.object(weekly, 'ensure_login', return_value=False):
            self.assertEqual(self.run_weekly('--no-send'), 1)
        self.alert.assert_not_called(); self.send.assert_not_called()

    def test_new_fetch_requires_coverage_before_sending(self):
        # A usable body is not evidence that every course scan completed.
        with patch.object(weekly, 'load_fresh_publish_cache', return_value=(None, {'publishable':False,'publish_block_reasons':['课程范围不完整']})), \
             patch.object(weekly, 'ensure_login', return_value=True):
            self.fetch.side_effect = None
            self.fetch.return_value = {'homeworks':[{'title':'部分结果', 'course':'必修课程',
                'deadline':(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')}]}
            self.alert.side_effect = None
            self.assertNotEqual(self.run_weekly(), 0)
        self.send.assert_not_called()

    def test_task_dry_run_executes_weekly_preview_without_notification(self):
        def invoke(cmd, **kwargs):
            self.assertIn('--no-send', cmd)
            self.assertEqual(kwargs['env']['BUCT_NOTIFY_CHANNEL'], 'none')
            return SimpleNamespace(returncode=self.run_weekly(*cmd[2:]), stdout='', stderr='')
        with patch.dict('os.environ', {}, clear=False), \
             patch.object(task_run.subprocess, 'run', side_effect=invoke), \
             patch.object(task_run, '_alert', side_effect=AssertionError('task alert forbidden')), \
             patch.object(sys, 'argv', ['task_run.py','--dry-run']), redirect_stdout(io.StringIO()):
            self.assertEqual(task_run.main(), 0)
        self.assertTrue((self.root/'last_message.txt').exists())
        self.send.assert_not_called();self.alert.assert_not_called()

    def test_failed_task_dry_run_cannot_alert(self):
        with patch.dict('os.environ', {}, clear=False), \
             patch.object(task_run.subprocess, 'run', return_value=SimpleNamespace(returncode=1,stdout='',stderr='fixture failure')), \
             patch.object(task_run, '_alert', side_effect=AssertionError('task alert forbidden')) as alert, \
             patch.object(sys, 'argv', ['task_run.py','--dry-run']), redirect_stdout(io.StringIO()):
            self.assertEqual(task_run.main(), 1)
        alert.assert_not_called()

    def test_manual_edit_delete_and_invalid_date_preserve_saved_data(self):
        deadline=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        self.assertTrue(manual_call({'action':'add','course':'手动课程',
            'title':'原任务','deadline':deadline})['ok'])
        self.assertTrue(manual_call({'action':'edit','id':1,'title':'新任务'})['ok'])
        self.assertEqual(manual.load()[0]['title'], '新任务')
        self.assertFalse(manual_call({'action':'edit','id':1,'deadline':'invalid'})['ok'])
        self.assertEqual(manual.load()[0]['deadline'], deadline)
        self.assertTrue(manual_call({'action':'rm','id':1})['ok'])
        self.assertEqual(manual.load(), [])

    def test_expired_entries_only_show_with_explicit_local_filter(self):
        past=(datetime.now()-timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        data=[{'title':'过去任务','course':'课程','deadline':past}]
        self.assertEqual(hub.build_rows(data,[],7), [])
        self.assertEqual(len(hub.build_rows(data,[],7,include_expired=True)), 1)
        self.assertEqual(weekly.merge_and_filter({'homeworks':data},[],7), [])

    def test_test_blacklist_hides_tests_without_hiding_homework(self):
        version=cache_store.version_hash(self.index)
        dl=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        cache_store.begin_scan('test-added',mode='test',expected_lids=self.index,
            tests_requested=True,course_index_version=version)
        cache_store.commit_course_scan('test-added',lid='1',course='必修课程',
            homeworks=[{'hwtid':'1','title':'任务1','deadline':dl}],
            tests=[{'test_id':'t1','title':'测试1','deadline':dl}],
            homework_status='ok',tests_status='ok',index_version=version)
        cache_store.update_scan('test-added',status='complete',finished_at=time.time())
        self.assertEqual(len(hub.run_list(7,False)['rows']),4)
        hub.run_test_blacklist_apply(['1'])
        self.assertEqual(len(hub.run_list(7,False)['rows']),3)
        preview=hub.run_publish(7,True,'none')
        self.assertIn('任务1',preview['message']);self.assertNotIn('测试1',preview['message'])
        hub.run_test_blacklist_remove(['1'])
        self.assertEqual(len(hub.run_list(7,False)['rows']),4)

    def test_no_send_fetch_exception_cannot_alert(self):
        with patch.object(weekly,'load_fresh_publish_cache',return_value=(None,{})), \
             patch.object(weekly,'ensure_login',return_value=True):
            self.fetch.side_effect=RuntimeError('fixture fetch failed')
            self.assertEqual(self.run_weekly('--no-send'),1)
        self.alert.assert_not_called();self.send.assert_not_called()

    def test_fresh_cache_sends_both_audiences_via_stub_only(self):
        self.send.side_effect=None;self.send.return_value=(0,'','')
        hub.run_blacklist_elective(['2'])
        self.assertEqual(self.run_weekly(),0)
        self.assertEqual([c.args[1] for c in self.send.call_args_list],['class','personal'])
        self.assertNotIn('任务2',self.send.call_args_list[0].args[0])
        self.assertIn('任务2',self.send.call_args_list[1].args[0])
        self.alert.assert_not_called();self.fetch.assert_not_called()

    def test_final_freshness_check_blocks_send_if_cache_expires(self):
        good=weekly.load_fresh_publish_cache()
        with patch.object(weekly,'load_fresh_publish_cache',side_effect=[good,good,
             (None,{'publish_block_reasons':['缓存已过期']})]):
            self.assertEqual(self.run_weekly(),2)
        self.send.assert_not_called()

    def test_scheduled_already_pushed_skips_weekly(self):
        with patch.object(task_run,'push_status_today',return_value={'class':True,'personal':True,'personal_enabled':True}), \
             patch.object(task_run.subprocess,'run',side_effect=AssertionError('must skip')) as run, \
             patch.object(sys,'argv',['task_run.py']),redirect_stdout(io.StringIO()):
            self.assertEqual(task_run.main(),0)
        run.assert_not_called()

    def test_no_send_freshness_probe_does_not_alert(self):
        import alert
        with patch.object(weekly,'check_freshness',return_value=2), \
             patch.object(alert,'send',side_effect=AssertionError('alert forbidden')) as send:
            self.assertEqual(self.run_weekly('--no-send','--check-freshness','--alert-if-stale'),2)
        send.assert_not_called()

    def test_scheduled_partial_delivery_only_retries_missing_audience(self):
        with patch.dict('os.environ', {}, clear=False), \
             patch.object(task_run,'push_status_today',return_value={'class':True,'personal':False,'personal_enabled':True}), \
             patch.object(notify,'resolve_webhook',return_value=('https://example.invalid/fixture','fixture')), \
             patch.object(task_run.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run, \
             patch.object(sys,'argv',['task_run.py']),redirect_stdout(io.StringIO()):
            self.assertEqual(task_run.main(),0)
        cmd=run.call_args.args[0]
        self.assertIn('--skip-class',cmd);self.assertNotIn('--skip-personal',cmd)
