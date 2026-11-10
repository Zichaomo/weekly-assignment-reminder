"""Keep login, stored-state checks and scraping on the same page evidence."""
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from helpers import FakePage, IsolatedCase

import login
import scrape


PERSONAL = 'https://course.buct.edu.cn/meol/personal.do'
STATISTICS = '在线总时长 12 小时 登录时间 今天'


def auth_page(text='', url=PERSONAL, selectors=None):
    page = FakePage(text=text, url=url, selector_values=selectors)
    page.frames = [page]
    page.main_frame = page
    page.context = SimpleNamespace(cookies=Mock(return_value=[{'name': 'CASTGC'}]))
    page.wait_for_timeout = Mock()
    page.screenshot = Mock()
    return page


class LoginPageEvidenceTests(unittest.TestCase):
    def test_all_predicates_reject_unverified_pages_even_with_ticket(self):
        cases = [
            ('blank SSO', auth_page(url=login.LOGIN_URL)),
            ('blank personal page', auth_page()),
            ('portal shell', auth_page('我的课程 退出', 'https://portal.buct.edu.cn/')),
            ('new portal', auth_page('个人中心 注销', 'https://sidportal.buct.edu.cn/')),
            ('SSO form', auth_page(STATISTICS, 'https://sidportal.buct.edu.cn/login')),
            ('verification prompt', auth_page('用户名密码 验证码 我的课程')),
            ('old platform login', auth_page('我的课程 退出',
                                           'https://course.buct.edu.cn/meol/login.jsp')),
            ('visible password', auth_page(STATISTICS,
                                          selectors={"input[type='password']": True})),
            ('foreign page', auth_page(STATISTICS, 'https://evilbuct.edu.cn/meol/personal.do')),
            ('lookalike host', auth_page(STATISTICS,
                                        'https://course.buct.edu.cn.example.com/meol/personal.do')),
            ('login return query', auth_page('统一身份认证登录 我的课程', login.LOGIN_URL)),
        ]
        for name, page in cases:
            with self.subTest(name=name):
                self.assertFalse(login.is_logged_in(page))
                self.assertFalse(scrape.page_authenticated(page))
                self.assertFalse(scrape.logged_in(page, page.context))
                self.assertFalse(scrape.wait_authenticated(page, page.context, timeout=0))

    def test_valid_platform_pages_work_without_ticket(self):
        pages = [
            auth_page(STATISTICS + ' 统一身份认证登录'),
            auth_page(STATISTICS, PERSONAL + '?back=sso_login.jsp'),
            auth_page('我的课程', 'https://course.buct.edu.cn/meol/index.jsp'),
            auth_page(selectors={'#userName': '脱敏同学'}),
        ]
        for page in pages:
            page.context.cookies.return_value = []
            with self.subTest(url=page.url, text=page.text):
                self.assertTrue(login.is_logged_in(page))
                self.assertTrue(scrape.page_authenticated(page))
                self.assertTrue(scrape.logged_in(page, page.context))
                self.assertTrue(scrape.wait_authenticated(page, page.context, timeout=0))

    def test_login_frame_text_cannot_authenticate_its_parent(self):
        page = auth_page(url='https://sidportal.buct.edu.cn/login')
        page.frames.append(auth_page(STATISTICS, 'https://example.com/embedded'))
        self.assertFalse(login.is_logged_in(page))
        self.assertFalse(scrape.page_authenticated(page))


class LoginStateCheckTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.state_file = self.root / 'storage_state.json'
        self.state_file.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        self.state_patch = patch.object(login, 'STATE_FILE', self.state_file)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.page = auth_page(STATISTICS)
        self.context = Mock(pages=[self.page])
        self.context.new_page.return_value = self.page
        self.browser = Mock()
        self.browser.new_context.return_value = self.context
        self.playwright = Mock()
        self.playwright.chromium.launch.return_value = self.browser
        self.runtime = patch.object(login, 'sync_playwright')
        self.start = self.runtime.start()
        self.start.return_value.__enter__.return_value = self.playwright
        self.addCleanup(self.runtime.stop)

    def test_missing_state_is_false_without_launching_browser(self):
        self.state_file.unlink()
        self.assertFalse(login._logged_in_via_state())
        self.start.assert_not_called()

    def test_corrupt_state_is_false_and_browser_is_closed(self):
        self.state_file.write_text('{bad json', encoding='utf-8')
        self.browser.new_context.side_effect = json.JSONDecodeError('invalid state', '{', 0)
        self.assertFalse(login._logged_in_via_state())
        self.browser.close.assert_called_once()

    def test_invalid_state_schema_is_false_and_browser_is_closed(self):
        self.browser.new_context.side_effect = PlaywrightError('invalid cookies schema')
        self.assertFalse(login._logged_in_via_state())
        self.browser.close.assert_called_once()

    def test_navigation_failure_is_false_and_browser_is_closed(self):
        self.page.goto = Mock(side_effect=PlaywrightError('navigation timed out'))
        self.assertFalse(login._logged_in_via_state())
        self.browser.close.assert_called_once()

    def test_state_check_uses_bounded_shared_wait_and_stored_state(self):
        self.page.text = ''
        self.page.url = login.LOGIN_URL
        with patch.object(scrape, 'wait_authenticated', return_value=True) as wait:
            self.assertTrue(login._logged_in_via_state())
        wait.assert_called_once_with(self.page, self.context, timeout=12)
        self.browser.new_context.assert_called_once_with(
            storage_state=str(self.state_file), ignore_https_errors=True)
        self.assertEqual(self.page.gotos, [login.LOGIN_URL])
        self.browser.close.assert_called_once()

    def test_state_check_never_bypasses_shared_rejection_with_frame_text(self):
        self.page.url = 'https://sidportal.buct.edu.cn/login'
        self.page.frames.append(auth_page(STATISTICS, 'https://example.com/embedded'))
        with patch.object(scrape, 'wait_authenticated', return_value=False) as wait:
            self.assertFalse(login._logged_in_via_state())
        wait.assert_called_once()
        self.browser.close.assert_called_once()

    def test_check_cli_retains_status_tokens_and_exit_codes(self):
        for valid, code, token in ((False, 1, 'NEED_LOGIN'), (True, 0, 'LOGGED_IN')):
            output = io.StringIO()
            with self.subTest(valid=valid), patch.object(login.sys, 'argv', ['login.py', '--check']), \
                    patch.object(login, 'PROFILE_DIR', self.root / 'profile'), \
                    patch.object(login, 'FetchLock'), \
                    patch.object(login, '_logged_in_via_state', return_value=valid), \
                    redirect_stdout(output):
                self.assertEqual(login.main(), code)
                self.assertEqual(output.getvalue().strip(), token)


class LoginEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.page = auth_page()
        # The fixture represents the final page after the SSO redirect.
        self.page.goto = Mock()
        self.context = Mock(pages=[self.page])
        self.context.new_page.return_value = self.page
        self.runtime = patch.object(login, 'sync_playwright')
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_automatic_login_does_not_save_blank_personal_page_with_ticket(self):
        with patch.object(login, 'launch_any', return_value=self.context), \
                patch.object(login, 'auto_submit', return_value='nopass'), \
                patch.object(login, 'has_captcha', return_value=False), \
                patch.object(login.time, 'sleep'), \
                patch.object(login.time, 'time', side_effect=[0, 100]), \
                patch.object(login, 'save_state') as save:
            self.assertEqual(login.run_auto(headless=True, no_wait=True), 1)
        save.assert_not_called()
        self.context.close.assert_called_once()

    def test_manual_login_does_not_save_blank_personal_page_with_ticket(self):
        with patch.object(login, 'launch_ctx', return_value=self.context), \
                patch.object(login.time, 'sleep'), \
                patch.object(login.time, 'time', side_effect=[0, 1, 301]), \
                patch.object(login, 'save_state') as save:
            self.assertEqual(login.run_manual(), 1)
        save.assert_not_called()
        self.context.close.assert_called_once()

    def test_automatic_login_still_saves_verified_platform_session(self):
        self.page.text = STATISTICS
        with patch.object(login, 'launch_any', return_value=self.context), \
                patch.object(login.time, 'sleep'), \
                patch.object(login, 'save_state', return_value=True) as save:
            self.assertEqual(login.run_auto(headless=True, no_wait=True), 0)
        save.assert_called_once()

    def test_automatic_login_finishes_device_prompt_before_platform_check(self):
        self.page.url = 'https://sidportal.buct.edu.cn/'

        def dismiss(page):
            page.url = PERSONAL
            page.text = STATISTICS
            return True

        with patch.object(login, 'launch_any', return_value=self.context), \
                patch.object(login, 'auto_submit', return_value='ok'), \
                patch.object(login, 'dismiss_device_dialog', side_effect=dismiss), \
                patch.object(login, 'has_captcha', return_value=False), \
                patch.object(login.time, 'sleep'), \
                patch.object(login.time, 'time', side_effect=[0, 100]), \
                patch.object(login, 'save_state', return_value=True) as save:
            self.assertEqual(login.run_auto(headless=True, no_wait=True), 0)
        save.assert_called_once()
        self.context.close.assert_called_once()

    def test_manual_login_still_saves_verified_platform_session(self):
        self.page.text = STATISTICS
        with patch.object(login, 'launch_ctx', return_value=self.context), \
                patch.object(login.time, 'sleep'), \
                patch.object(login.time, 'time', side_effect=[0, 1]), \
                patch.object(login, 'save_state', return_value=True) as save:
            self.assertEqual(login.run_manual(), 0)
        save.assert_called_once()

    def test_pending_credentials_require_fresh_manual_context(self):
        self.page.text = STATISTICS
        with patch.object(login.local_settings, 'needs_login', return_value=True), \
                patch.object(login, 'launch_ctx', return_value=self.context), \
                patch.object(login, 'launch_fresh', return_value=self.context, create=True) as fresh, \
                patch.object(login.time, 'sleep'), \
                patch.object(login.time, 'time', side_effect=[0, 1]), \
                patch.object(login, 'save_state', return_value=True):
            self.assertEqual(login.run_manual(), 0)
        fresh.assert_called_once()

    def test_session_is_rechecked_immediately_before_saving(self):
        with patch.object(login.time, 'sleep'):
            self.assertFalse(login.save_state(self.context, self.page, 'test login'))
        self.context.storage_state.assert_not_called()


if __name__ == '__main__':
    unittest.main()
