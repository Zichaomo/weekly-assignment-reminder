import json
from pathlib import Path
from unittest.mock import patch
from helpers import IsolatedCase
import console_runtime as runtime
import schedule_status as schedule
import server


class SystemStatus(IsolatedCase):
    def test_disabled_task_has_no_next_run_but_retains_setting_and_history(self):
        task={'name':'BUCT-Homework-Weekly','registered':True,'enabled':False,
              'next_run':'2099-01-04 09:00:00','schedule_time':'09:00','days':7,'last_run':'old'}
        with patch.object(schedule,'ROOT',self.root),patch.object(schedule,'_cache',None), \
             patch.object(schedule,'_query_windows',return_value={'available':True,'tasks':[task]}):
            state=schedule.snapshot()
        self.assertIsNone(state['tasks'][0]['next_run'])
        self.assertEqual(state['tasks'][0]['schedule_time'],'09:00')
        self.assertEqual(state['tasks'][0]['last_run'],'old')
        self.assertEqual(task['next_run'],'2099-01-04 09:00:00', 'normalization cannot mutate the query cache')

    def test_version_compares_content_and_includes_ui(self):
        (self.root/'one.py').write_text('a=1')
        (self.root/'console.html').write_text('html')
        a=runtime.disk_version(self.root)
        (self.root/'one.py').write_text('a=2')
        self.assertNotEqual(a,runtime.disk_version(self.root))
        b=runtime.disk_version(self.root)
        (self.root/'panel.js').write_text('new ui')
        self.assertNotEqual(b,runtime.disk_version(self.root))

    def post(self, version='v', pid=1):
        h=object.__new__(server.Handler)
        h.path='/api/runtime/update'
        h._body=lambda:{'expected_version':version,'expected_pid':pid}
        h._send=lambda status,data:(status,data)
        return h._do_POST()

    def test_restart_refuses_running_tasks_and_stale_page(self):
        with patch.object(server,'_RESTARTING',False), \
             patch.object(runtime,'status',return_value={'disk_version':'v','pid':1}), \
             patch.object(runtime,'launch_restart') as launch,patch.object(server,'runtime_busy',return_value=True):
            self.assertEqual(self.post()[0],409)
            self.assertEqual(self.post('old')[0],409)
            self.assertEqual(self.post(pid=2)[0],409)
        launch.assert_not_called()

    def test_restart_launches_worker_before_exit_timer_and_blocks_new_tasks(self):
        with patch.object(server,'_RESTARTING',False), \
             patch.object(runtime,'status',return_value={'disk_version':'v','pid':1}), \
             patch.object(runtime,'launch_restart') as launch,patch.object(server,'runtime_busy',return_value=False), \
             patch.object(server.threading,'Timer') as timer:
            self.post()
            launch.assert_called_once_with(server.PORT)
            timer.return_value.start.assert_called_once()
            with self.assertRaises(ValueError):server.submit_task('fetch',{})

    def test_schedule_receipts_exclude_dry_run_test_and_partial_success(self):
        runs=[{'time':'today','ok':True,'channel':'none','error':'https://unit.invalid/?key=secret token=secret'}]
        pushes=[{'time':'old','ok':True,'channel':'wecom','source':'weekly','audience':'class'},
                {'time':'test','ok':True,'channel':'wecom','source':'manual-test','audience':'class'},
                {'time':'dry','ok':True,'channel':'none','source':'weekly'},
                {'time':'partial','ok':False,'channel':'wecom','source':'weekly'},
                {'time':'duplicate','ok':True,'channel':'wecom','source':'weekly','note':'duplicate-skipped'}]
        (self.root/'task_log.jsonl').write_text('\n'.join(map(json.dumps,runs)))
        (self.root/'push_log.jsonl').write_text('\n'.join(map(json.dumps,pushes)))
        with patch.object(schedule,'ROOT',self.root),patch.object(schedule,'_cache',None), \
             patch.object(schedule,'_query_windows',return_value={'available':True,'tasks':[]}) as query:
            state=schedule.snapshot();schedule.snapshot();schedule.snapshot(True)
        self.assertEqual(query.call_count,2)
        self.assertEqual(state['last_success']['class']['time'],'old')
        self.assertNotIn('secret',state['last_attempt']['error'])
        self.assertEqual(state['last_attempt']['channel'],'none')

    def test_busy_checks_persisted_queued_work(self):
        with patch.object(server,'TASKS',{}):
            self.assertFalse(server.runtime_busy())
            import cache_store
            cache_store.register_task('queued','sig','fetch',{})
            self.assertTrue(server.runtime_busy())

    def test_broken_update_keeps_current_backend_alive(self):
        with patch.object(server,'_RESTARTING',False), \
             patch.object(runtime,'status',return_value={'disk_version':'v','pid':1}), \
             patch.object(runtime,'launch_restart',side_effect=SyntaxError('bad source')), \
             patch.object(server,'runtime_busy',return_value=False),patch.object(server.threading,'Timer') as timer:
            self.assertEqual(self.post()[0],409)
            self.assertFalse(server._RESTARTING)
            timer.assert_not_called()
