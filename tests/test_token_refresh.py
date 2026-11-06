"""Offline renewal/401 contracts using synthetic tokens and a fake upstream."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import types
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from token_refresh import CLIENT_ID, RESOURCE, RefreshError, TokenRefresher, validate_token
from server import Handler
from diagnostics import safe_record
import test_native_api as fixtures
import token_helper


SETTINGS = {'database': 'fixture.db', 'object_id': 'user-one', 'tenant_id': 'tenant-one',
            'username': 'one@example.invalid', 'python': 'python'}


def token(expiry, label='one', **extra):
    fields = {'aud': RESOURCE, 'azp': CLIENT_ID, 'scp': 'LLM.Read',
              'oid': SETTINGS['object_id'], 'tid': SETTINGS['tenant_id'], 'exp': expiry, **extra}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip('=') + '.' + label


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.time = 1000
        self.config = ('https://loki.delve.office.com/api/v1/llmapi',
                       {'Authorization': 'Bearer ' + token(1100)}, fixtures.CATALOG)
        self.acquire = Mock(return_value=(token(4600), 4600))
        self.log = Mock()
        self.manager = TokenRefresher(SETTINGS, self.log, loader=lambda: self.config,
                                      acquire=self.acquire, clock=lambda: self.time)
        self.binding = patch('token_refresh.locate_db', return_value=Path('fixture.db'))
        self.binding.start()

    def tearDown(self):
        self.manager.close()
        self.binding.stop()

    def test_readonly_does_not_refresh_and_request_renews_near_expiry(self):
        self.manager.get_config(refresh=False)
        self.acquire.assert_not_called()
        result = self.manager.get_config()
        self.assertEqual(result[1]['Authorization'], 'Bearer ' + token(4600))
        self.manager.get_config()
        self.acquire.assert_called_once_with(False)
        self.assertEqual(self.config[1]['Authorization'], 'Bearer ' + token(1100))

    def test_timer_renews_without_a_model_request(self):
        completed = threading.Event()
        self.acquire.side_effect = lambda force: (completed.set() or token(4600), 4600)
        self.manager.start()
        self.assertTrue(completed.wait(3))
        self.manager.get_config(refresh=False)  # synchronize with completed refresh
        self.assertEqual(self.manager.token, token(4600))

    def test_scheduled_near_expiry_forces_refresh(self):
        self.manager.get_config()
        self.time = 4350
        self.acquire.return_value = (token(7950), 7950)
        self.manager.get_config()
        self.assertEqual(self.acquire.call_args.args, (True,))

    def test_concurrent_401_requests_share_one_forced_refresh(self):
        self.manager.get_config()
        previous = self.manager.token
        self.acquire.return_value = (token(4700, 'new'), 4700)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(self.manager.after_401, [previous] * 12))
        self.assertEqual(self.acquire.call_count, 2)
        self.assertTrue(all(t == token(4700, 'new') for t in results))
        self.assertEqual(self.acquire.call_args.args, (True,))

    def test_bad_token_not_replayed_and_backoff_limits_broker_calls(self):
        self.manager.get_config()
        previous = self.manager.token
        self.acquire.side_effect = RefreshError('interaction_required')
        for _ in range(12):
            self.assertIsNone(self.manager.after_401(previous))
        self.assertEqual(self.acquire.call_count, 2)
        self.assertEqual(self.manager.last_error, 'interaction_required')
        self.assertEqual(self.manager.get_config()[1]['Authorization'], 'Bearer ' + previous)

    def test_same_token_from_force_refresh_is_not_retried(self):
        self.manager.get_config()
        self.assertIsNone(self.manager.after_401(self.manager.token))

    def test_account_switch_during_acquisition_discards_token(self):
        def acquire(force):
            self.config = (self.config[0], {'Authorization': 'Bearer ' + token(4600, oid='other')}, self.config[2])
            return token(4600), 4600
        self.acquire.side_effect = acquire
        with self.assertRaises(RefreshError):
            self.manager.get_config()
        self.assertIsNone(self.manager.token)

    def test_database_switch_never_uses_cached_identity(self):
        self.manager.get_config()
        with patch('token_refresh.locate_db', return_value=Path('other.db')):
            with self.assertRaises(RefreshError):
                self.manager.get_config()
        self.assertIsNone(self.manager.token)

    def test_malformed_or_wrong_identity_token_is_rejected(self):
        for bad, expiry in [('bad\nheader', 4600), (token(1010), 4600),
                            (token(4600, aud='wrong'), 4600), (token(4600), float('nan')),
                            (token(4600, oid='other'), 4600)]:
            with self.subTest(token=bad):
                with self.assertRaises(RefreshError):
                    validate_token(bad, expiry, SETTINGS, now=1000)

    def test_refresh_diagnostics_never_include_secrets_or_accounts(self):
        data = safe_record({'token_refresh_enabled': True, 'token_refresh_state': 'interaction_required',
                            'token_refresh_failures': 1, 'access_token': 'PRIVATE', 'username': 'PRIVATE'})
        self.assertEqual(len(data), 3)
        self.assertNotIn('PRIVATE', json.dumps(data))


class RetryWireTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.default_effort, self.server.upstream_timeout = 'xhigh', 10
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.config = ('https://loki.delve.office.com/api/v1/llmapi', {'Authorization': 'Bearer old'}, fixtures.CATALOG)
        self.refresher = Mock()
        self.refresher.get_config.return_value = self.config
        self.refresher.after_401.return_value = 'new'
        self.server.token_refresher = self.refresher

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def send(self, responses, path='/desktop/v1/messages', stream=False):
        body = json.dumps({'model': 'claude-opus-5', 'messages': [], 'max_tokens': 16, 'stream': stream}).encode()
        class Upstream(fixtures.FakeUpstream):
            def request(self, method, path, body, headers):
                self.captured = method, path, bytes(body), dict(headers)
        upstreams = [Upstream(r) for r in responses]
        with patch('server.http.client.HTTPSConnection', side_effect=upstreams) as factory:
            request = Request(self.url + path + '?beta=true', data=body,
                              headers={'Content-Type': 'application/json', 'anthropic-beta': 'context-1m-2025-08-07'})
            try:
                with urlopen(request, timeout=5) as response:
                    result = (response.status, response.read())
            except HTTPError as error:
                result = (error.code, error.read())
                error.close()
        return result, upstreams, factory.call_count

    def test_401_then_success_preserves_body_query_beta_and_call_id(self):
        result, upstreams, count = self.send([fixtures.FakeResponse(b'{}', status=401),
                                            fixtures.FakeResponse(b'{"type":"message","content":[]}')])
        self.assertEqual(result[0], 200)
        self.assertEqual(count, 2)
        self.refresher.after_401.assert_called_once_with('old')
        first, second = [u.captured for u in upstreams]
        self.assertEqual(first[:3], second[:3])
        self.assertEqual(second[1], '/api/v1/llmapi/v1/messages?beta=true')
        self.assertEqual(second[3]['anthropic-beta'], 'context-1m-2025-08-07')
        self.assertEqual(first[3]['Authorization'], 'Bearer old')
        self.assertEqual(first[3]['X-ClientCallId'], second[3]['X-ClientCallId'])
        self.assertEqual(second[3]['Authorization'], 'Bearer new')

    def test_repeated_401_stops_after_one_retry(self):
        result, _, count = self.send([fixtures.FakeResponse(b'{}', status=401), fixtures.FakeResponse(b'{}', status=401)])
        self.assertEqual((result[0], count), (401, 2))
        self.refresher.after_401.assert_called_once()

    def test_403_and_429_are_not_replayed(self):
        for status in (403, 429):
            result, _, count = self.send([fixtures.FakeResponse(b'{}', status=status)])
            self.assertEqual((result[0], count), (status, 1))
        self.refresher.after_401.assert_not_called()


    def test_refresh_failure_preserves_original_401(self):
        self.refresher.after_401.return_value = None
        result, _, count = self.send([fixtures.FakeResponse(b'{}', status=401)])
        self.assertEqual((result[0], count), (401, 1))

    def test_native_stream_401_retries_before_any_stream_bytes(self):
        wire = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        result, _, count = self.send([fixtures.FakeResponse(b'{}', status=401),
            fixtures.FakeResponse(wire, 'text/event-stream')], stream=True)
        self.assertEqual(result, (200, wire))
        self.assertEqual(count, 2)

    def test_stream_error_after_200_never_replays(self):
        wire = b'event: error\ndata: {"type":"error","error":{"type":"authentication_error"}}\n\n'
        _, _, count = self.send([fixtures.FakeResponse(wire, 'text/event-stream')], stream=True)
        self.assertEqual(count, 1)
        self.refresher.after_401.assert_not_called()


class BrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Mock()
        self.app.get_accounts.return_value = [{'username': SETTINGS['username'],
            'local_account_id': SETTINGS['object_id'], 'realm': SETTINGS['tenant_id']}]
        self.app.acquire_token_silent_with_error.return_value = {
            'access_token': token(5000), 'expires_in': 4000,
            'id_token_claims': {'oid': SETTINGS['object_id'], 'tid': SETTINGS['tenant_id']}}
        self.factory = Mock(return_value=self.app)
        self.native_params = Mock()
        self.native_builder = Mock(return_value=self.native_params)
        self.broker = types.SimpleNamespace(_build_msal_runtime_auth_params=self.native_builder)
        sdk = types.SimpleNamespace(PublicClientApplication=self.factory, broker=self.broker, __version__='1.39.0')
        extensions = types.SimpleNamespace(FilePersistenceWithDataProtection=Mock(), PersistedTokenCache=Mock())
        for patcher in (patch.dict('sys.modules', {'msal': sdk, 'msal_extensions': extensions}),
                        patch('token_helper.profile_dir', return_value=Path(self.temp.name)),
                        patch('token_helper.time.time', return_value=1000)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_force_refresh_is_forwarded_and_cache_is_persistent(self):
        response = self.app.acquire_token_silent_with_error.return_value
        def invoke(*args, **kwargs):
            if kwargs.get('force_refresh'):
                self.broker._build_msal_runtime_auth_params(CLIENT_ID, 'authority')
            return response
        self.app.acquire_token_silent_with_error.side_effect = invoke
        result, _ = token_helper.acquire(SETTINGS, force=True)
        self.assertEqual(result, token(5000))
        self.assertTrue(self.app.acquire_token_silent_with_error.call_args.kwargs['force_refresh'])
        self.assertTrue(self.factory.call_args.kwargs['enable_broker_on_windows'])
        self.assertIsNotNone(self.factory.call_args.kwargs['token_cache'])
        self.native_params.set_access_token_to_renew.assert_called_once_with(token(5000))
        self.assertIs(self.broker._build_msal_runtime_auth_params, self.native_builder)

    def test_force_refresh_renews_the_rejected_token_and_restores_adapter_on_failure(self):
        def fail(*args, **kwargs):
            self.broker._build_msal_runtime_auth_params(CLIENT_ID, 'authority')
            raise OSError('synthetic broker failure')
        self.app.acquire_token_silent_with_error.side_effect = fail
        with self.assertRaises(OSError):
            token_helper.acquire(SETTINGS, force=True, token_to_renew='rejected-fixture')
        self.native_params.set_access_token_to_renew.assert_called_once_with('rejected-fixture')
        self.assertIs(self.broker._build_msal_runtime_auth_params, self.native_builder)

    def test_background_acquisition_never_opens_login(self):
        self.app.get_accounts.return_value = []
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            token_helper.acquire(SETTINGS)
        self.app.acquire_token_interactive.assert_not_called()

    def test_wam_common_realm_uses_exact_home_tenant_and_object(self):
        account = self.app.get_accounts.return_value[0]
        account.update(realm='common', home_account_id='home-user.' + SETTINGS['tenant_id'])
        result, _ = token_helper.acquire(SETTINGS)
        self.assertEqual(result, token(5000))
        self.app.acquire_token_interactive.assert_not_called()

    def test_wam_account_with_another_home_tenant_is_not_used(self):
        account = self.app.get_accounts.return_value[0]
        account.update(realm='common', home_account_id='home-user.other-tenant')
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            token_helper.acquire(SETTINGS)
        self.app.acquire_token_silent_with_error.assert_not_called()

    def test_broker_result_for_another_account_is_rejected(self):
        self.app.acquire_token_silent_with_error.return_value['id_token_claims']['oid'] = 'other'
        with self.assertRaisesRegex(RefreshError, 'account_mismatch'):
            token_helper.acquire(SETTINGS)



if __name__ == '__main__':
    unittest.main()
