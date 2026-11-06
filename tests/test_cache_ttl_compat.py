"""Cache TTL compatibility: protocol locations, byte preservation and scope."""
import copy
import json
import unittest

from native_compat import adapt_cache_ttl
from diagnostics import safe_record, load_options
from tempfile import TemporaryDirectory
from pathlib import Path


class CacheTtlTests(unittest.TestCase):
    def adapt(self, raw, **kwargs):
        options = dict(upstream_host='loki.delve.office.com', resolved_model='claude-opus-5.5')
        options.update(kwargs)
        payload = json.loads(raw)
        before = copy.deepcopy(payload)
        result = adapt_cache_ttl(raw, payload, **options)
        self.assertEqual(payload, before)
        return result

    def test_protocol_locations_and_only_ttl_bytes_change(self):
        marker = {'type': 'ephemeral', 'ttl': '1h'}
        payload = {'cache_control': marker, 'system': [{'type': 'text', 'text': '你好', 'cache_control': marker}],
            'tools': [{'name': 'example', 'cache_control': marker}],
            'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x',
                'cache_control': marker, 'content': [{'type': 'text', 'text': 'fixture', 'cache_control': marker}]}]}]}
        raw = json.dumps(payload, indent=2).encode()
        updated, forwarded, changed = self.adapt(raw)
        self.assertTrue(changed)
        self.assertEqual(updated, raw.replace(b'"ttl": "1h"', b'"ttl": "5m"'))
        self.assertEqual(json.loads(updated), forwarded)

    def test_user_data_tools_schema_signatures_and_unknown_fields_untouched(self):
        decoy = {'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}
        payload = {'system': [{'type': 'text', 'text': 'cache_control ttl 1h', **decoy}],
            'tools': [{'name': 'x', 'input_schema': decoy}], 'metadata': decoy,
            'messages': [{'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': '1h', 'signature': '1h'},
                {'type': 'tool_use', 'input': decoy}, {'type': 'document', 'source': decoy}]}]}
        raw = json.dumps(payload).encode()
        updated, _, changed = self.adapt(raw)
        self.assertTrue(changed)
        self.assertEqual(updated, raw.replace(b'"ttl": "1h"', b'"ttl": "5m"', 1))

    def test_scope_opt_out_and_unknown_ttls_pass_through(self):
        raw = b'{"cache_control":{"type":"ephemeral","ttl":"1h"}}'
        for options in ({'enabled': False}, {'upstream_host': 'df.loki.delve.office.com'},
                        {'upstream_host': 'loki.delve.office.com.invalid'}, {'resolved_model': 'claude-opus-6'}):
            self.assertEqual(self.adapt(raw, **options), (raw, json.loads(raw), False))
        for value in ('5m', '2h', None, 3600):
            body = json.dumps({'cache_control': {'type': 'ephemeral', 'ttl': value}}).encode()
            self.assertEqual(self.adapt(body), (body, json.loads(body), False))
        body = b'{"cache_control":{"type":"unknown","ttl":"1h"}}'
        self.assertEqual(self.adapt(body), (body, json.loads(body), False))

    def test_escaped_keys_values_preserve_surrounding_bytes(self):
        raw = b'{ "cache_contro\\u006c": {"type":"ephemeral", "t\\u0074l" : "1\\u0068"}, "max_tokens":6.4e1 }'
        updated, _, changed = self.adapt(raw, resolved_model='claude-opus-5-5')
        self.assertTrue(changed)
        self.assertEqual(updated, raw.replace(b'"1\\u0068"', b'"5m"'))

    def test_ambiguous_protocol_keys_leave_whole_body_unchanged(self):
        for raw in (b'{"cache_control":{"type":"ephemeral","ttl":"5m","ttl":"1h"}}',
                    b'{"cache_control":{"type":"ephemeral","ttl":"1h"},"messages":[],"messages":[]}'):
            self.assertEqual(self.adapt(raw), (raw, json.loads(raw), False))

    def test_diagnostics_and_boolean_option(self):
        self.assertEqual(safe_record({'cache_ttl_compat_applied': True, 'body': 'private'}),
                         {'cache_ttl_compat_applied': True})
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'proxy-options.json'
            path.write_text('{"loki_cache_ttl_compat": false}')
            self.assertFalse(load_options(folder)['loki_cache_ttl_compat'])
            path.write_text('{"loki_cache_ttl_compat": "false"}')
            with self.assertRaises(ValueError):
                load_options(folder)


if __name__ == '__main__':
    unittest.main()
