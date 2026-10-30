import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET
from helpers import IsolatedCase
import schedule_control as control
import schedule_status
import server


class ScheduleControl(IsolatedCase):
    def test_future_boundary_never_triggers_immediately(self):
        now=datetime(2026,11,1,10,0)
        self.assertEqual(control.next_sunday('09:00',now),'2026-11-08T09:00:00')
        self.assertEqual(control.next_sunday('11:00',now),'2026-11-01T11:00:00')
        self.assertEqual(control.next_sunday('00:05',now),'2026-11-08T00:05:00')

    def test_xml_is_sunday_only_and_preserves_retry_and_catchup_time(self):
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for catchup in (False,True):
            xml=ET.fromstring(control.definition('10:30',14,catchup))
            self.assertIsNotNone(xml.find('.//t:Sunday',ns))
            args=xml.find('.//t:Arguments',ns).text
            self.assertIn('--wecom config --days 14',args)
            self.assertNotIn('--run-now',args)
            if catchup:self.assertIn('--target-time 10:30',args)
            else:self.assertIsNotNone(xml.find('.//t:Repetition',ns))

    def test_invalid_inputs_never_invoke_windows(self):
        with patch.object(control.subprocess,'run') as run:
            for params in [('set','25:00',7,True),('set','09:00',0,True),('oops','09:00',7,True),('set','09:00',True,True)]:
                with self.assertRaises(ValueError):control.change(*params)
        run.assert_not_called()

    def test_cancel_and_set_use_fixed_transactional_script(self):
        import base64
        with patch.object(control.os,'name','nt'),patch.object(control.subprocess,'run',return_value=SimpleNamespace(stdout='{"ok":true}')) as run:
            self.assertTrue(control.change('cancel')['ok'])
        script=base64.b64decode(run.call_args.args[0][-1]).decode('utf-16-le')
        self.assertIn('$t.Enabled=$false',script)
        self.assertIn("'BUCT-Homework-Weekly','BUCT-Homework-Catchup'",script)
        self.assertIn('$old[$name].xml',script)
        self.assertNotIn('.Run(',script)
        self.assertEqual(run.call_args.kwargs['timeout'],30)

    def test_failed_mutation_reports_rollback_and_unknown_result_honestly(self):
        with patch.object(control.os,'name','nt'),patch.object(control.subprocess,'run',return_value=SimpleNamespace(stdout='{"ok":false,"rollback_ok":false}')):
            res=control.change('set');self.assertFalse(res['ok']);self.assertFalse(res['rollback_ok'])
        with patch.object(control.os,'name','nt'),patch.object(control.subprocess,'run',return_value=SimpleNamespace(stdout='not json')):
            self.assertIn('无法确认',control.change('cancel')['error'])

    def test_api_rejects_stale_revision_and_busy_without_mutation(self):
        h=object.__new__(server.Handler);h.path='/api/schedule'
        h._send=lambda code,data:(code,data)
        h._body=lambda:{'action':'cancel','expected_revision':'old'}
        with patch.object(server,'_RESTARTING',False),patch.object(server,'runtime_busy',return_value=False), \
             patch.object(schedule_status,'snapshot',return_value={'available':True,'revision':'new'}),patch.object(control,'change') as change:
            self.assertEqual(h._do_POST()[0],409)
        change.assert_not_called()

        with patch.object(server,'runtime_busy',return_value=True),patch.object(control,'change') as change:
            self.assertEqual(h._do_POST()[0],409)
        change.assert_not_called()

    def test_access_denied_requests_uac_once_and_does_not_recurse_in_helper(self):
        denied=SimpleNamespace(stdout='{"ok":false,"code":-2147024891,"rollback_ok":false}')
        with patch.object(control.os,'name','nt'),patch.object(control.subprocess,'run',return_value=denied), \
             patch.object(control,'_elevated_change',return_value={'ok':True}) as elevated:
            self.assertTrue(control.change('set','09:00',7,True)['ok'])
            elevated.assert_called_once_with('set','09:00',7,True)
            self.assertFalse(control.change('cancel',allow_elevation=False)['ok'])
            self.assertEqual(elevated.call_count,1)
