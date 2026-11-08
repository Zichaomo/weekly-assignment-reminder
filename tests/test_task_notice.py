import sys
import unittest
from unittest.mock import patch
import task_notice


class TaskNoticeTests(unittest.TestCase):
    def test_launch_only_starts_detached_local_helper(self):
        with patch.object(task_notice.os,'name','nt'), patch.object(task_notice.subprocess,'Popen') as launch:
            self.assertTrue(task_notice.launch('start'))
        command = launch.call_args.args[0]
        self.assertTrue(command[1].endswith('task_notice.py'))
        self.assertEqual(command[2:],[ 'start','0'])
        self.assertEqual(launch.call_args.kwargs['creationflags'],0x08000000)
        self.assertNotIn('webhook',' '.join(command))

    def test_unavailable_notification_cannot_interrupt_task(self):
        with patch.object(task_notice.os,'name','nt'), patch.object(task_notice.subprocess,'Popen',side_effect=OSError('fixture')):
            self.assertFalse(task_notice.launch('start'))

    def test_helper_uses_only_local_balloon_and_never_group_alert(self):
        import alert
        with patch.object(sys,'argv',['task_notice.py','success','42']), patch.object(alert,'_ps_balloon',return_value=(True,'ok')) as balloon, patch.object(alert,'send',side_effect=AssertionError('group alert forbidden')):
            self.assertEqual(task_notice.main(),0)
        self.assertIn('42',balloon.call_args.args[1])

    def test_notice_text_explains_allowed_actions_and_partial_failure(self):
        title,text=task_notice.message('start')
        self.assertIn('30–90秒',text)
        self.assertIn('本地列表和预览',text)
        self.assertIn('部分消息可能已发送',task_notice.message('failure',20)[1])
