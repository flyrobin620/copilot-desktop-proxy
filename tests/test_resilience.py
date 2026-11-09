"""Real loopback load tests: concurrent forwarding, liveness, streaming and safe diagnostics."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
from pathlib import Path
from tests import CONFIG, SRC
import socket
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import zipfile

from adapter import iter_sse
from collect_diagnostics import collect
from diagnostics import DEFAULT_OPTIONS, EventLog, load_options
from server import Handler, ProxyHTTPServer
from supervisor import probe, hidden_process_options
from test_native_api import CATALOG, FakeResponse, FakeUpstream
from usage_store import UsageStore


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='proxy load ')
        self.root = Path(self.temp.name)
        self.log = EventLog(self.root / 'proxy.log')
        # Exercise upgrades that preserve a pre-1.1.7 options file.
        (self.root / 'proxy-options.json').write_text('{"max_concurrency":2,"max_connections":8}')
        self.server = ProxyHTTPServer(('127.0.0.1', 0), Handler, load_options(self.root), self.log)
        self.server.default_effort = 'xhigh'
        self.server.usage_store = UsageStore(self.root / 'usage.sqlite3')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        self.log.close()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('Timed out: ' + str(self.server.snapshot()))

    def post(self, body, path='/v1/messages', headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        try:
            client.request('POST', path, body, {'Content-Type': 'application/json', **(headers or {})})
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_parallel_large_and_small_requests_all_reach_upstream_without_waiting(self):
        gate, connected = threading.Event(), []
        class WaitingUpstream(FakeUpstream):
            def getresponse(inner):
                connected.append(True)
                if not gate.wait(10): raise TimeoutError()
                return inner.response
        body = json.dumps({'model': 'claude-opus-5', 'messages': [{'role': 'user', 'content': 'x'*(5*1024*1024)}],
                           'max_tokens': 16}).encode()
        def factory(*args, **kwargs):
            return WaitingUpstream(FakeResponse(b'{"type":"message","content":[]}'))
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', side_effect=factory), ThreadPoolExecutor(12) as pool:
            small = b'{"model":"claude-opus-5","messages":[],"max_tokens":16}'
            pending = [pool.submit(self.post, body if i < 2 else small) for i in range(12)]
            try:
                # Every request must reach upstream while earlier responses are still pending.
                self.wait_for(lambda: len(connected) == 12)
                self.assertTrue(probe(self.port, os.getpid(), 1))
                self.assertEqual(self.server.snapshot()['active_requests'], 12)
                self.assertFalse(self.server.snapshot()['local_throttling'])
            finally:
                gate.set()
            self.assertEqual([future.result()[0] for future in pending], [200]*12)
        self.wait_for(lambda: self.server.snapshot()['active_requests'] == 0)

    def test_concurrent_sessions_keep_request_headers_streams_and_usage_isolated(self):
        count = 4
        gate = threading.Barrier(count)
        sessions = [str(uuid.uuid4()) for _ in range(count)]
        captures, lock = {}, threading.Lock()

        def stream_for(index):
            events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': index + 1}}},
                      {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'synthetic-' + str(index)}},
                      {'type': 'message_delta', 'usage': {'output_tokens': index + 11}},
                      {'type': 'message_stop'}]
            return b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)

        class FragmentedResponse(FakeResponse):
            def read1(inner, size):
                return super().read1(min(size, 13))

        class IsolatedUpstream(FakeUpstream):
            def __init__(inner, *_args, **_kwargs):
                super().__init__(None)

            def request(inner, method, path, body, headers):
                request = json.loads(body)
                index = int(request['messages'][0]['content'])
                inner.response = FragmentedResponse(stream_for(index), 'text/event-stream')
                with lock:
                    captures[index] = (body, dict(headers))

            def getresponse(inner):
                gate.wait(timeout=10)
                return inner.response

        bodies = [json.dumps({'model': 'claude-opus-5', 'stream': True,
                              'messages': [{'role': 'user', 'content': str(index)}],
                              'metadata': {'user_id': json.dumps({'account_uuid': 'synthetic-account', 'session_id': session})}}).encode()
                  for index, session in enumerate(sessions)]
        with patch('server.load_config', side_effect=lambda: ('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', side_effect=IsolatedUpstream), ThreadPoolExecutor(count) as pool:
            pending = [pool.submit(self.post, body) for body in bodies]
            for index, future in enumerate(pending):
                self.assertEqual(future.result(), (200, stream_for(index)))
        self.wait_for(lambda: self.server.snapshot()['active_requests'] == 0)
        self.wait_for(lambda: self.server.usage_store.report()['totals']['requests'] == count)
        for index, session in enumerate(sessions):
            self.assertEqual(captures[index][0], bodies[index])
            self.assertEqual(captures[index][1]['X-ConversationId'], session)
        self.assertEqual(len({headers['X-ClientCallId'] for _, headers in captures.values()}), count)
        events = self.server.usage_store.report()['events']
        self.assertEqual(sorted((event['input_tokens'], event['output_tokens']) for event in events),
                         [(index + 1, index + 11) for index in range(count)])
        self.assertTrue(all(event['outcome'] == 'success' for event in events))

    def test_more_than_32_open_connections_can_all_receive_liveness_responses(self):
        clients = []
        try:
            for _ in range(40):
                client = socket.create_connection(('127.0.0.1', self.port), timeout=2)
                client.sendall(b'GET /livez HTTP/1.1\r\n')  # Deliberately incomplete headers.
                clients.append(client)
            self.wait_for(lambda: self.server.snapshot()['active_connections'] == 40)
            self.assertTrue(probe(self.port, os.getpid(), 1))
            for client in clients:
                client.sendall(f'Host: 127.0.0.1:{self.port}\r\nConnection: close\r\n\r\n'.encode())
                response = http.client.HTTPResponse(client)
                try:
                    response.begin()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())['status'], 'alive')
                finally:
                    response.close()
        finally:
            for client in clients: client.close()
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        self.assertTrue(probe(self.port, os.getpid(), 1))

    def test_native_stop_finishes_without_waiting_for_upstream_eof(self):
        class KeepOpen(FakeResponse):
            def read1(inner, size):
                if inner.tell() == len(inner.getvalue()):
                    raise AssertionError('Must not wait for EOF after message_stop')
                return super().read1(size)
        wire = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=FakeUpstream(KeepOpen(wire, 'text/event-stream'))):
            self.assertEqual(self.post(b'{"model":"claude-opus-5","stream":true,"messages":[]}'), (200, wire))

    def test_native_truncated_stream_emits_protocol_error_and_clears_active_count(self):
        wire = b'data: {"type":"message_start","message":{}}\n\n'
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=FakeUpstream(FakeResponse(wire, 'text/event-stream'))):
            status, raw = self.post(b'{"model":"claude-opus-5","stream":true,"messages":[]}')
        self.assertEqual(status, 200)
        self.assertIn(b'event: error\ndata: {"type": "error"', raw)
        self.wait_for(lambda: self.server.snapshot()['active_requests'] == 0)
        self.wait_for(lambda: self.server.usage_store.report()['totals']['requests'] == 1)
        self.assertEqual(self.server.usage_store.report()['events'][0]['outcome'], 'error')

    def test_native_forwards_stop_blank_line_even_when_it_arrives_in_another_read(self):
        class SplitStop(FakeResponse):
            def read1(inner, size):
                return super().read1(min(size, len(inner.getvalue()) - 1) if inner.tell() == 0 else size)
        wire = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=FakeUpstream(SplitStop(wire, 'text/event-stream'))):
            self.assertEqual(self.post(b'{"model":"claude-opus-5","stream":true,"messages":[]}'), (200, wire))

    def test_native_multiline_stop_finishes_before_next_transport_read(self):
        class StopThenDisconnect(FakeResponse):
            def read1(inner, size):
                if inner.tell() == len(inner.getvalue()):
                    raise http.client.IncompleteRead(b'')
                return super().read1(size)
        wire = b'event: message_stop\ndata: {"type":\ndata: "message_stop"}\n\n'
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=FakeUpstream(StopThenDisconnect(wire, 'text/event-stream'))):
            self.assertEqual(self.post(b'{"model":"claude-opus-5","stream":true,"messages":[]}'), (200, wire))

    def test_remote_429_is_preserved_for_both_api_protocols(self):
        body = b'{"model":"claude-opus-5","messages":[{"role":"user","content":"test"}],"max_tokens":16}'
        for path in ('/v1/messages', '/v1/chat/completions'):
            with self.subTest(path=path):
                upstream = FakeUpstream(FakeResponse(b'{"error":{"type":"rate_limit_error"}}', status=429))
                with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
                     patch('server.http.client.HTTPSConnection', return_value=upstream):
                    status, raw = self.post(body, path)
                self.assertEqual(status, 429)
                self.assertEqual(upstream.captured[0], 'POST')
                self.assertNotIn(b'concurrency_limit', raw)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        records = [json.loads(line) for line in (self.root/'proxy.log').read_text().splitlines()]
        finished = [record for record in records if record['event'] == 'request_finished']
        self.assertEqual(len(finished), 2)
        self.assertTrue(all(record['upstream_status'] == 429 and record['reason'] == 'upstream_http_error'
                            for record in finished))

    def test_upstream_timeout_is_logged_without_exception_or_request_contents(self):
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', side_effect=TimeoutError('PRIVATE_EXCEPTION_CONTENT')):
            status, raw = self.post(b'{"model":"claude-opus-5","messages":[{"role":"user","content":"PRIVATE_BODY_CONTENT"}]}')
        self.assertEqual(status, 504)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        log = (self.root / 'proxy.log').read_text()
        self.assertIn('upstream_timeout', log)
        self.assertIn('TimeoutError', log)
        self.assertNotIn('PRIVATE_', log)
        self.assertNotIn('PRIVATE_', raw.decode())

    def test_403_logs_safe_reason_and_credential_timing_without_replay(self):
        upstream = FakeUpstream(FakeResponse(b'{"error":{"code":"Forbidden","message":"PRIVATE_RESPONSE"}}', status=403))
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)) as config, \
             patch('server.credential_timing', return_value={'credential_timing':'not_expired','credential_expires_in_seconds':100}), \
             patch('server.http.client.HTTPSConnection', return_value=upstream) as connect:
            status, raw = self.post(b'{"model":"claude-opus-5","messages":[]}')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)['error']['type'], 'permission_error')
        config.assert_called_once()
        connect.assert_called_once()
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        log = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE_', log)
        records = [json.loads(line) for line in log.splitlines()]
        rejection = next(record for record in records if record['event'] == 'authorization_rejected')
        self.assertEqual(rejection['upstream_error_category'], 'access_denied')
        self.assertEqual(rejection['credential_timing'], 'not_expired')
        self.assertEqual(rejection['upstream_status'], 403)

    def test_authorization_endpoint_is_local_metadata_without_an_upstream_probe(self):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
                 patch('server.account_selection', return_value={'database_selection':'explicit','matches_saved_account':False}), \
                 patch('server.http.client.HTTPSConnection', side_effect=AssertionError('No upstream access')):
                client.request('GET', '/authorization')
                response = client.getresponse()
                data = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertTrue(data['credential_readable'])
            self.assertEqual(data['credential_timing'], 'unknown')
            self.assertFalse(data['matches_saved_account'])
        finally:
            client.close()

    def test_success_log_links_request_to_credential_timing_used_for_forwarding(self):
        timing = {'credential_timing': 'not_expired', 'credential_expires_in_seconds': 900,
                  'credential_expires_at': '2026-10-12T10:00:00+00:00',
                  'credential_issued_at': '2026-10-12T09:00:00+00:00', 'credential_lifetime_seconds': 3600}
        upstream = FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.credential_timing', return_value=timing), \
             patch('server.http.client.HTTPSConnection', return_value=upstream):
            status, _ = self.post(b'{"model":"claude-opus-5","messages":[]}')
        self.assertEqual(status, 200)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        records = [json.loads(line) for line in (self.root/'proxy.log').read_text().splitlines()]
        finished = next(row for row in records if row['event'] == 'request_finished')
        response = next(row for row in records if row['event'] == 'upstream_response')
        self.assertEqual(finished['request_id'], response['request_id'])
        self.assertEqual(finished['upstream_status'], 200)
        self.assertEqual(finished['credential_expires_at'], timing['credential_expires_at'])
        self.assertEqual(finished['credential_lifetime_seconds'], 3600)
        self.assertIn('credential_observed_at', finished)

    def test_request_features_for_success_and_400_preserve_wire_body_headers_and_status(self):
        body = b'{ "model":"claude-opus-5", "messages":[{"role":"system","content":"PRIVATE_SYSTEM","output_config":{"effort":"high"}},{"role":"user","content":"PRIVATE_PROMPT"}], "max_tokens":64, "thinking":{"type":"adaptive"}, "output_config":{"effort":"high"}, "tools":[{"name":"PRIVATE_TOOL","defer_loading":true}], "metadata":{"user_id":"PRIVATE_SESSION"} }'
        beta = 'context-1m-2025-08-07,advanced-tool-use-2025-11-20,mid-conversation-system-2026-04-07,PRIVATE_HEADER'
        for status, host in ((200, 'loki.delve.office.com'), (400, 'df.loki.delve.office.com')):
            upstream = FakeUpstream(FakeResponse(
                b'{"type":"message","content":[]}' if status == 200 else
                b'{"error":{"type":"invalid_request_error","code":"PRIVATE_CODE","message":"Upstream rejected the request."}}', status=status))
            with self.subTest(status=status), \
                 patch('server.load_config', return_value=('https://' + host + '/api/v1/llmapi', {}, CATALOG)), \
                 patch('server.http.client.HTTPSConnection', return_value=upstream) as connect:
                received, result = self.post(body, '/v1/messages?beta=true', {'anthropic-beta': beta})
            self.assertEqual(received, status)
            connect.assert_called_once()
            self.assertEqual(upstream.captured[1], '/api/v1/llmapi/v1/messages?beta=true')
            self.assertEqual(upstream.captured[2], body)
            self.assertEqual(upstream.captured[3]['anthropic-beta'], beta)
            if status == 400:
                self.assertEqual(json.loads(result)['error']['message'], 'Upstream rejected the request.')
                self.assertEqual(json.loads(result)['error']['type'], 'invalid_request_error')
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        text = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE', text)
        records = [json.loads(line) for line in text.splitlines()]
        responses = [record for record in records if record['event'] == 'upstream_response']
        self.assertEqual(len(responses), 2)
        for record in responses:
            self.assertEqual(record['request_metadata_version'], 2)
            self.assertEqual(record['endpoint_kind'], 'messages')
            self.assertTrue(record['request_metadata_complete'])
            self.assertEqual(record['upstream_environment'], 'df' if record['upstream_status'] == 400 else 'prod')
            self.assertEqual(record['thinking_mode'], 'adaptive')
            self.assertEqual(record['effort_mode'], 'high')
            self.assertEqual(record['tool_count'], 1)
            self.assertEqual(record['deferred_tool_count'], 1)
            self.assertEqual(record['max_tokens'], 64)
            self.assertEqual(record['beta_unknown_count'], 1)
            self.assertTrue(record['beta_context_1m'])
            self.assertTrue(record['beta_mid_conversation_system'])
            self.assertEqual(record['message_role_system_count'], 1)
            self.assertEqual(record['message_role_user_count'], 1)
            self.assertEqual(record['message_output_config_count'], 1)
            self.assertEqual(record['message_effort_count'], 1)
        errors = [record for record in records if record['event'] == 'upstream_http_error']
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0]['upstream_generic_rejection'])
        self.assertEqual(errors[0]['upstream_error_code'], 'other')
        self.assertFalse(any(record['event'] == 'authorization_rejected' for record in records))

    def test_count_tokens_error_is_distinguishable_without_logging_url_or_error_text(self):
        upstream = FakeUpstream(FakeResponse(b'<html>PRIVATE_RESPONSE</html>', status=404))
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=upstream):
            status, _ = self.post(b'{"model":"claude-opus-5","messages":[]}', '/v1/messages/count_tokens?PRIVATE_QUERY')
        self.assertEqual(status, 404)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        text = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE', text)
        records = [json.loads(line) for line in text.splitlines()]
        response = next(record for record in records if record['event'] == 'upstream_response')
        self.assertEqual(response['endpoint_kind'], 'count_tokens')
        self.assertEqual(response['upstream_environment'], 'other')
        error = next(record for record in records if record['event'] == 'upstream_http_error')
        self.assertFalse(error['upstream_error_json'])

    def test_loki_keep_all_compat_only_changes_df_opus55_and_preserves_other_wire_values(self):
        class Gateway(FakeUpstream):
            def request(inner, method, path, body, headers):
                super().request(method, path, body, headers)
                # Reject retained directives even outside the workaround's scope:
                # prove that a 400 does not trigger broader rewriting or a retry.
                rejected = 'context_management' in json.loads(body)
                inner.response = FakeResponse(b'{"error":{"message":"Upstream rejected the request."}}' if rejected
                    else b'{"type":"message","content":[]}', status=400 if rejected else 200)
        directive = b'"context_management":{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]},'
        rest = b'"messages":[{"role":"user","content":"\\u4f60\\u597d"}],"thinking":{"type":"adaptive"},"output_config":{"effort":"xhigh"},"tools":[{"name":"synthetic","defer_loading":true,"input_schema":{"type":"object"}}],"max_tokens":256'
        catalog = [{'model_id': model} for model in ('claude-opus-5.5', 'claude-opus-5',
                   'claude-sonnet-5.5', 'claude-haiku-5.5', 'claude-opus-6')]
        beta = 'context-1m-2025-08-07,context-management-2025-06-27,advanced-tool-use-2025-11-20'
        for host in ('loki.delve.office.com', 'df.loki.delve.office.com'):
            for entry in catalog:
                model = entry['model_id']
                model_field = b'"model":' + json.dumps(model).encode() + b','
                body = b'{' + directive + model_field + rest + b'}'
                affected = host == 'df.loki.delve.office.com' and model == 'claude-opus-5.5'
                with self.subTest(host=host, model=model):
                    upstream = Gateway(None)
                    with patch('server.load_config', return_value=('https://' + host + '/api/v1/llmapi', {}, catalog)), \
                         patch('server.http.client.HTTPSConnection', return_value=upstream) as connect, \
                         patch.object(upstream, 'request', wraps=upstream.request) as send:
                        status, _ = self.post(body, headers={'anthropic-beta': beta})
                    self.assertEqual(status, 200 if affected else 400)
                    connect.assert_called_once_with(host, timeout=30)
                    send.assert_called_once()
                    self.assertEqual(upstream.captured[2], b'{' + model_field + rest + b'}' if affected else body)
                    self.assertEqual(upstream.captured[3]['anthropic-beta'], beta)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        rows = [json.loads(line) for line in (self.root/'proxy.log').read_text().splitlines()]
        responses = [row for row in rows if row['event'] == 'upstream_response']
        self.assertEqual(len(responses), 10)
        for row in responses:
            affected = row['upstream_environment'] == 'df' and row['model'] == 'claude-opus-5.5'
            self.assertEqual(row['context_management_compat_applied'], affected)
            self.assertEqual(row['has_context_management'], not affected)
        self.assertTrue(all(row['beta_context_management'] for row in responses))

    def test_loki_compat_uses_actual_model_after_auto_and_1m_alias_resolution(self):
        directive = b'"context_management":{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]},'
        rest = b'"messages":[],"max_tokens":64'
        for resolved in ('claude-opus-5.5', 'claude-opus-5-5', 'claude-opus-6'):
            catalog = CATALOG + [{'model_id': resolved}]
            for alias in ('copilot-auto[1m]', 'copilot-opus', resolved.replace('.', '-') + '[1m]'):
                body = b'{' + directive + b'"model":' + json.dumps(alias).encode() + b',' + rest + b'}'
                affected = resolved != 'claude-opus-6'
                upstream = FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
                with self.subTest(resolved=resolved, alias=alias), \
                     patch('server.load_config', return_value=('https://df.loki.delve.office.com/api/v1/llmapi', {}, catalog)), \
                     patch('server.http.client.HTTPSConnection', return_value=upstream):
                    status, _ = self.post(body)
                self.assertEqual(status, 200)
                expected = b'{' + (b'' if affected else directive) + b'"model":' + json.dumps(resolved).encode() + b',' + rest + b'}'
                self.assertEqual(upstream.captured[2], expected)

    def test_loki_compat_opt_out_and_real_context_edits_are_not_silently_rewritten(self):
        catalog = CATALOG + [{'model_id': 'claude-opus-5.5'}]
        for enabled, edit in ((False, {'type': 'clear_thinking_20251015', 'keep': 'all'}),
                              (True, {'type': 'clear_tool_uses_20250919'})):
            self.server.options['loki_context_management_compat'] = enabled
            body = json.dumps({'model': 'claude-opus-5.5', 'messages': [], 'context_management': {'edits': [edit]}}).encode()
            upstream = FakeUpstream(FakeResponse(b'{"error":{"type":"invalid_request_error"}}', status=400))
            with patch('server.load_config', return_value=('https://df.loki.delve.office.com', {}, catalog)), \
                 patch('server.http.client.HTTPSConnection', return_value=upstream) as connect:
                status, _ = self.post(body)
            self.assertEqual(status, 400)
            connect.assert_called_once()
            self.assertEqual(upstream.captured[2], body)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        rows = [json.loads(line) for line in (self.root/'proxy.log').read_text().splitlines()]
        self.assertTrue(all(not row['context_management_compat_applied'] for row in rows if row['event'] == 'upstream_response'))

    def test_loki_compat_option_defaults_on_for_old_files_and_requires_boolean(self):
        path = self.root/'proxy-options.json'
        path.write_text('{}')
        self.assertTrue(load_options(self.root)['loki_context_management_compat'])
        path.write_text('{"loki_context_management_compat":false}')
        self.assertFalse(load_options(self.root)['loki_context_management_compat'])
        for value in (0, 1, 'true', None, []):
            path.write_text(json.dumps({'loki_context_management_compat': value}))
            with self.assertRaises(ValueError):
                load_options(self.root)

    def test_thinking_display_compat_scope_aliases_and_wire_preservation(self):
        display_beta = 'thinking-display-updates-2026-08-18'
        beta = 'context-1m-2025-08-07,' + display_beta + ',advanced-tool-use-2025-11-20'
        preserved = b'"messages":[{"role":"user","content":"PRIVATE_PROMPT"},{"role":"system","content":"PRIVATE_SYSTEM"}],"tools":[{"name":"PRIVATE_TOOL","input_schema":{"type":"object"},"defer_loading":true}],"output_config":{"effort":"xhigh"},"max_tokens":2.56e2'
        stream = b'data: {"type":"message_start","message":{"usage":{"input_tokens":26}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":4}}\n\ndata: {"type":"message_stop"}\n\n'

        class Gateway(FakeUpstream):
            def request(inner, method, path, body, headers):
                super().request(method, path, body, headers)
                # This synthetic validator requires both parts to be removed;
                # it does not claim the real server rejects each part alone.
                rejected = ('display' in json.loads(body)['thinking'] or
                            display_beta in headers.get('anthropic-beta', ''))
                inner.response = FakeResponse(b'{"error":{"message":"Upstream rejected the request."}}'
                    if rejected else stream, 'application/json' if rejected else 'text/event-stream',
                    status=400 if rejected else 200)

        cases = (
            ('df.loki.delve.office.com', 'claude-opus-5.5', True, '/v1/messages', False, True),
            ('df.loki.delve.office.com', 'claude-opus-5-5[1m]', True, '/v1/messages?beta=true', False, True),
            ('df.loki.delve.office.com', 'copilot-auto[1m]', True, '/v1/messages', False, True),
            ('df.loki.delve.office.com', 'copilot-auto[1m]', True, '/v1/messages', True, False),
            ('loki.delve.office.com', 'claude-opus-5.5', True, '/v1/messages', False, False),
            ('df.loki.delve.office.com', 'claude-opus-5', True, '/v1/messages', False, False),
            ('df.loki.delve.office.com', 'claude-sonnet-5.5', True, '/v1/messages', False, False),
            ('df.loki.delve.office.com', 'claude-opus-5.5', False, '/v1/messages', False, False),
            ('df.loki.delve.office.com', 'claude-opus-5.5', True, '/v1/messages/count_tokens', False, False),
        )
        for host, model, enabled, path, future, affected in cases:
            catalog = [{'model_id': m} for m in ('claude-opus-5.5', 'claude-opus-5', 'claude-sonnet-5.5')]
            if future:
                catalog.append({'model_id': 'claude-opus-6'})
            body = b'{"model":' + json.dumps(model).encode() + b',"stream":true,"thinking":{ "display":"updates", "type":"adaptive" },' + preserved + b'}'
            self.server.options['loki_thinking_display_compat'] = enabled
            upstream = Gateway(None)
            with self.subTest(host=host, model=model, enabled=enabled, path=path, future=future), \
                 patch('server.load_config', return_value=('https://' + host + '/api/v1/llmapi', {}, catalog)), \
                 patch('server.http.client.HTTPSConnection', return_value=upstream) as connect, \
                 patch.object(upstream, 'request', wraps=upstream.request) as send:
                status, response = self.post(body, path, {'anthropic-beta': beta})
            connect.assert_called_once_with(host, timeout=30)
            send.assert_called_once()
            self.assertEqual(status, 200 if affected else 400)
            self.assertIn(preserved, upstream.captured[2])
            self.assertEqual(upstream.captured[3]['anthropic-beta'], beta.replace(display_beta + ',', '') if affected else beta)
            if affected:
                self.assertEqual(response, stream)
                self.assertIn(b'"thinking":{ "type":"adaptive" }', upstream.captured[2])
            else:
                self.assertIn(b'"thinking":{ "display":"updates", "type":"adaptive" }', upstream.captured[2])
            if future:
                self.assertEqual(json.loads(upstream.captured[2])['model'], 'claude-opus-6')
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        text = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE', text)
        rows = [json.loads(line) for line in text.splitlines()]
        responses = [r for r in rows if r['event'] == 'upstream_response']
        self.assertEqual(len(responses), len(cases))
        for row, case in zip(responses, cases):
            self.assertEqual(row['thinking_display_compat_applied'], case[-1])
            self.assertEqual(row['thinking_mode'], 'adaptive')
            self.assertEqual(row['thinking_display_mode'], 'absent' if case[-1] else 'updates')
            self.assertEqual(row['beta_thinking_display_updates'], not case[-1])
            self.assertTrue(row['beta_context_1m'])
            self.assertTrue(row['beta_advanced_tool_use'])

    def test_tool_search_beta_is_renamed_only_for_prod_and_body_bytes_are_preserved(self):
        refused, accepted = 'tool-search-tool-2025-10-19', 'advanced-tool-use-2025-11-20'
        beta = 'claude-code-20250219,interleaved-thinking-2025-05-14,' + refused
        body = (b'{"model":"claude-opus-5.5","stream":true,"messages":[{"role":"user","content":"PRIVATE_PROMPT"},'
                b'{"role":"assistant","content":[{"type":"tool_use","id":"toolu_1","name":"ToolSearch","input":{"query":"PRIVATE_QUERY"}}]},'
                b'{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":[{"type":"tool_reference","tool_name":"PRIVATE_TOOL"}]}]}],'
                b'"tools":[{"name":"ToolSearch","input_schema":{"type":"object"}},'
                b'{"name":"PRIVATE_TOOL","description":"PRIVATE_DESC","input_schema":{"type":"object"},"defer_loading":true}],"max_tokens":2.56e2}')
        stream = b'data: {"type":"message_start","message":{"usage":{"input_tokens":26}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":4}}\n\ndata: {"type":"message_stop"}\n\n'

        class Gateway(FakeUpstream):
            def request(inner, method, path, body, headers):
                super().request(method, path, body, headers)
                rejected = refused in headers.get('anthropic-beta', '')
                inner.response = FakeResponse(b'{"error":{"message":"The requested anthropic-beta feature is not supported."}}'
                    if rejected else stream, 'application/json' if rejected else 'text/event-stream',
                    status=400 if rejected else 200)

        cases = (
            ('loki.delve.office.com', True, '/v1/messages', True),
            ('loki.delve.office.com', True, '/v1/messages?beta=true', True),
            ('loki.delve.office.com', False, '/v1/messages', False),
            ('df.loki.delve.office.com', True, '/v1/messages', False),
        )
        for host, enabled, path, affected in cases:
            self.server.options['loki_tool_search_beta_compat'] = enabled
            upstream = Gateway(None)
            with self.subTest(host=host, enabled=enabled, path=path),                  patch('server.load_config', return_value=('https://' + host + '/api/v1/llmapi', {}, [{'model_id': 'claude-opus-5.5'}])),                  patch('server.http.client.HTTPSConnection', return_value=upstream) as connect:
                status, response = self.post(body, path, {'anthropic-beta': beta})
            connect.assert_called_once_with(host, timeout=30)
            self.assertEqual(upstream.captured[2], body)
            self.assertEqual(upstream.captured[3]['anthropic-beta'], beta.replace(refused, accepted) if affected else beta)
            self.assertEqual(status, 200 if affected else 400)
            if affected:
                self.assertEqual(response, stream)
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        text = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE', text)
        responses = [r for r in map(json.loads, text.splitlines()) if r['event'] == 'upstream_response']
        self.assertEqual([r['tool_search_beta_compat_applied'] for r in responses], [c[-1] for c in cases])
        self.assertTrue(all(r['beta_tool_search'] != r['tool_search_beta_compat_applied'] for r in responses))
        self.assertEqual([r['beta_advanced_tool_use'] for r in responses], [c[-1] for c in cases])
        self.assertTrue(all(r['beta_unknown_count'] == 0 and r['deferred_tool_count'] == 1 and r['tool_reference_count'] == 1
                            for r in responses))

    def test_tool_search_beta_option_defaults_on_and_requires_boolean(self):
        path = self.root/'proxy-options.json'
        path.write_text('{}')
        self.assertTrue(load_options(self.root)['loki_tool_search_beta_compat'])
        path.write_text('{"loki_tool_search_beta_compat":false}')
        self.assertFalse(load_options(self.root)['loki_tool_search_beta_compat'])
        self.assertTrue(load_options(self.root)['loki_cache_ttl_compat'])
        for value in (0, 1, 'true', None, []):
            path.write_text(json.dumps({'loki_tool_search_beta_compat': value}))
            with self.assertRaises(ValueError):
                load_options(self.root)

    def test_thinking_display_header_is_omitted_when_no_other_beta_remains(self):
        body = b'{"model":"claude-opus-5.5","stream":true,"thinking":{"type":"adaptive","display":"updates"},"messages":[],"context_management":{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}}'
        upstream = FakeUpstream(FakeResponse(b'{"error":{"message":"Still rejected."}}', status=400))
        with patch('server.load_config', return_value=('https://df.loki.delve.office.com/api/v1/llmapi', {}, [{'model_id': 'claude-opus-5.5'}])), \
             patch('server.http.client.HTTPSConnection', return_value=upstream) as connect, \
             patch.object(upstream, 'request', wraps=upstream.request) as send:
            status, response = self.post(body, headers={'anthropic-beta': 'thinking-display-updates-2026-08-18'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(response)['error']['message'], 'Still rejected.')
        connect.assert_called_once()
        send.assert_called_once()
        self.assertNotIn('anthropic-beta', upstream.captured[3])
        parsed = json.loads(upstream.captured[2])
        self.assertNotIn('context_management', parsed)
        self.assertEqual(parsed['thinking'], {'type': 'adaptive'})

    def test_thinking_display_option_is_independent_defaults_on_and_requires_boolean(self):
        path = self.root/'proxy-options.json'
        path.write_text('{"loki_context_management_compat":false}')
        self.assertTrue(load_options(self.root)['loki_thinking_display_compat'])
        self.assertFalse(load_options(self.root)['loki_context_management_compat'])
        path.write_text('{"loki_thinking_display_compat":false}')
        self.assertFalse(load_options(self.root)['loki_thinking_display_compat'])
        self.assertTrue(load_options(self.root)['loki_context_management_compat'])
        for value in (0, 1, 'true', None, []):
            path.write_text(json.dumps({'loki_thinking_display_compat': value}))
            with self.assertRaises(ValueError):
                load_options(self.root)

    def test_openai_diagnostics_describe_adapted_payload_without_adding_native_beta(self):
        upstream = FakeUpstream(FakeResponse(b'{"error":{"type":"invalid_request_error"}}', status=400))
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=upstream):
            status, _ = self.post(b'{"model":"claude-opus-5","messages":[{"role":"user","content":"PRIVATE_PROMPT"}],"max_tokens":64}',
                '/v1/chat/completions', {'anthropic-beta': 'PRIVATE_HEADER'})
        self.assertEqual(status, 400)
        self.assertNotIn('anthropic-beta', upstream.captured[3])
        payload = json.loads(upstream.captured[2])
        self.wait_for(lambda: self.server.snapshot()['active_connections'] == 0)
        text = (self.root/'proxy.log').read_text()
        self.assertNotIn('PRIVATE', text)
        response = next(json.loads(line) for line in text.splitlines() if json.loads(line)['event'] == 'upstream_response')
        self.assertEqual(response['endpoint_kind'], 'chat_completions')
        self.assertEqual(response['beta_count'], 0)
        self.assertEqual(response['thinking_mode'], payload['thinking']['type'])
        self.assertEqual(response['effort_mode'], payload['output_config']['effort'])

    def test_multiline_sse_event_has_total_size_bound(self):
        raw = (b'data: ' + b' '*600000 + b'\n')*2 + b'\n'
        with self.assertRaisesRegex(RuntimeError, 'size limit'):
            list(iter_sse(io.BytesIO(raw)))

    def test_rotating_logs_and_offline_export_exclude_private_files(self):
        log = EventLog(self.root / 'supervisor.log', max_bytes=250, backups=2)
        try:
            for _ in range(20):
                log.event('recover', reason='worker_exited', exit_code=2, authorization='PRIVATE_FIELD')
        finally:
            log.close()
        self.assertTrue((self.root / 'supervisor.log.2').exists())
        (self.root / 'server.stderr.log').write_text('PRIVATE_STDERR')
        (self.root / 'local-options.json').write_text('PRIVATE_PATH')
        (self.root / 'proxy.log').write_text('{"event":"request_error","headers":"PRIVATE_HEADER"}\nPRIVATE_RAW_LINE\n')
        (self.root / 'authorization.log').write_text(json.dumps({'event': 'authorization_expiry_advanced',
            'credential_expires_at': '2026-10-12T10:00:00+00:00', 'credential_expiry_change_seconds': 3600,
            'authorization': 'PRIVATE_TOKEN', 'claims': {'sub': 'PRIVATE_ACCOUNT'}}) + '\n')
        with patch('collect_diagnostics.status', return_value={'running': False, 'port': -1}):
            archive = collect(self.root)
        with zipfile.ZipFile(archive) as output:
            self.assertIn('authorization.log', output.namelist())
            self.assertEqual(json.loads(output.read('authorization.log'))['credential_expiry_change_seconds'], 3600)
            self.assertNotIn('server.stderr.log', output.namelist())
            self.assertNotIn('usage.sqlite3', output.namelist())
            for name in output.namelist():
                self.assertNotIn(b'PRIVATE_', output.read(name))

    def test_disk_logging_failure_does_not_break_request(self):
        with patch.object(self.log.handler, 'emit', side_effect=OSError('disk unavailable')):
            self.log.event('resource_sample')
        self.assertFalse(self.log.healthy)
        self.assertTrue(probe(self.port, os.getpid(), 1))

    def test_runtime_options_are_validated(self):
        self.assertEqual(load_options(self.root), DEFAULT_OPTIONS)
        path = self.root / 'proxy-options.json'
        path.write_text('{"max_concurrency":2,"max_connections":8,"upstream_timeout":900}')
        self.assertEqual(load_options(self.root), {**DEFAULT_OPTIONS, 'upstream_timeout': 900})
        for raw in ('{"client_timeout":0}', '{"upstream_timeout":true}', '{"unknown":2}',
                    '{"upstream_timeout":1801}'):
            path.write_text(raw)
            with self.assertRaises(ValueError): load_options(self.root)

    def test_real_worker_keeps_listening_when_account_and_usage_storage_are_unavailable(self):
        worker_root = self.root / 'isolated worker'
        (worker_root / 'src').mkdir(parents=True)
        for name in ('server.py', 'app_config.py', 'adapter.py', 'model_selection.py', 'usage_store.py', 'diagnostics.py', 'parent_lifetime.py', 'authorization.py', 'native_compat.py', 'desktop_models.py', 'token_refresh.py', 'paths.py'):
            shutil.copyfile(SRC / name, worker_root / 'src' / name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        (worker_root / 'not-a-directory').write_bytes(b'fixture')
        worker = subprocess.Popen([sys.executable, '-B', str(worker_root/'src'/'server.py'), '--port', str(port),
                                   '--usage-db', str(worker_root/'not-a-directory'/'usage.sqlite3')],
                                  env={**os.environ, 'COPILOT_PROXY_DB': str(worker_root/'missing-account.db')},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_process_options())
        try:
            self.wait_for(lambda: probe(port, worker.pid, .2))
            for path in ('/health', '/usage'):
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                try:
                    client.request('GET', path)
                    response = client.getresponse()
                    self.assertEqual(response.status, 503)
                    response.read()
                finally:
                    client.close()
            self.assertTrue(probe(port, worker.pid, .2))
            self.assertIsNone(worker.poll())
        finally:
            worker.terminate()
            worker.wait(timeout=5)
        log = (worker_root / 'proxy.log').read_text()
        self.assertIn('usage_init_failed', log)
        self.assertIn('config_error', log)
        self.assertNotIn('missing-account.db', log)


if __name__ == '__main__':
    unittest.main()
