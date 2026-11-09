"""Offline preservation and opt-out tests for Loki's context-management workaround."""
import copy
import json
import unittest

from native_compat import (adapt_context_management, KEEP_ALL_THINKING, adapt_thinking_display,
                           DISPLAY_UPDATES_BETA, UPDATES_THINKING, adapt_tool_search_beta,
                           TOOL_SEARCH_BETA, TOOL_SEARCH_REPLACEMENT_BETA, adapt_prompt_too_large,
                           PROMPT_TOO_LONG_MESSAGE)


class ContextManagementTests(unittest.TestCase):
    def adapt_affected(self, raw, payload, enabled=True):
        return adapt_context_management(raw, payload, upstream_host='df.loki.delve.office.com',
                                        resolved_model='claude-opus-5.5', enabled=enabled)

    def test_only_known_backend_and_exact_model_are_eligible(self):
        payload = {'context_management': KEEP_ALL_THINKING}
        raw = json.dumps(payload).encode()
        for host in ('df.loki.delve.office.com', 'loki.delve.office.com',
                     'df.loki.delve.office.com.example.invalid', 'example.invalid', None):
            for model in ('claude-opus-5.5', 'claude-opus-5-5', 'claude-opus-5',
                          'claude-sonnet-5.5', 'claude-haiku-5.5', 'claude-opus-6',
                          'claude-opus-5.50', 'claude-opus-5.5-latest', 'copilot-auto', None):
                with self.subTest(host=host, model=model):
                    result = adapt_context_management(raw, payload, upstream_host=host, resolved_model=model)
                    affected = host == 'df.loki.delve.office.com' and model in ('claude-opus-5.5', 'claude-opus-5-5')
                    if affected:
                        self.assertTrue(result[2])
                        self.assertEqual(json.loads(result[0]), {})
                    else:
                        self.assertEqual(result, (raw, payload, False))

    def test_removes_first_middle_last_and_only_field_without_reserializing_other_values(self):
        directive = b'"context_management" : { "edits": [ {"keep":"all", "type":"clear_thinking_20251015"} ] }'
        first = b'"model" : "claude-opus-5.5"'
        last = b'"messages": [{"role":"user", "content":"\\u4f60\\u597d \\n \\u00e9"}], "max_tokens" : 6.4e1'
        for parts in ((directive, first, last), (first, directive, last), (first, last, directive), (directive,)):
            with self.subTest(position=parts.index(directive)):
                raw = b'{  ' + b', \n  '.join(parts) + b'  }'
                payload = json.loads(raw)
                before = copy.deepcopy(payload)
                updated, forwarded, changed = self.adapt_affected(raw, payload)
                self.assertTrue(changed)
                self.assertEqual(payload, before)
                self.assertNotIn('context_management', forwarded)
                self.assertEqual(json.loads(updated), forwarded)
                for part in parts:
                    if part != directive:
                        self.assertIn(part, updated)

    def test_nested_directives_thinking_signatures_cache_and_tools_stay_byte_identical(self):
        values = b'"messages":[{"role":"assistant","content":[{"type":"thinking","thinking":"synthetic","signature":"synthetic-signature"}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"synthetic","content":"context_management"}]}],"tools":[{"name":"synthetic","input_schema":{"properties":{"context_management":{"type":"string"}}},"defer_loading":true,"cache_control":{"type":"ephemeral"}}]'
        raw = b'{"context_management":' + json.dumps(KEEP_ALL_THINKING).encode() + b',' + values + b'}'
        updated, payload, changed = self.adapt_affected(raw, json.loads(raw))
        self.assertTrue(changed)
        self.assertEqual(updated, b'{' + values + b'}')
        self.assertEqual(json.loads(updated), payload)

    def test_absent_real_clearing_empty_and_unknown_forms_are_not_changed(self):
        for value in (None, {}, {'edits': []}, {'edits': [{'type': 'clear_thinking_20251015', 'keep': 1}]},
                      {'edits': [{'type': 'clear_tool_uses_20250919'}]},
                      {'edits': KEEP_ALL_THINKING['edits'], 'extra': True},
                      {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all', 'extra': True}]},
                      {'edits': KEEP_ALL_THINKING['edits'] * 2}):
            with self.subTest(value=value):
                payload = {'messages': [], 'context_management': value}
                raw = json.dumps(payload).encode()
                self.assertEqual(self.adapt_affected(raw, payload), (raw, payload, False))
        raw = b'{"messages":[]}'
        self.assertEqual(self.adapt_affected(raw, json.loads(raw)), (raw, json.loads(raw), False))

    def test_opt_out_preserves_the_original_request(self):
        payload = {'context_management': KEEP_ALL_THINKING}
        raw = json.dumps(payload).encode()
        self.assertEqual(self.adapt_affected(raw, payload, enabled=False), (raw, payload, False))

    def test_ambiguous_fields_or_decoding_failures_leave_input_untouched(self):
        exact = json.dumps(KEEP_ALL_THINKING).encode()
        for raw in (
            b'{"model":"a","model":"b","context_management":' + exact + b'}',
            b'{"context_management":{},"context_management":' + exact + b'}',
            b'{"context_management":{"edits":[{"type":"clear_thinking_20251015","keep":1,"keep":"all"}]}}',
        ):
            payload = json.loads(raw)
            self.assertEqual(self.adapt_affected(raw, payload), (raw, payload, False))
        payload = {'context_management': KEEP_ALL_THINKING}
        for raw in (b'\xff', b'{', b'{"context_management" 1}', b'{"unrelated": 1}',
                    b'{"context_management": {}}'):
            self.assertEqual(self.adapt_affected(raw, payload), (raw, payload, False))

    def test_bom_escaped_key_and_non_ascii_values_remain_valid(self):
        raw = b'\xef\xbb\xbf{ "context_\\u006danagement":' + json.dumps(KEEP_ALL_THINKING).encode() + ', "system":"中文" }'.encode()
        updated, payload, changed = self.adapt_affected(raw, json.loads(raw))
        self.assertTrue(changed)
        self.assertTrue(updated.startswith(b'\xef\xbb\xbf'))
        self.assertIn('"system":"中文"'.encode(), updated)
        self.assertEqual(json.loads(updated), payload)


class ThinkingDisplayTests(unittest.TestCase):
    def adapt(self, raw, payload=None, beta=DISPLAY_UPDATES_BETA, **kwargs):
        return adapt_thinking_display(raw, json.loads(raw) if payload is None else payload, beta,
            upstream_host=kwargs.pop('host', 'df.loki.delve.office.com'),
            resolved_model=kwargs.pop('model', 'claude-opus-5.5'), **kwargs)

    def test_only_reproduced_backend_model_stream_and_thinking_shape_are_eligible(self):
        base = {'stream': True, 'thinking': UPDATES_THINKING}
        for host in ('df.loki.delve.office.com', 'loki.delve.office.com', 'example.invalid',
                     'df.loki.delve.office.com.example.invalid', None):
            for model in ('claude-opus-5.5', 'claude-opus-5-5', 'claude-opus-5',
                          'claude-sonnet-5.5', 'claude-opus-6', 'claude-opus-5.5-latest', 'copilot-auto', None):
                with self.subTest(host=host, model=model):
                    raw = json.dumps(base).encode()
                    out = self.adapt(raw, host=host, model=model)
                    eligible = host == 'df.loki.delve.office.com' and model in ('claude-opus-5.5', 'claude-opus-5-5')
                    self.assertEqual(out[3], eligible)
                    if not eligible:
                        self.assertEqual(out, (raw, base, DISPLAY_UPDATES_BETA, False))
        for thinking in (None, {}, {'type': 'adaptive'}, {'type': 'adaptive', 'display': 'summarized'},
                         {'type': 'enabled', 'display': 'updates', 'budget_tokens': 1000},
                         {**UPDATES_THINKING, 'block_binding': {}}, {'type': 'disabled', 'display': 'updates'}):
            payload = {**base, 'thinking': thinking}
            raw = json.dumps(payload).encode()
            self.assertEqual(self.adapt(raw), (raw, payload, DISPLAY_UPDATES_BETA, False))
        for stream in (None, False, 1, 'true'):
            payload = {**base, 'stream': stream}
            raw = json.dumps(payload).encode()
            self.assertEqual(self.adapt(raw), (raw, payload, DISPLAY_UPDATES_BETA, False))

    def test_header_and_body_are_changed_together_and_unknown_versions_are_preserved(self):
        payload = {'stream': True, 'thinking': UPDATES_THINKING}
        raw = json.dumps(payload).encode()
        for beta in ('', None, 'effort-2025-11-24', 'thinking-display-updates-2026-09-18',
                     DISPLAY_UPDATES_BETA + ',thinking-display-updates-2026-09-18'):
            self.assertEqual(self.adapt(raw, beta=beta), (raw, payload, beta, False))
        self.assertEqual(self.adapt(raw, enabled=False), (raw, payload, DISPLAY_UPDATES_BETA, False))
        for beta, expected in ((DISPLAY_UPDATES_BETA, ''),
                               ('x, ' + DISPLAY_UPDATES_BETA + ', y', 'x, y'),
                               (' ' + DISPLAY_UPDATES_BETA + ',x,' + DISPLAY_UPDATES_BETA, 'x')):
            out = self.adapt(raw, beta=beta)
            self.assertTrue(out[3])
            self.assertEqual(out[2], expected)
            self.assertEqual(out[1]['thinking'], {'type': 'adaptive'})
            self.assertEqual(payload['thinking'], UPDATES_THINKING)
        no_display = {'stream': True, 'thinking': {'type': 'adaptive'}}
        raw = json.dumps(no_display).encode()
        self.assertEqual(self.adapt(raw), (raw, no_display, DISPLAY_UPDATES_BETA, False))

    def test_preserves_other_bytes_signatures_caching_tools_and_system_roles(self):
        rest = b'"stream":true,"messages":[{"role":"system","content":"\\u4f60\\u597d"},{"role":"assistant","content":[{"type":"thinking","thinking":"synthetic","signature":"synthetic-signature"}]}],"tools":[{"name":"synthetic","input_schema":{"type":"object","properties":{"display":{"const":"updates"}}},"defer_loading":true,"cache_control":{"type":"ephemeral"}}],"max_tokens":2.56e2'
        for thought in (b'{ "display" : "updates", "type" : "adaptive" }',
                        b'{ "type" : "adaptive", "displ\\u0061y" : "updates" }'):
            for parts in ((b'"thinking":'+thought, rest), (rest, b'"thinking":'+thought)):
                raw = b'\xef\xbb\xbf{ ' + b',\n'.join(parts) + b' }'
                original = json.loads(raw)
                before = copy.deepcopy(original)
                updated, forwarded, beta, changed = self.adapt(raw, original)
                self.assertTrue(changed)
                self.assertEqual(original, before)
                self.assertEqual(json.loads(updated), forwarded)
                self.assertEqual(forwarded['thinking'], {'type': 'adaptive'})
                self.assertIn(rest, updated)
                self.assertIn(b'"type" : "adaptive"', updated)
                self.assertTrue(updated.startswith(b'\xef\xbb\xbf'))
                self.assertEqual(beta, '')

    def test_ambiguous_or_mismatched_raw_fields_preserve_body_and_header(self):
        thought = b'{"type":"adaptive","display":"updates"}'
        for raw in (
            b'{"stream":true,"thinking":{},"thinking":'+thought+b'}',
            b'{"stream":false,"stream":true,"thinking":'+thought+b'}',
            b'{"stream":true,"thinking":{"type":"enabled","type":"adaptive","display":"updates"}}',
            b'{"stream":true,"thinking":{"type":"adaptive","display":"omitted","display":"updates"}}',
        ):
            payload = json.loads(raw)
            self.assertEqual(self.adapt(raw), (raw, payload, DISPLAY_UPDATES_BETA, False))
        payload = {'stream': True, 'thinking': UPDATES_THINKING}
        for raw in (b'\xff', b'{', b'{"thinking" 1}', b'{"stream":true}',
                    b'{"thinking":{"type":"adaptive"}}'):
            self.assertEqual(self.adapt(raw, payload), (raw, payload, DISPLAY_UPDATES_BETA, False))

    def test_two_workarounds_compose_and_can_be_disabled_independently(self):
        payload = {'stream': True, 'thinking': UPDATES_THINKING, 'context_management': KEEP_ALL_THINKING,
                   'messages': [{'role': 'user', 'content': 'synthetic'}]}
        raw = json.dumps(payload).encode()
        beta = 'context-1m-2025-08-07,context-management-2025-06-27,' + DISPLAY_UPDATES_BETA
        for context_on in (False, True):
            for display_on in (False, True):
                first, parsed, context_changed = adapt_context_management(raw, payload,
                    upstream_host='df.loki.delve.office.com', resolved_model='claude-opus-5.5', enabled=context_on)
                second, forwarded, header, display_changed = self.adapt(first, parsed, beta, enabled=display_on)
                self.assertEqual((context_changed, display_changed), (context_on, display_on))
                self.assertEqual('context_management' in forwarded, not context_on)
                self.assertEqual('display' in forwarded['thinking'], not display_on)
                self.assertEqual(DISPLAY_UPDATES_BETA in header, not display_on)
                self.assertIn('context-1m-2025-08-07,context-management-2025-06-27', header)
                self.assertEqual(json.loads(second), forwarded)
                self.assertEqual(payload['thinking'], UPDATES_THINKING)
                self.assertIn('context_management', payload)


if __name__ == '__main__':
    unittest.main()


class ToolSearchBetaTests(unittest.TestCase):
    PROD = 'loki.delve.office.com'

    def test_only_exact_prod_host_and_exact_value_are_renamed(self):
        header = 'claude-code-20250219,' + TOOL_SEARCH_BETA + ',effort-2025-11-24'
        renamed = 'claude-code-20250219,' + TOOL_SEARCH_REPLACEMENT_BETA + ',effort-2025-11-24'
        self.assertEqual(adapt_tool_search_beta(header, upstream_host=self.PROD), (renamed, True))
        for host in ('df.loki.delve.office.com', 'example.invalid', 'loki.delve.office.com.example.invalid', None):
            with self.subTest(host=host):
                self.assertEqual(adapt_tool_search_beta(header, upstream_host=host), (header, False))
        self.assertEqual(adapt_tool_search_beta(header, upstream_host=self.PROD, enabled=False), (header, False))
        for other in ('', 'tool-search-tool-2026-01-01', 'tool-search-tool-2025-10-19x', 'xtool-search-tool-2025-10-19',
                      'advanced-tool-use-2025-11-20', 'claude-code-20250219'):
            with self.subTest(other=other):
                self.assertEqual(adapt_tool_search_beta(other, upstream_host=self.PROD), (other, False))
        for invalid in (None, 1, ['tool-search-tool-2025-10-19']):
            self.assertEqual(adapt_tool_search_beta(invalid, upstream_host=self.PROD), (invalid, False))

    def test_position_spacing_and_existing_equivalent_are_preserved(self):
        cases = (
            (TOOL_SEARCH_BETA, TOOL_SEARCH_REPLACEMENT_BETA),
            ('a-2025-01-01, ' + TOOL_SEARCH_BETA + ' ,b-2025-01-01', 'a-2025-01-01,' + TOOL_SEARCH_REPLACEMENT_BETA + ',b-2025-01-01'),
            (TOOL_SEARCH_REPLACEMENT_BETA + ',' + TOOL_SEARCH_BETA, TOOL_SEARCH_REPLACEMENT_BETA),
            (TOOL_SEARCH_BETA + ',x-2025-01-01, ' + TOOL_SEARCH_REPLACEMENT_BETA, 'x-2025-01-01, ' + TOOL_SEARCH_REPLACEMENT_BETA),
            (TOOL_SEARCH_BETA + ',' + TOOL_SEARCH_BETA, TOOL_SEARCH_REPLACEMENT_BETA),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(adapt_tool_search_beta(header, upstream_host=self.PROD), (expected, True))


class PromptTooLargeTests(unittest.TestCase):
    PROD = 'loki.delve.office.com'
    DLP = (b'{"error":{"message":"The request text exceeds the configured prompt data protection scan limit.",'
           b'"type":"invalid_request_error","code":"prompt_dlp_content_too_large"}}')

    def test_exact_prod_scan_limit_413_becomes_a_400_claude_code_compacts_on(self):
        status, error = adapt_prompt_too_large(413, self.DLP, upstream_host=self.PROD)
        self.assertEqual(status, 400)
        self.assertEqual(error, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': PROMPT_TOO_LONG_MESSAGE,
                                                            'param': None, 'code': 'prompt_too_long'}})
        message = error['error']['message']
        # Claude Code 2.1.317 matches these case-insensitively / as a token (see native_compat).
        self.assertTrue(message.lower().startswith('prompt is too long'))
        self.assertTrue(message.endswith('capability_rejected: prompt_too_long'))
        # No digits: Claude Code would read "<N> tokens > <M>" from the message as a token gap.
        self.assertFalse(any(ch.isdigit() for ch in message))

    def test_other_hosts_statuses_codes_messages_and_bodies_pass_through(self):
        self.assertIsNone(adapt_prompt_too_large(413, self.DLP, upstream_host=self.PROD, enabled=False))
        for host in ('df.loki.delve.office.com', 'example.invalid', 'loki.delve.office.com.example.invalid', None):
            with self.subTest(host=host):
                self.assertIsNone(adapt_prompt_too_large(413, self.DLP, upstream_host=host))
        for status in (400, 401, 403, 404, 429, 500, 200):
            with self.subTest(status=status):
                self.assertIsNone(adapt_prompt_too_large(status, self.DLP, upstream_host=self.PROD))
        variants = (
            self.DLP.replace(b'prompt_dlp_content_too_large', b'request_too_large'),
            self.DLP.replace(b'scan limit.', b'scan limit'),
            self.DLP.replace(b'"code":"prompt_dlp_content_too_large"', b'"code":null'),
            b'{"error":{"message":"Upstream rejected the request.","type":"invalid_request_error","code":"invalid_body"}}',
            b'{"message":"The request text exceeds the configured prompt data protection scan limit.",'
            b'"code":"prompt_dlp_content_too_large"}',
            b'{"error":"prompt_dlp_content_too_large"}', b'[]', b'null', b'', b'<html>413</html>', bytes([255]), None, b'[' * 2000,
        )
        for raw in variants:
            with self.subTest(raw=raw[:80] if raw else raw):
                self.assertIsNone(adapt_prompt_too_large(413, raw, upstream_host=self.PROD))
