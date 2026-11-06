"""Wire-level tests for Claude Code's native Messages endpoint."""
import io
import json
import os
import threading
import unittest
import time
import uuid
from tempfile import TemporaryDirectory
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from server import Handler, conversation_id, MAX_BODY
from usage_store import UsageStore
from model_selection import default_client_model_id

CATALOG = [{'model_id': 'claude-opus-5', 'supported_reasoning_efforts': '["xhigh"]'}]


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type='application/json', status=200):
        super().__init__(body)
        self.status, self.content_type = status, content_type

    def getheader(self, key, default=None):
        return self.content_type if key.lower() == 'content-type' else default


class FakeUpstream:
    def __init__(self, response):
        self.response = response
        self.sock = self

    def settimeout(self, _timeout):
        pass

    def request(self, method, path, body, headers):
        self.captured = method, path, body, headers

    def getresponse(self):
        return self.response

    def close(self):
        pass


class NativeApiTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.default_effort, self.server.upstream_timeout = 'xhigh', 10
        self.temporary = TemporaryDirectory()
        self.server.usage_store = UsageStore(Path(self.temporary.name) / 'usage.sqlite3')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def post(self, path, body, upstream):
        config = ('https://loki.delve.office.com/api/v1/llmapi', {}, CATALOG)
        with patch('server.load_config', return_value=config), patch('server.http.client.HTTPSConnection', return_value=upstream):
            request = Request(self.url + path, data=body, headers={
                'Content-Type': 'application/json', 'anthropic-version': '2023-06-01',
                'anthropic-beta': 'interleaved-thinking-2025-05-14'})
            with urlopen(request, timeout=5) as response:
                return response.read()

    def test_native_body_and_beta_are_preserved(self):
        body = b'{ "model": "claude-opus-5", "messages": [{"role":"user","content":"hi"}], "system": [{"type":"text","text":"test","cache_control":{"type":"ephemeral"}}], "max_tokens": 16 }'
        result = b'{"type":"message","content":[{"type":"text","text":"ok"}]}'
        upstream = FakeUpstream(FakeResponse(result))
        self.assertEqual(self.post('/v1/messages?beta=true', body, upstream), result)
        _, path, wire, headers = upstream.captured
        self.assertEqual(path, '/api/v1/llmapi/v1/messages?beta=true')
        self.assertEqual(wire, body)
        self.assertEqual(headers['anthropic-beta'], 'interleaved-thinking-2025-05-14')

    def test_cache_ttl_compat_on_desktop_cli_and_count_routes(self):
        body = b'{"model":"copilot-auto[1m]","max_tokens":32,"system":[{"type":"text","text":"fixture","cache_control":{"type":"ephemeral","ttl":"1h"}}],"messages":[{"role":"user","content":"hi"}]}'
        catalog = [{'model_id': 'claude-opus-5.5'}]
        for path in ('/desktop/v1/messages', '/v1/messages', '/desktop/v1/messages/count_tokens'):
            for enabled in (True, False):
                with self.subTest(path=path, enabled=enabled), patch(__name__ + '.CATALOG', catalog):
                    self.server.options = {'loki_cache_ttl_compat': enabled}
                    upstream = FakeUpstream(FakeResponse(b'{"type":"message"}'))
                    self.post(path + '?beta=true', body, upstream)
                    expected = body.replace(b'copilot-auto[1m]', b'claude-opus-5.5')
                    if enabled:
                        expected = expected.replace(b'"ttl":"1h"', b'"ttl":"5m"')
                    self.assertEqual(upstream.captured[2], expected)
                    self.assertEqual(upstream.captured[1], '/api/v1/llmapi' + path.removeprefix('/desktop') + '?beta=true')
                    self.assertEqual(upstream.captured[3]['anthropic-beta'], 'interleaved-thinking-2025-05-14')

    def test_liveness_is_independent_of_account_and_upstream(self):
        with patch('server.load_config', side_effect=AssertionError('Liveness must not read account configuration')), \
             patch('server.http.client.HTTPSConnection', side_effect=AssertionError('Liveness must not call upstream')):
            with urlopen(self.url+'/livez') as response:
                body=json.load(response)
        self.assertEqual(body['service'],'copilot-desktop-proxy')
        self.assertEqual(body['pid'],os.getpid())
        self.assertEqual(body['status'],'alive')

    def test_native_tool_search_preserves_deferred_tools_references_and_beta(self):
        body = json.dumps({'model': 'copilot-auto[1m]', 'max_tokens': 16, 'tools': [
            {'type': 'tool_search_tool_regex_20251119', 'name': 'tool_search_tool_regex'},
            {'name': 'synthetic_lookup', 'defer_loading': True, 'input_schema': {'type': 'object'}}],
            'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'synthetic-call',
                'content': [{'type': 'tool_reference', 'tool_name': 'synthetic_lookup'}]}]}]}).encode()
        result = json.dumps({'type': 'message', 'content': [{'type': 'tool_search_tool_result',
            'tool_use_id': 'synthetic-search', 'content': {'type': 'tool_search_tool_search_result',
            'tool_references': [{'type': 'tool_reference', 'tool_name': 'synthetic_lookup'}]}}]}).encode()
        upstream = FakeUpstream(FakeResponse(result))
        beta = 'advanced-tool-use-2025-11-20,context-1m-2025-08-07'
        with patch('server.load_config', return_value=('https://example.invalid', {}, CATALOG)), \
             patch('server.http.client.HTTPSConnection', return_value=upstream):
            request = Request(self.url + '/v1/messages?beta=true', data=body,
                              headers={'Content-Type': 'application/json', 'anthropic-beta': beta})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.read(), result)
        self.assertEqual(upstream.captured[2], body.replace(b'"copilot-auto[1m]"', b'"claude-opus-5"'))
        self.assertEqual(upstream.captured[3]['anthropic-beta'], beta)

    def test_native_sse_keeps_events_and_signatures(self):
        wire = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"test-signature"}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'
        upstream = FakeUpstream(FakeResponse(wire, 'text/event-stream'))
        body = b'{"model":"claude-opus-5","stream":true,"messages":[],"max_tokens":16}'
        self.assertEqual(self.post('/v1/messages', body, upstream), wire)

    def test_auto_alias_changes_only_model_on_native_wire(self):
        body = b'{ "model" : "copilot-auto", "messages": [], "max_tokens":16, "system":[{"type":"text","text":"copilot-auto","cache_control":{"type":"ephemeral"}}] }'
        upstream = FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
        self.post('/v1/messages', body, upstream)
        self.assertEqual(upstream.captured[2], body.replace(b'"model" : "copilot-auto"', b'"model" : "claude-opus-5"'))

    def test_1m_hint_is_removed_without_losing_context_beta_or_body_bytes(self):
        body=b'{ "model" : "copilot-auto[1m]", "messages": [], "max_tokens":32, "metadata":{"model":"copilot-auto[1m]"} }'
        upstream=FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
        config=('https://example.invalid', {}, CATALOG)
        with patch('server.load_config',return_value=config), patch('server.http.client.HTTPSConnection',return_value=upstream):
            request=Request(self.url+'/v1/messages',data=body,headers={
                'Content-Type':'application/json','anthropic-beta':'context-1m-2025-08-07'})
            with urlopen(request) as response: response.read()
        self.assertEqual(upstream.captured[2],body.replace(b'"model" : "copilot-auto[1m]"',b'"model" : "claude-opus-5"'))
        self.assertEqual(upstream.captured[3]['anthropic-beta'],'context-1m-2025-08-07')

    def test_large_context_body_passes_and_oversize_is_rejected_before_reading(self):
        body=json.dumps({'model':'claude-opus-5','max_tokens':32,
                         'messages':[{'role':'user','content':'x'*(5*1024*1024)}]}).encode()
        upstream=FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
        self.post('/v1/messages',body,upstream)
        self.assertEqual(upstream.captured[2],body)
        with patch('server.load_config') as config:
            request=Request(self.url+'/v1/messages',data=b'{}',headers={
                'Content-Type':'application/json','Content-Length':str(MAX_BODY+1)})
            with self.assertRaises(HTTPError) as caught: urlopen(request)
            self.assertEqual(caught.exception.code,413)
            self.assertEqual(json.load(caught.exception)['error']['code'],'body_size_limit')
            caught.exception.close()
            config.assert_not_called()

    def test_unsupported_counting_is_not_fabricated(self):
        upstream = FakeUpstream(FakeResponse(b'', status=404))
        with self.assertRaises(HTTPError) as caught:
            self.post('/v1/messages/count_tokens', b'{"model":"claude-opus-5","messages":[]}', upstream)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.load(caught.exception)['type'], 'error')
        caught.exception.close()

    def test_stable_claude_session_id(self):
        session = '423020fc-9c3c-4a65-8728-8d0f4682c7a3'
        for user_id in (json.dumps({'session_id': session}), 'user_hash_account_none_session_' + session):
            self.assertEqual(conversation_id({'metadata': {'user_id': user_id}}), session)

    def test_distinct_sessions_with_same_account_never_share_conversation_id(self):
        sessions = [str(uuid.uuid4()) for _ in range(2)]
        for encode in (lambda value: json.dumps({'account_uuid': 'synthetic-account', 'session_id': value}),
                       lambda value: 'user_same_account_same_session_' + value):
            requests = [{'metadata': {'user_id': encode(value)}} for value in sessions]
            self.assertEqual([conversation_id(request) for request in requests], sessions)
            self.assertEqual(conversation_id(requests[0]), sessions[0])

    def test_absent_or_invalid_session_uses_new_id_instead_of_account_or_cached_session(self):
        for metadata in (None, {}, {'user_id': 'same-account'}, {'user_id': '{}'},
                         {'user_id': json.dumps({'session_id': 'not-a-uuid'})},
                         {'user_id': json.dumps({'session_id': str(uuid.uuid1())})}):
            with self.subTest(metadata_type=type(metadata).__name__):
                request = {'metadata': metadata}
                values = [conversation_id(request) for _ in range(5)]
                self.assertEqual(len(set(values)), 5)
                self.assertTrue(all(uuid.UUID(value).version == 4 for value in values))

    def test_native_interleaved_sessions_forward_only_client_supplied_history(self):
        sessions = [str(uuid.uuid4()) for _ in range(2)]
        first = [{'role': 'user', 'content': 'synthetic-session-a-first'}]
        second = [{'role': 'user', 'content': 'synthetic-session-b-first'}]
        continued = first + [{'role': 'assistant', 'content': 'synthetic-session-a-answer'},
                             {'role': 'user', 'content': 'synthetic-session-a-next'}]
        captures = []
        for session, messages in ((sessions[0], first), (sessions[1], second), (sessions[0], continued)):
            body = json.dumps({'model': 'copilot-auto', 'messages': messages, 'max_tokens': 16,
                               'metadata': {'user_id': json.dumps({'session_id': session})}}).encode()
            upstream = FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
            self.post('/v1/messages', body, upstream)
            self.assertEqual(upstream.captured[2], body.replace(b'"copilot-auto"', b'"claude-opus-5"'))
            captures.append(upstream.captured)
        self.assertEqual([row[3]['X-ConversationId'] for row in captures], [sessions[0], sessions[1], sessions[0]])
        self.assertEqual(len({row[3]['X-ClientCallId'] for row in captures}), 3)
        self.assertNotIn(b'synthetic-session-a', captures[1][2])
        self.assertNotIn(b'synthetic-session-b', captures[2][2])

    def test_openai_sessions_do_not_append_previous_messages_or_tools(self):
        for marker in ('synthetic-a', 'synthetic-b'):
            body = json.dumps({'model': 'claude-opus-5', 'max_tokens': 16,
                               'messages': [{'role': 'system', 'content': marker + '-system'},
                                            {'role': 'user', 'content': marker + '-user'}],
                               'tools': [{'type': 'function', 'function': {'name': marker, 'parameters': {'type': 'object'}}}]}).encode()
            upstream = FakeUpstream(FakeResponse(b'{"type":"message","content":[]}'))
            self.post('/v1/chat/completions', body, upstream)
            payload = json.loads(upstream.captured[2])
            self.assertEqual(payload['messages'], [{'role': 'user', 'content': [{'type': 'text', 'text': marker + '-user'}]}])
            self.assertEqual(payload['system'], [{'type': 'text', 'text': marker + '-system'}])
            self.assertEqual([tool['name'] for tool in payload['tools']], [marker])

    def test_native_usage_is_recorded_and_reported_without_config_access(self):
        result=b'{"type":"message","content":[],"usage":{"input_tokens":2,"output_tokens":3,"cache_read_input_tokens":5,"cache_creation_input_tokens":7}}'
        self.post('/v1/messages', b'{"model":"claude-opus-5","messages":[],"max_tokens":16}', FakeUpstream(FakeResponse(result)))
        for _ in range(20):
            if self.server.usage_store.report()['totals']['requests']: break
            time.sleep(.01)
        with patch('server.load_config', side_effect=AssertionError('Usage must not read account configuration')) as config, \
             patch('server.http.client.HTTPSConnection', side_effect=AssertionError('Usage must not fetch account totals')) as upstream:
            with urlopen(self.url+'/usage?period=month&offset=-480') as response:
                report=json.load(response)
        config.assert_not_called()
        upstream.assert_not_called()
        self.assertEqual(report['source'],'local-proxy')
        self.assertEqual(report['totals']['total_tokens'],17)
        self.assertEqual(report['events'][0]['outcome'],'success')
        self.assertNotIn('content',report['events'][0])

    def test_report_validates_parameters_and_rejects_cross_origin(self):
        for request in (self.url+'/usage?period=invalid',Request(self.url+'/usage',headers={'Origin':'https://example.com'})):
            with self.assertRaises(HTTPError) as caught: urlopen(request)
            self.assertIn(caught.exception.code,(400,403))
            caught.exception.close()

    def test_models_discovery_returns_entire_live_catalog_and_aliases(self):
        models = CATALOG + [{'model_id': 'claude-sonnet-5.5', 'supported_reasoning_efforts': '[]'},
                            {'model_id': 'claude-haiku-4.5', 'supported_reasoning_efforts': '[]'}]
        with patch('server.load_config', return_value=('https://example.invalid', {}, models)):
            with urlopen(self.url+'/v1/models?limit=1000') as response:
                first = json.load(response)
            models.append({'model_id': 'claude-opus-6', 'supported_reasoning_efforts': '[]'})
            with urlopen(self.url+'/v1/models?limit=1000') as response:
                updated = json.load(response)
        self.assertFalse(first['has_more'])
        self.assertTrue({default_client_model_id(m['model_id']) for m in models[:-1]}.issubset({m['id'] for m in first['data']}))
        self.assertIn('copilot-auto[1m]',{m['id'] for m in first['data']})
        self.assertIn('copilot-auto[1m]',{m['id'] for m in updated['data']})
        self.assertIn('claude-opus-6[1m]', {m['id'] for m in updated['data']})
        self.assertEqual(next(m for m in updated['data'] if m['id']=='copilot-auto')['resolved_model'], 'claude-opus-6')

    def test_models_discovery_denylist_changes_only_affected_family_defaults(self):
        models = [{'model_id': name} for name in ('claude-opus-5.5', 'claude-sonnet-5.5', 'claude-haiku-4.5')]
        with patch('server.load_config', return_value=('https://example.invalid', {}, models)), \
             patch('model_selection.CONTEXT_1M_DENYLIST', frozenset({'claude-opus-5-5'})):
            with urlopen(self.url + '/v1/models') as response:
                data = json.load(response)['data']
        ids = {row['id'] for row in data}
        self.assertNotIn('copilot-auto[1m]', ids)
        self.assertNotIn('copilot-opus[1m]', ids)
        self.assertNotIn('claude-opus-5-5[1m]', ids)
        self.assertTrue({'copilot-auto', 'copilot-opus', 'claude-opus-5-5',
                         'copilot-sonnet[1m]', 'copilot-haiku[1m]', 'claude-sonnet-5-5[1m]',
                         'claude-haiku-4-5[1m]'}.issubset(ids))


if __name__ == '__main__':
    unittest.main()
