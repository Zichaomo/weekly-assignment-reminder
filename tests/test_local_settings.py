"""First-run setup and console-managed credentials and delivery settings."""
import importlib
import json
from unittest.mock import patch

from helpers import IsolatedCase
import server


class SettingsApiTests(IsolatedCase):
    def handler(self, path, body=None, origin=None):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.headers = {'Host': '127.0.0.1:8277', 'Origin': origin or 'http://127.0.0.1:8277',
                           'Content-Type': 'application/json'}
        handler._send = lambda code, data, **kw: (code, data)
        handler._body = lambda: body or {}
        return handler

    def test_management_entries_are_available_without_secrets(self):
        for path in ('/api/settings/account', '/api/settings/wecom', '/api/setup'):
            with self.subTest(path=path):
                code, data = self.handler(path)._do_GET()
                self.assertEqual(code, 200)
                self.assertTrue(data['ok'])

    def test_external_origin_cannot_mutate_settings(self):
        handler = self.handler('/api/settings/wecom', {'channel': 'wecom'}, 'https://example.com')
        code, _ = handler._do_POST()
        self.assertEqual(code, 403)

    def test_login_tasks_do_not_persist_credential_parameters(self):
        with patch.object(server, 'submit_task', return_value=('fixture-login', False)) as submit:
            code, _ = self.handler('/api/tasks', {'op': 'login', 'password': 'fixture'})._do_POST()
        self.assertEqual(code, 200)
        self.assertEqual(submit.call_args.args, ('login', {}))

    def test_busy_console_refuses_configuration_changes(self):
        with patch.object(server, 'runtime_busy', return_value=True):
            code, _ = self.handler('/api/settings/account', {})._do_POST()
        self.assertEqual(code, 409)

    def test_login_child_is_bounded_and_output_is_not_returned(self):
        from types import SimpleNamespace
        with patch.object(server.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='fixture-private-output', stderr='')) as run:
            result = server.run_task('login', {})
        self.assertTrue(result['ok'])
        self.assertFalse('fixture-private-output' in json.dumps(result))
        self.assertIn('--fresh', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 600)

    def test_course_completion_updates_config_under_platform_lock(self):
        def mark():
            self.assertTrue(lock.return_value.__enter__.called)
            self.assertFalse(lock.return_value.__exit__.called)
        with patch.object(server.hub, 'run_refresh_courses', return_value={'ok': True}), \
                patch.object(server, 'FetchLock') as lock, \
                patch.object(server.local_settings, 'mark_courses_refreshed', side_effect=mark):
            self.assertTrue(server.run_task('refresh-courses', {})['ok'])


class LocalSettingsTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.settings = importlib.import_module('local_settings')

    def revision(self):
        return self.settings.account_status()['revision']

    def test_password_is_encrypted_and_not_returned(self):
        password = 'fixture-password-' + 'only'
        data = self.settings.save_account({'expected_revision': self.revision(),
                                          'username': 'fixture-student', 'password': password})
        self.assertTrue(data['password_saved'])
        self.assertFalse(password in json.dumps(data))
        self.assertFalse(password in self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(self.settings.credentials(), ('fixture-student', password))

    def test_edge_import_uses_native_dpapi_without_optional_dependencies(self):
        import base64
        import edge_cred
        key = bytes(range(32))
        encoded = base64.b64encode(b'DPAPI' + self.settings._protect(key)).decode()
        state = self.root / 'Local State'
        state.write_text(json.dumps({'os_crypt': {'encrypted_key': encoded}}), encoding='utf-8')
        self.assertTrue(edge_cred._master_key(state) == key)

    def test_personal_webhook_cannot_equal_class_target(self):
        hook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture-only'
        with self.assertRaises(ValueError):
            self.settings.save_wecom({'expected_revision': self.revision(), 'channel': 'wecom',
                                      'wecom_webhook': hook, 'personal_webhook': hook,
                                      'personal_enabled': True, 'personal_channel': 'wecom'})

    def test_first_deploy_launcher_installs_dependencies_and_opens_console(self):
        from helpers import ROOT
        source = (ROOT / 'setup.bat').read_text(encoding='utf-8')
        self.assertTrue('Python314\\python.exe' in source)
        self.assertTrue('-m pip install playwright Pillow' in source)
        self.assertTrue('console.bat' in source)

    def test_blank_password_keeps_existing_and_changed_username_requires_new_one(self):
        self.settings.save_account({'expected_revision': self.revision(),
                                   'username': 'student-a', 'password': 'fixture-password'})
        self.settings.save_account({'expected_revision': self.revision(),
                                   'username': 'student-a', 'password': ''})
        self.assertEqual(self.settings.credentials()[1], 'fixture-password')
        with self.assertRaises(ValueError):
            self.settings.save_account({'expected_revision': self.revision(),
                                       'username': 'student-b', 'password': ''})

    def test_credentials_import_and_clear_use_explicit_actions(self):
        with patch('edge_cred.get_saved_credentials', return_value=('student-a', 'fixture-password')):
            out = self.settings.save_account({'expected_revision': self.revision(), 'action': 'import-edge'})
        self.assertEqual(out['username'], 'student-a')
        self.settings.save_account({'expected_revision': self.revision(), 'action': 'clear'})
        self.assertEqual(self.settings.credentials(), (None, None))

    def test_stale_saves_and_failed_atomic_write_preserve_config(self):
        before = self.config_path.read_bytes()
        with self.assertRaises(self.settings.Conflict):
            self.settings.save_account({'expected_revision': 'stale', 'username': 'x', 'password': 'y'})
        with patch.object(self.settings.os, 'replace', side_effect=OSError('fixture write failed')):
            with self.assertRaises(OSError):
                self.settings.save_account({'expected_revision': self.revision(), 'username': 'x', 'password': 'y'})
        self.assertEqual(before, self.config_path.read_bytes())

    def test_wecom_changes_preserve_unknown_fields_and_blank_links(self):
        hook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture-only'
        self.write_config({'channel': 'wecom', 'wecom_webhook': hook,
                           'scan': {'custom': 12}, 'personal': {'channel': 'pushplus', 'pushplus_token': 'fixture'}})
        out = self.settings.save_wecom({'expected_revision': self.revision(), 'channel': 'wecom',
                                       'wecom_webhook': ''})
        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['wecom_webhook'], hook)
        self.assertEqual(cfg['scan'], {'custom': 12})
        self.assertEqual(cfg['personal']['channel'], 'pushplus')
        self.assertFalse('fixture-only' in json.dumps(out))

    def test_invalid_hook_and_missing_main_hook_reject_without_writes(self):
        before = self.config_path.read_bytes()
        for hook in ('https://example.com/?key=fixture', ''):
            with self.subTest(hook=bool(hook)), self.assertRaises(ValueError):
                self.settings.save_wecom({'expected_revision': self.revision(), 'channel': 'wecom',
                                          'wecom_webhook': hook})
        self.assertEqual(before, self.config_path.read_bytes())

    def test_new_account_invalidates_saved_session_and_gets_new_cache_scope(self):
        self.settings.save_account({'expected_revision': self.revision(), 'username': 'a', 'password': 'x'})
        self.settings.mark_logged_in()
        self.assertFalse(self.settings.needs_login())
        self.settings.save_account({'expected_revision': self.revision(), 'username': 'b', 'password': 'y'})
        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertTrue(cfg['account_scope'].startswith('school-'))
        self.assertTrue(self.settings.needs_login())

    def test_pending_account_cannot_reuse_an_old_login_state(self):
        import login
        with patch.object(self.settings, 'needs_login', return_value=True), \
                patch.object(login, 'sync_playwright') as browser:
            self.assertFalse(login._logged_in_via_state())
        browser.assert_not_called()

    def test_saved_credentials_invalidate_old_cached_scans(self):
        import cache_store
        with patch.object(cache_store, 'invalidate_scope_scans') as invalidate:
            self.settings.save_account({'expected_revision': self.revision(),
                                       'username': 'fixture', 'password': 'fixture'})
        invalidate.assert_called_once()

    def test_notify_uses_console_saved_channel(self):
        import notify
        self.settings.save_wecom({'expected_revision': self.revision(), 'channel': 'none'})
        self.assertEqual(notify.load_config()['channel'], 'none')

    def test_auth_failure_points_to_console_account_panel(self):
        import hub
        import weekly
        with patch.object(weekly, 'fetch', side_effect=weekly.AuthenticationRequired('fixture')),\
                patch.object(weekly, 'ensure_login', return_value=False):
            result = hub.run_list(7, True)
        self.assertIn('学校账号', result['error'])

    def test_identity_conflict_points_to_console_account_panel(self):
        import cache_store
        cache_store.bind_identity('fixture-a')
        with self.assertRaises(cache_store.AccountIdentityConflict) as conflict:
            cache_store.bind_identity('fixture-b')
        self.assertIn('学校账号', str(conflict.exception))

    def test_managed_credentials_override_browser_autofill(self):
        import login
        from unittest.mock import Mock
        from contextlib import redirect_stdout
        import io
        user, password = Mock(), Mock()
        values = {'user': 'old-account', 'password': 'old-password'}
        user.input_value.side_effect = lambda **kw: values['user']
        password.input_value.side_effect = lambda **kw: values['password']
        user.fill.side_effect = lambda value: values.update(user=value)
        password.fill.side_effect = lambda value: values.update(password=value)
        with patch.object(self.settings, 'credentials', return_value=('managed-account', 'managed-password')), \
                patch.object(login, 'ensure_password_tab', return_value=True), \
                patch.object(login, 'find_login_frame', return_value=Mock()), \
                patch.object(login, 'is_new_sso', return_value=True), \
                patch.object(login, '_visible', side_effect=[user, password]), \
                patch.object(login, '_sync_values') as sync, \
                patch.object(login, '_click_submit', return_value='ok'), \
                patch.object(login.time, 'sleep'), redirect_stdout(io.StringIO()):
            self.assertEqual(login.auto_submit(Mock()), 'ok')
        self.assertTrue(sync.call_args.args[1:] == ('managed-account', 'managed-password'))

    def test_first_run_status_is_local_and_completion_creates_safe_config(self):
        self.config_path.unlink()
        with patch('subprocess.run', side_effect=AssertionError('first-run GET must stay local')):
            status = self.settings.setup_status()
        self.assertTrue(status['needs_setup'])
        self.settings.complete_setup({'expected_revision': status['revision']})
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8'))['channel'], 'clipboard')
        self.assertFalse(self.settings.setup_status()['needs_setup'])
