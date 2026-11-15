"""Mocked delivery recovery for a refused local proxy, with no real requests."""
import errno
import io
import json
import ssl
from contextlib import redirect_stderr
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from helpers import IsolatedCase
import notify


class WecomProxyTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.cfg = {'_wecom_webhook_override': 'https://unit.invalid/send?key=fixture-secret',
                    '_delivery_errors': []}
        self.proxies = patch.object(notify.urllib.request, 'getproxies',
                                    return_value={'https': 'http://127.0.0.1:7890'})
        self.proxies.start()
        self.addCleanup(self.proxies.stop)
        self.bypass = patch.object(notify.urllib.request, 'proxy_bypass', return_value=False)
        self.bypass.start()
        self.addCleanup(self.bypass.stop)
        self.stderr = io.StringIO()
        self.redirect = redirect_stderr(self.stderr)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def refused(self):
        return URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'connection refused'))

    def test_refused_loopback_proxy_retries_same_payload_directly_once(self):
        for kwargs in ({}, {'markdown': True}, {'image_bytes': b'fixture-picture'}):
            with self.subTest(kwargs=kwargs):
                direct = Mock()
                direct.open.return_value = io.BytesIO(b'{"errcode":0}')
                with patch.object(notify.urllib.request, 'urlopen', side_effect=self.refused()) as proxied, \
                     patch.object(notify.urllib.request, 'build_opener', return_value=direct) as build:
                    self.assertTrue(notify.send_wecom('fixture text', self.cfg, **kwargs))
                proxied.assert_called_once()
                direct.open.assert_called_once()
                self.assertEqual(direct.open.call_args.args[0].full_url, proxied.call_args.args[0].full_url)
                self.assertEqual(direct.open.call_args.args[0].data, proxied.call_args.args[0].data)
                self.assertEqual(direct.open.call_args.args[0].get_method(), proxied.call_args.args[0].get_method())
                self.assertEqual(direct.open.call_args.kwargs['timeout'], 60 if kwargs.get('image_bytes') else 20)
                self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(self.cfg['_delivery_errors'], [])

    def test_working_proxy_uses_original_request(self):
        with patch.object(notify.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"errcode":0}')), \
             patch.object(notify.urllib.request, 'build_opener') as build:
            self.assertTrue(notify.send_wecom('fixture', self.cfg))
        build.assert_not_called()

    def test_urllib_proxy_mutation_is_reset_before_direct_retry(self):
        def refused(request, **kwargs):
            request.set_proxy('127.0.0.1:7890', 'http')
            request.add_unredirected_header('Proxy-authorization', 'fixture-proxy-credential')
            raise self.refused()
        direct = Mock()
        direct.open.return_value = io.BytesIO(b'{"errcode":0}')
        with patch.object(notify.urllib.request, 'urlopen', side_effect=refused), \
             patch.object(notify.urllib.request, 'proxy_bypass', side_effect=lambda host: host.startswith('127.0.0.1')), \
             patch.object(notify.urllib.request, 'build_opener', return_value=direct):
            self.assertTrue(notify.send_wecom('fixture', self.cfg))
        request = direct.open.call_args.args[0]
        self.assertEqual(request.host, 'unit.invalid')
        self.assertEqual(request.get_method(), 'POST')
        self.assertIsNone(request.get_header('Proxy-authorization'))
        self.assertEqual(json.loads(request.data)['text']['content'], 'fixture')

    def test_other_network_failures_do_not_trigger_a_second_send(self):
        failures = [URLError(TimeoutError('timed out')), URLError(ssl.SSLError('TLS failed')),
                    URLError(ConnectionResetError('reset after request')),
                    HTTPError('https://unit.invalid/send?key=fixture-secret', 403, 'forbidden', {}, None)]
        for error in failures:
            with self.subTest(error=type(error).__name__), \
                 patch.object(notify.urllib.request, 'urlopen', side_effect=error), \
                 patch.object(notify.urllib.request, 'build_opener') as build:
                self.assertFalse(notify.send_wecom('fixture', self.cfg))
                build.assert_not_called()

    def test_remote_absent_and_bypassed_proxies_do_not_trigger_direct_retry(self):
        for proxies, bypass in [({'https': 'http://proxy.unit.invalid:8080'}, False),
                                ({}, False), ({'https': 'http://127.0.0.1:7890'}, True)]:
            with self.subTest(proxies=proxies, bypass=bypass), \
                 patch.object(notify.urllib.request, 'getproxies', return_value=proxies), \
                 patch.object(notify.urllib.request, 'proxy_bypass', return_value=bypass), \
                 patch.object(notify.urllib.request, 'urlopen', side_effect=self.refused()), \
                 patch.object(notify.urllib.request, 'build_opener') as build:
                self.assertFalse(notify.send_wecom('fixture', self.cfg))
                build.assert_not_called()

    def test_failed_direct_retry_reports_sanitized_reason(self):
        direct = Mock()
        direct.open.side_effect = URLError('https://unit.invalid/send?key=fixture-secret connection refused')
        with patch.object(notify.urllib.request, 'urlopen', side_effect=self.refused()), \
             patch.object(notify.urllib.request, 'build_opener', return_value=direct):
            self.assertFalse(notify.send_wecom('fixture', self.cfg))
        direct.open.assert_called_once()
        self.assertTrue(self.cfg['_delivery_errors'])
        self.assertIn('直连', self.cfg['_delivery_errors'][-1])
        self.assertNotIn('fixture-secret', str(self.cfg['_delivery_errors']) + self.stderr.getvalue())

    def test_api_failure_reports_code_and_redacted_error(self):
        payload = json.dumps({'errcode': 40013, 'errmsg': 'invalid key=fixture-secret'}).encode()
        with patch.object(notify.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            self.assertFalse(notify.send_wecom('fixture', self.cfg))
        self.assertIn('40013', self.cfg['_delivery_errors'][-1])
        self.assertNotIn('fixture-secret', str(self.cfg['_delivery_errors']) + self.stderr.getvalue())
