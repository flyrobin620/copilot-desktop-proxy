"""Safe authorization metadata and one-request troubleshooting without upstream access."""
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from authorization import AuthorizationMonitor, account_selection, credential_timing, rejection_category
from check_authorization import check, guidance
from diagnostics import EventLog, safe_record
from server import token_expired, upstream_error


class AuthorizationTests(unittest.TestCase):
    def timing(self, claims):
        # Invalid non-credential fixture; only the decoder's returned JSON is exercised.
        with patch('authorization.base64.urlsafe_b64decode', return_value=json.dumps(claims).encode()):
            return credential_timing({'Authorization': 'Bearer fixture.payload.fixture'}, now=1000)

    def test_only_numeric_expiry_claims_can_be_treated_as_expired(self):
        for claims in ({}, {'exp': None}, {'exp': '10'}, {'exp': True}, {'exp': float('nan')}, [], {'exp': 10**30}, {'exp': 10**1000}):
            with self.subTest(claims=claims):
                self.assertEqual(self.timing(claims), {'credential_timing': 'unknown'})
        self.assertFalse(token_expired({}))
        self.assertEqual(credential_timing({'Authorization': 'Bearer opaque'}), {'credential_timing': 'unknown'})

    def test_timing_distinguishes_expiry_and_clock_skew_without_returning_claims(self):
        self.assertEqual(self.timing({'exp': 1100, 'sub': 'PRIVATE_SUBJECT'}),
                         {'credential_timing': 'not_expired', 'credential_expires_in_seconds': 100,
                          'credential_expires_at': '1970-01-01T00:18:20+00:00'})
        self.assertEqual(self.timing({'exp': 999})['credential_timing'], 'expired')
        self.assertEqual(self.timing({'exp': 1100, 'nbf': 1050})['credential_timing'], 'not_yet_valid')
        self.assertEqual(self.timing({'exp': 1010})['credential_timing'], 'expired')

    def test_issue_time_and_lifetime_are_optional_validated_hints(self):
        value = self.timing({'exp': 1100, 'iat': 900, 'sub': 'PRIVATE_ACCOUNT'})
        self.assertEqual(value['credential_issued_at'], '1970-01-01T00:15:00+00:00')
        self.assertEqual(value['credential_lifetime_seconds'], 200)
        self.assertNotIn('PRIVATE_', json.dumps(value))
        for issued in (None, True, '900', -1, 1101, float('nan'), float('inf'), 10**1000):
            with self.subTest(issued=issued):
                value = self.timing({'exp': 1100, 'iat': issued})
                self.assertEqual(value['credential_expires_in_seconds'], 100)
                self.assertNotIn('credential_issued_at', value)
                self.assertNotIn('credential_lifetime_seconds', value)

    def test_rejection_uses_only_known_machine_codes_not_error_prose(self):
        self.assertEqual(rejection_category(b'{"error":{"code":"Forbidden","innererror":{"code":"ResponsibleAIPolicyViolation"}}}'), 'content_policy')
        self.assertEqual(rejection_category(b'{"error":{"code":"ExpiredAuthenticationToken"}}'), 'credential_expired')
        self.assertEqual(rejection_category(b'{"error":{"code":"ModelAccessDenied"}}'), 'model_access')
        self.assertEqual(rejection_category(b'{}', 'Bearer error="invalid_token"'), 'credential_rejected')
        for raw in (b'<html>expired PRIVATE_BODY</html>', b'{"error":{"message":"token expired PRIVATE_BODY","code":"PRIVATE_CODE"}}', b'[]'):
            self.assertEqual(rejection_category(raw), 'unspecified')

    def test_401_and_403_have_distinct_safe_protocol_errors(self):
        for code, kind in ((401, 'authentication_error'), (403, 'permission_error')):
            error = upstream_error(code, b'{"error":{"message":"PRIVATE_RESPONSE"}}', {})
            self.assertEqual(error['error']['type'], kind)
            self.assertNotIn('PRIVATE_', json.dumps(error))
        self.assertEqual(upstream_error(403, b'{}', {})['error']['upstream_error_category'], 'unspecified')
        for code in (401, 403):
            self.assertEqual(upstream_error(code, b'{}', {}, 'Bearer error="invalid_token"')['error']['upstream_error_category'], 'credential_rejected')

    def test_account_selection_reports_mismatch_without_paths_or_identifiers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root/'PRIVATE_ACCOUNT'/'data.db'
            selected.parent.mkdir()
            selected.touch()
            (root/'.run').mkdir()
            (root/'local-options.json').write_text(json.dumps({'database': str(selected)}))
            (root/'.run/recovery-options.json').write_text(json.dumps({'database': str(root/'PRIVATE_OTHER'/'data.db')}))
            with patch('authorization.APP_ROOT', root), patch('authorization.locate_db', return_value=selected), \
                 patch.dict('os.environ', {'COPILOT_PROXY_DB': str(selected)}):
                summary = account_selection(root)
            self.assertEqual(summary['account_database_count'], 1)
            self.assertTrue(summary['matches_saved_account'])
            self.assertFalse(summary['matches_recovery_account'])
            self.assertNotIn('PRIVATE_', json.dumps(summary))

    def test_diagnostics_whitelist_keeps_timing_but_drops_credentials(self):
        value = safe_record({'credential_timing':'not_expired', 'credential_expires_in_seconds':300,
                             'upstream_error_category':'unspecified', 'credential_readable':True,
                             'Authorization':'PRIVATE_TOKEN','claims':{'sub':'PRIVATE_ACCOUNT'},'database':'PRIVATE_PATH'})
        self.assertEqual(set(value), {'credential_timing','credential_expires_in_seconds','upstream_error_category','credential_readable'})
        self.assertNotIn('PRIVATE_', json.dumps(value))

    def test_checker_does_not_send_a_probe_to_another_folders_worker(self):
        with patch('check_authorization.status', return_value={'running':True,'worker_pid':123,'port':8787}), \
             patch('check_authorization.request', return_value=(200,{'service':'copilot-desktop-proxy','pid':456})) as request:
            result = check()
        self.assertEqual(result['result'], 'start_proxy_from_this_folder')
        self.assertEqual(request.call_count, 1)

    def responses(self, probe):
        live = {'service':'copilot-desktop-proxy','pid':123,'version':'1.1.8'}
        auth = {**live,'credential_readable':True,'credential_timing':'not_expired','Authorization':'PRIVATE_TOKEN'}
        return [(200,live),(200,auth),(200,{'default_model':'claude-opus-5'}),probe,(200,auth)]

    def test_checker_sends_one_minimal_request_and_saves_no_response_content(self):
        responses = self.responses((200,{'type':'message','content':[{'type':'text','text':'PRIVATE_REPLY'}]}))
        with patch('check_authorization.status', return_value={'running':True,'worker_pid':123}), \
             patch('check_authorization.request', side_effect=responses) as request:
            result = check()
        self.assertEqual(result['result'], 'minimal_request_succeeded')
        probes = [call for call in request.call_args_list if call.args[1] == '/v1/messages']
        self.assertEqual(len(probes), 1)
        self.assertNotIn('tools', probes[0].args[2])
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_checker_reports_a_minimal_403_without_replaying_it(self):
        responses = self.responses((403,{'error':{'upstream_error_category':'access_denied','message':'PRIVATE_RESPONSE'}}))
        with patch('check_authorization.status', return_value={'running':True,'worker_pid':123}), \
             patch('check_authorization.request', side_effect=responses) as request:
            result = check()
        self.assertEqual(result['probe_http_status'], 403)
        self.assertEqual(result['rejection'], {'upstream_error_category':'access_denied'})
        self.assertEqual(sum(call.args[1] == '/v1/messages' for call in request.call_args_list), 1)
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_checker_does_not_call_upstream_when_local_credential_is_expired(self):
        responses = self.responses((200,{}))[:2] + [(503,{'status':'authorization_expired'})]
        with patch('check_authorization.status', return_value={'running':True,'worker_pid':123}), \
             patch('check_authorization.request', side_effect=responses) as request:
            result = check()
        self.assertEqual(result['result'], 'local_authorization_unavailable')
        self.assertEqual(request.call_count, 3)
        self.assertIn('No upstream test was sent', guidance(result).replace('no upstream', 'No upstream'))

    def test_checker_preserves_probe_result_when_later_metadata_read_fails(self):
        responses = self.responses((403,{'error':{'upstream_error_category':'access_denied'}}))
        responses[-1] = OSError('PRIVATE_EXCEPTION')
        with patch('check_authorization.status', return_value={'running':True,'worker_pid':123}), \
             patch('check_authorization.request', side_effect=responses) as request:
            result = check()
        self.assertEqual(result['result'], 'minimal_request_failed')
        self.assertEqual(result['probe_http_status'], 403)
        self.assertTrue(result['authorization_after_unavailable'])
        self.assertEqual(sum(call.args[1] == '/v1/messages' for call in request.call_args_list), 1)
        self.assertNotIn('PRIVATE_', json.dumps(result))


