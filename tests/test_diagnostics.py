"""Offline privacy and schema checks for request feature diagnostics."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from diagnostics import BETA_FAMILIES, ENUMS, EventLog, request_metadata, safe_record, upstream_error_metadata


class RequestMetadataTests(unittest.TestCase):
    def test_thinking_display_compat_metadata_is_fixed_and_safe_to_export(self):
        fields = request_metadata({'thinking': {'type': 'adaptive', 'display': 'updates'}},
            '/v1/messages', 'thinking-display-updates-2026-08-18')
        self.assertEqual(fields['thinking_display_mode'], 'updates')
        self.assertTrue(fields['beta_thinking_display_updates'])
        record = {**fields, 'thinking_display_compat_applied': True, 'loki_thinking_display_compat': True,
                  'private_body': 'PRIVATE_BODY', 'private_header': 'PRIVATE_HEADER'}
        safe = safe_record(record)
        self.assertTrue(safe['thinking_display_compat_applied'])
        self.assertTrue(safe['loki_thinking_display_compat'])
        self.assertNotIn('PRIVATE', json.dumps(safe))

    def test_gateway_tool_search_beta_is_a_known_family(self):
        payload = {'tools': [{'name': 'PRIVATE', 'input_schema': {'type': 'object'}, 'defer_loading': True}]}
        for beta in ('tool-search-tool-2025-10-19', 'tool-search-2025-10-19'):
            fields = request_metadata(payload, '/v1/messages', 'claude-code-20250219,' + beta)
            self.assertTrue(fields['beta_tool_search'])
            self.assertEqual((fields['beta_count'], fields['beta_unknown_count']), (2, 0))
            self.assertEqual(fields['deferred_tool_count'], 1)
        safe = safe_record({'tool_search_beta_compat_applied': True, 'loki_tool_search_beta_compat': False})
        self.assertEqual(safe, {'tool_search_beta_compat_applied': True, 'loki_tool_search_beta_compat': False})

    def test_counts_structural_features_without_content_or_mutation(self):
        payload = {
            'model': 'PRIVATE_MODEL', 'max_tokens': 64000,
            'system': [{'type': 'text', 'text': 'PRIVATE_SYSTEM', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
            'metadata': {'user_id': 'PRIVATE_ACCOUNT_SESSION'},
            'messages': [
                {'role': 'user', 'content': 'PRIVATE_PROMPT'},
                {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'PRIVATE_THINKING', 'signature': 'PRIVATE_SIGNATURE'},
                    {'type': 'tool_use', 'name': 'PRIVATE_NAME', 'input': {'type': 'tool_reference'}}]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'PRIVATE_ID', 'content': [
                        {'type': 'tool_reference', 'tool_name': 'PRIVATE_TOOL'},
                        {'type': 'text', 'text': 'PRIVATE_TOOL_RESULT', 'cache_control': {'type': 'ephemeral'}}]},
                    {'type': 'tool_reference', 'tool_name': 'PRIVATE_TOOL_2'}]}],
            'tools': [{'name': 'PRIVATE_TOOL', 'description': 'PRIVATE_DESCRIPTION', 'defer_loading': True,
                       'input_schema': {'type': 'tool_reference', 'cache_control': {'ttl': '1h'}}},
                      {'type': 'tool_search_tool_regex_20251119', 'name': 'PRIVATE_SEARCH'},
                      {'type': 'tool_search_tool_bm25', 'name': 'PRIVATE_SEARCH_2',
                       'cache_control': {'type': 'ephemeral'}}],
            'thinking': {'type': 'enabled', 'budget_tokens': 8192},
            'output_config': {'effort': 'xhigh', 'format': {'type': 'json_schema', 'schema': {'PRIVATE_SCHEMA': {}}}},
            'context_management': {'PRIVATE_EDITS': []}, 'tool_choice': {'type': 'auto'},
            'temperature': 1, 'top_p': 0.9, 'top_k': 20, 'service_tier': 'auto', 'stop_sequences': ['PRIVATE_STOP'],
        }
        before = copy.deepcopy(payload)
        fields = request_metadata(payload, '/v1/messages',
            'context-1m-2025-08-07,advanced-tool-use-2025-11-20,claude-code-20250219,PRIVATE_HEADER,, ')
        self.assertEqual(payload, before)
        self.assertEqual((fields['message_count'], fields['tool_count']), (3, 3))
        self.assertEqual((fields['deferred_tool_count'], fields['tool_search_tool_count'], fields['tool_reference_count']), (1, 2, 2))
        self.assertEqual((fields['cache_control_count'], fields['cache_control_1h_count']), (3, 1))
        self.assertEqual((fields['thinking_mode'], fields['effort_mode'], fields['tool_choice_mode']), ('enabled', 'xhigh', 'auto'))
        self.assertEqual(fields['output_format_mode'], 'json_schema')
        self.assertEqual((fields['max_tokens'], fields['thinking_budget_tokens']), (64000, 8192))
        self.assertEqual((fields['beta_count'], fields['beta_unknown_count']), (4, 1))
        self.assertTrue(fields['beta_context_1m'])
        self.assertTrue(fields['beta_advanced_tool_use'])
        self.assertTrue(fields['beta_claude_code'])
        self.assertFalse(fields['beta_effort'])
        self.assertTrue(fields['has_context_management'])
        self.assertTrue(fields['request_metadata_complete'])
        self.assertNotIn('PRIVATE_', json.dumps(fields))
        self.assertEqual(fields, safe_record(fields))

    def test_unknown_and_malformed_types_remain_bounded_private_categories(self):
        for invalid in (True, 3.5, float('nan'), 'PRIVATE_VALUE', [], {'PRIVATE_VALUE': 1}):
            with self.subTest(invalid=invalid):
                fields = request_metadata({'messages': invalid, 'tools': [invalid, {'type': invalid}],
                    'thinking': {'type': invalid, 'budget_tokens': invalid},
                    'output_config': {'effort': invalid}, 'tool_choice': invalid, 'max_tokens': invalid,
                    'output_format': invalid}, '/PRIVATE_PATH', 'PRIVATE_HEADER')
                self.assertTrue(fields['request_metadata_complete'])
                self.assertEqual(fields['endpoint_kind'], 'other')
                self.assertEqual(fields['thinking_mode'], 'other')
                self.assertEqual(fields['effort_mode'], 'other')
                self.assertNotIn('max_tokens', fields)
                self.assertNotIn('thinking_budget_tokens', fields)
                self.assertNotIn('PRIVATE_', json.dumps(fields))
        for number in (-1, 2**31, True, None):
            self.assertNotIn('max_tokens', request_metadata({'max_tokens': number}, '/v1/messages', ''))
        self.assertEqual(request_metadata({'max_tokens': 0}, '/v1/messages', '')['max_tokens'], 0)

    def test_optional_fields_and_nested_malformed_content_do_not_break_metadata(self):
        for path, kind in (('/v1/messages', 'messages'), ('/v1/messages/count_tokens', 'count_tokens'),
                           ('/v1/chat/completions', 'chat_completions')):
            fields = request_metadata({}, path, None)
            self.assertEqual(fields['endpoint_kind'], kind)
            self.assertEqual(fields['thinking_mode'], 'absent')
            self.assertEqual(fields['effort_mode'], 'absent')
            self.assertFalse(fields['has_output_config'])
        payload = {'system': [None], 'messages': [None, {}, {'content': [None, {'type': 'tool_result', 'content': 'PRIVATE'}]}],
                   'thinking': 'PRIVATE', 'output_config': 'PRIVATE', 'tool_choice': {'type': 'tool', 'name': 'PRIVATE'},
                   'tools': None}
        fields = request_metadata(payload, '/v1/messages', 42)
        self.assertTrue(fields['request_metadata_complete'])
        self.assertEqual(fields['thinking_mode'], 'other')
        self.assertEqual(fields['effort_mode'], 'other')
        self.assertEqual(fields['tool_choice_mode'], 'tool')
        self.assertEqual(fields['tool_count'], 0)
        self.assertEqual(fields['beta_count'], 0)
        self.assertNotIn('PRIVATE', json.dumps(fields))

    def test_telemetry_failure_returns_only_a_failure_marker(self):
        with patch('diagnostics.re.fullmatch', side_effect=RuntimeError('PRIVATE_FAILURE')):
            fields = request_metadata({}, '/v1/messages', 'context-1m-2025-08-07')
        self.assertEqual(fields, {'request_metadata_version': 2, 'request_metadata_complete': False})

    def test_extended_wire_features_are_counted_without_content_or_mutation(self):
        payload = {
            'PRIVATE_TOP_LEVEL': 'PRIVATE_VALUE', 'speed': 'fast',
            'thinking': {'type': 'adaptive', 'display': 'omitted'},
            'system': [{'type': 'text', 'text': 'PRIVATE_SYSTEM',
                        'cache_control': {'type': 'ephemeral', 'scope': 'PRIVATE_SCOPE'}}],
            'messages': [
                {'role': 'system', 'clear_at': 'PRIVATE_CLEAR_AT', 'PRIVATE_MESSAGE_FIELD': 'PRIVATE_VALUE',
                 'output_config': {'effort': 'xhigh', 'timing': 'PRIVATE_TIME'},
                 'content': [
                     {'type': 'text', 'text': 'PRIVATE_SYSTEM_TURN'},
                     {'type': 'tool_addition', 'tool': {'type': 'tool_definition',
                         'definition': {'name': 'PRIVATE_NAME', 'description': 'PRIVATE_DESCRIPTION'}}},
                     {'type': 'tool_removal', 'tool': {'type': 'tool_reference', 'name': 'PRIVATE_TOOL'}}]},
                {'role': 'user', 'content': 'PRIVATE_PROMPT'},
                {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'PRIVATE_THINKING', 'signature': 'PRIVATE_SIGNATURE'},
                    {'type': 'redacted_thinking', 'data': 'PRIVATE_DATA'},
                    {'type': 'tool_use', 'id': 'PRIVATE_ID', 'input': {'PRIVATE_INPUT': 1}},
                    {'type': 'image', 'source': {'data': 'PRIVATE_IMAGE'}},
                    {'type': 'document', 'source': {'data': 'PRIVATE_DOCUMENT'}},
                    {'type': 'PRIVATE_BLOCK', 'text': 'PRIVATE_TEXT'}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'PRIVATE_ID',
                    'content': [{'type': 'tool_reference', 'tool_name': 'PRIVATE_NAME'},
                                {'type': 'text', 'text': 'PRIVATE_RESULT'}]}]},
            ],
            'tools': [{'name': 'PRIVATE_TOOL', 'strict': True,
                       'input_schema': {'type': 'object', 'properties': {'PRIVATE_PROPERTY': {'type': 'string'}}}},
                      {'type': 'tool_search_tool_regex_20251119', 'name': 'PRIVATE_SEARCH'},
                      {'name': 'PRIVATE_TOOL_2', 'strict': True, 'input_schema': {'type': 'PRIVATE_SCHEMA_TYPE'}}],
        }
        before = copy.deepcopy(payload)
        fields = request_metadata(payload, '/v1/messages',
            'mid-conversation-system-2026-04-07,per-turn-control-2026-07-01,'
            'timing-2026-09-09,inline-tools-2026-09-15,PRIVATE_BETA')
        self.assertEqual(payload, before)
        self.assertEqual(fields['request_metadata_version'], 2)
        self.assertTrue(fields['request_metadata_complete'])
        self.assertEqual([fields['message_role_' + role + '_count'] for role in ('user', 'assistant', 'system', 'other')],
                         [2, 1, 1, 0])
        for name in ('message_output_config_count', 'message_effort_count', 'message_timing_count',
                     'message_clear_at_count', 'message_unknown_field_count', 'top_level_unknown_field_count',
                     'inline_tool_definition_count', 'cache_control_scope_count', 'tool_schema_non_object_count'):
            self.assertEqual(fields[name], 1, name)
        self.assertEqual(fields['content_text_count'], 4)
        for kind in ('thinking', 'redacted_thinking', 'tool_use', 'tool_result',
                     'tool_addition', 'tool_removal', 'image', 'document', 'other'):
            self.assertEqual(fields['content_' + kind + '_count'], 1, kind)
        self.assertEqual(fields['tool_strict_count'], 2)
        self.assertEqual(fields['speed_mode'], 'fast')
        self.assertEqual(fields['thinking_display_mode'], 'omitted')
        self.assertEqual(fields['beta_unknown_count'], 1)
        for family in ('mid_conversation_system', 'per_turn_control', 'timing', 'inline_tools'):
            self.assertTrue(fields['beta_' + family])
        self.assertNotIn('PRIVATE', json.dumps(fields))
        self.assertEqual(fields, safe_record(fields))

    def test_malformed_extended_fields_stay_private_and_never_change_payload(self):
        for invalid in (None, True, 42, 'PRIVATE_TYPE', [], {'PRIVATE_KEY': 'PRIVATE_VALUE'}):
            payload = {'speed': invalid, 'thinking': {'display': invalid}, 'system': 'PRIVATE_SYSTEM',
                       'messages': [None, {'role': invalid, 'output_config': invalid, 'content': [None,
                           {'type': invalid}, {'type': 'tool_addition', 'tool': invalid}]}],
                       'tools': [{'input_schema': invalid, 'strict': 'PRIVATE_STRICT'}]}
            before = copy.deepcopy(payload)
            fields = request_metadata(payload, '/v1/messages', '')
            self.assertTrue(fields['request_metadata_complete'])
            self.assertEqual(fields['message_role_other_count'], 2)
            self.assertEqual(fields['message_output_config_count'], 1)
            self.assertEqual(fields['message_effort_count'], 0)
            self.assertEqual(fields['inline_tool_definition_count'], 0)
            self.assertEqual(fields['content_other_count'], 2)
            self.assertEqual(fields['content_text_count'], 1)
            self.assertEqual(fields['tool_schema_non_object_count'], 1)
            self.assertEqual(fields['tool_strict_count'], 0)
            self.assertEqual(fields['speed_mode'], 'absent' if invalid is None else 'other')
            self.assertEqual(fields['thinking_display_mode'], 'absent' if invalid is None else 'other')
            self.assertEqual(payload, before)
            self.assertNotIn('PRIVATE', json.dumps(fields))

    def test_beta_family_detection_never_logs_unknown_header_text(self):
        header = ','.join(f'{name}-2026-10-16' for name in BETA_FAMILIES)
        fields = request_metadata({}, '/v1/messages', header + ',PRIVATE_VALUE,context-1m-PRIVATE')
        self.assertTrue(all(fields[key] for key in BETA_FAMILIES.values()))
        self.assertEqual(fields['beta_unknown_count'], 2)
        self.assertEqual(fields['beta_count'], len(BETA_FAMILIES) + 2)
        self.assertNotIn('PRIVATE_', json.dumps(fields))

    def test_error_metadata_uses_exact_machine_codes_and_never_error_prose(self):
        fields = upstream_error_metadata(b'{"error":{"type":"invalid_request_error","code":"BadRequest","message":"Upstream rejected the request."}}')
        self.assertTrue(fields['upstream_generic_rejection'])
        self.assertEqual(fields['upstream_error_code'], 'BadRequest')
        self.assertEqual(fields['upstream_error_type'], 'invalid_request_error')
        for error in ({'code': 'PRIVATE_CODE', 'type': 'PRIVATE_TYPE', 'message': 'context_length_exceeded PRIVATE'},
                      {'code': [], 'type': {}, 'message': {'PRIVATE': 'CONTENT'}}):
            fields = upstream_error_metadata(json.dumps({'error': error}))
            self.assertEqual(fields['upstream_error_code'], 'other')
            self.assertEqual(fields['upstream_error_type'], 'other')
            self.assertFalse(fields['upstream_generic_rejection'])
            self.assertNotIn('PRIVATE', json.dumps(fields))
        for raw in (b'[]', b'{"error":"PRIVATE"}', b'{"error":null}', b'{}'):
            self.assertTrue(upstream_error_metadata(raw)['upstream_error_json'])
            self.assertEqual(upstream_error_metadata(raw)['upstream_error_type'], 'absent')
        for raw in (b'<html>PRIVATE_BODY</html>', b'\xff', None, b'[' * 2000):
            self.assertEqual(upstream_error_metadata(raw), {'upstream_error_json': False})

    def test_exporter_and_log_reject_unknown_values_even_for_known_fields(self):
        self.assertEqual(safe_record(dict.fromkeys(ENUMS, 'PRIVATE')), {})
        self.assertEqual(safe_record(dict.fromkeys(ENUMS, [])), {})
        fields = request_metadata({}, '/v1/messages', 'PRIVATE_HEADER')
        fields.update(upstream_environment='df', upstream_status=400)
        with TemporaryDirectory() as directory:
            log = EventLog(Path(directory) / 'proxy.log')
            try:
                log.event('upstream_response', **fields, body='PRIVATE_BODY', headers='PRIVATE_HEADERS')
                log.event('upstream_http_error', **upstream_error_metadata(b'{"error":{"code":"PRIVATE_CODE"}}'))
            finally:
                log.close()
            text = (Path(directory) / 'proxy.log').read_text()
            self.assertNotIn('PRIVATE', text)
            for line in text.splitlines():
                record = json.loads(line)
                self.assertEqual(record, safe_record(record))


if __name__ == '__main__':
    unittest.main()