class AuthorizationMonitorTests(unittest.TestCase):
    def metadata(self, expiry, remaining=100):
        return {'credential_timing': 'not_expired' if remaining > 10 else 'expired',
                'credential_expires_in_seconds': remaining,
                'credential_expires_at': datetime.fromtimestamp(expiry, timezone.utc).isoformat(timespec='seconds')}

    def test_samples_distinguish_countdown_advance_regression_expiry_and_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'authorization.log'
            log = EventLog(path)
            loader = Mock(return_value=(None, {}, []))
            monitor = AuthorizationMonitor(log, loader)
            values = [self.metadata(2000), self.metadata(2000, 40), self.metadata(5000),
                      self.metadata(4000), self.metadata(4000, -5)]
            with patch('authorization.credential_timing', side_effect=values):
                for _ in values:
                    monitor.sample()
            self.assertEqual(loader.call_count, 5)
            self.assertNotIn('headers', vars(monitor))
            # A new worker reports a baseline; it must not claim to know downtime behavior.
            with patch('authorization.credential_timing', return_value=self.metadata(7000)):
                AuthorizationMonitor(log, loader).sample()
            monitor.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['event'] for row in records], ['authorization_baseline', 'authorization_sample',
                'authorization_expiry_advanced', 'authorization_expiry_regressed', 'authorization_timing_changed',
                'authorization_baseline'])
            self.assertEqual(records[2]['credential_expiry_change_seconds'], 3000)
            self.assertEqual(records[3]['credential_expiry_change_seconds'], -1000)
            self.assertEqual(records[2]['previous_credential_expires_at'], values[1]['credential_expires_at'])
            self.assertIn('previous_credential_observed_at', records[2])

    def test_read_failure_retains_last_good_observation_and_logs_no_private_data(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'authorization.log'
            log = EventLog(path)
            loader = Mock(side_effect=[(None, {}, []), OSError('PRIVATE_MESSAGE'), (None, {}, [])])
            monitor = AuthorizationMonitor(log, loader)
            with patch('authorization.credential_timing', side_effect=[self.metadata(2000), self.metadata(5000)]):
                for _ in range(3):
                    monitor.sample()
            monitor.close()
            raw = path.read_text()
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertNotIn('PRIVATE_', raw)
            self.assertEqual(records[1]['event'], 'authorization_read_failed')
            self.assertEqual(records[1]['error_type'], 'OSError')
            self.assertEqual(records[2]['event'], 'authorization_expiry_advanced')
            self.assertTrue(records[2]['credential_read_recovered'])

    def test_unknown_timing_does_not_invent_a_refresh(self):
        log = Mock()
        monitor = AuthorizationMonitor(log, Mock(return_value=(None, {}, [])))
        monitor.sample()
        with patch('authorization.credential_timing', return_value=self.metadata(2000)):
            monitor.sample()
        self.assertEqual([call.args[0] for call in log.event.call_args_list],
                         ['authorization_baseline', 'authorization_timing_changed'])

    def test_sampler_runs_without_requests_and_stops_promptly(self):
        sampled = threading.Event()
        log = Mock()
        log.event.side_effect = lambda *args, **kwargs: sampled.set()
        monitor = AuthorizationMonitor(log, Mock(return_value=(None, {}, [])))
        self.assertEqual(monitor.interval, 300)
        monitor.start()
        try:
            self.assertTrue(sampled.wait(2))
            self.assertTrue(monitor.snapshot()['authorization_monitor_running'])
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
        monitor.loader.assert_called_once()


if __name__ == '__main__':
    unittest.main()
