"""Offline Desktop policy lifecycle and loopback protocol regression tests."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from claude_desktop import (POLICY, BASE_URL, apply, ConfigurationError, machine_blocks,
                            RegistryPolicy, state_lock, local_configuration_present,
                            configure_desktop, LOCAL_ENTRY_ID, LOCAL_VALUES, LOCAL_META,
                            LOCAL_STATE, create_local_file, save_state)
from desktop_models import desktop_catalog, desktop_message_path
from server import Handler
from test_native_api import FakeResponse, FakeUpstream
from usage_store import UsageStore

CATALOG = [{'model_id': name, 'supported_reasoning_efforts': '["high"]'} for name in
           ('claude-opus-5', 'claude-haiku-4.5', 'claude-opus-5.5', 'claude-sonnet-5.5')]


class MemoryRegistry:
    def __init__(self):
        self.user, self.machine, self.writes = {}, {}, 0
        self.fail_after = None
        self.writable = True

    def can_write(self):
        return self.writable

    def read(self, machine=False):
        return dict(self.machine if machine else self.user)

    def set(self, name, value):
        if self.writes == self.fail_after:
            raise OSError('synthetic write failure')
        self.user[name] = (value, 1)
        self.writes += 1

    def delete(self, name):
        del self.user[name]


class DesktopPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.registry = MemoryRegistry()

    def run_mode(self, mode, dry_run=False):
        return apply(mode, self.registry, self.path, dry_run)

    def test_install_idempotence_and_restore(self):
        self.assertFalse(self.run_mode('status')['configured'])
        self.assertTrue(self.run_mode('enable')['configured'])
        first = self.path.read_bytes()
        self.assertEqual(self.run_mode('enable')['changed_values'], 0)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertTrue(self.run_mode('status')['configured'])
        self.assertEqual(self.registry.user['inferenceGatewayBaseUrl'], (BASE_URL, 1))
        self.assertEqual(self.run_mode('restore')['changed_values'], len(POLICY))
        self.assertEqual(self.registry.user, {})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_mode('restore')['changed_values'], 0)

    def test_preview_never_writes(self):
        self.run_mode('enable', True)
        self.assertEqual(self.registry.user, {})
        self.assertFalse(self.path.exists())
        self.run_mode('enable')
        self.run_mode('restore', True)
        self.assertEqual(len(self.registry.user), len(POLICY))
        self.assertTrue(self.path.exists())

    def test_local_profile_not_shadowed_or_read(self):
        root = Path(self.temp.name)
        self.assertFalse(local_configuration_present(root))
        library = root / 'Claude-3p' / 'configLibrary'
        library.mkdir(parents=True)
        # Deliberately invalid contents: detection must never need to parse it.
        (library / '_meta.json').write_text('not parsed')
        self.assertTrue(local_configuration_present(root))
        self.assertTrue(local_configuration_present(root / 'other', root))
        with self.assertRaisesRegex(ConfigurationError, '^local_configuration_conflict$'):
            apply('enable', self.registry, self.path, local_config=True)
        self.assertEqual(self.registry.user, {})
        self.assertFalse(self.path.exists())
        self.run_mode('enable')
        # Desktop may create its local library after our first installation.
        self.assertTrue(apply('enable', self.registry, self.path, local_config=True)['configured'])

    def test_existing_user_policy_including_identical_unowned_values_is_preserved(self):
        for original in ({'unrelated': ('user-managed', 1)},
                         {name: (value, 1) for name, value in POLICY.items()}):
            self.registry.user = copy.deepcopy(original)
            with self.assertRaisesRegex(ConfigurationError, '^user_policy_conflict$'):
                self.run_mode('enable')
            self.run_mode('restore')
            self.assertEqual(self.registry.user, original)
            self.assertFalse(self.path.exists())

    def test_machine_policy_presence_including_empty_default_blocks(self):
        for kind in (1, 2, 4):
            self.registry.machine = {'': ('', kind)}
            with self.assertRaisesRegex(ConfigurationError, '^machine_policy_conflict$'):
                self.run_mode('enable')
        for kind in (3, 7, 11):
            self.assertFalse(machine_blocks({'': ('', kind)}))
        self.assertEqual(self.registry.user, {})

    def test_partial_install_can_resume_or_restore(self):
        self.registry.fail_after = 3
        with self.assertRaises(OSError):
            self.run_mode('enable')
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.registry.user), 3)
        self.registry.fail_after = None
        self.assertTrue(self.run_mode('enable')['configured'])
        self.registry.machine = {'': ('admin', 1)}
        # Cleanup of owned HKCU values remains possible under new HKLM policy.
        self.run_mode('restore')
        self.assertEqual(self.registry.user, {})
        self.assertEqual(self.registry.machine, {'': ('admin', 1)})

    def test_changed_and_unrelated_values_preserved(self):
        self.run_mode('enable')
        self.registry.user['toolSearchEnabled'] = ('false', 1)
        self.registry.user['unrelated'] = ('user-managed', 1)
        with self.assertRaisesRegex(ConfigurationError, '^user_policy_conflict$'):
            self.run_mode('enable')
        result = self.run_mode('restore')
        self.assertEqual(result['preserved_values'], 2)
        self.assertEqual(self.registry.user, {'toolSearchEnabled': ('false', 1),
                                             'unrelated': ('user-managed', 1)})

    def test_later_gateway_is_not_broken_by_restore(self):
        self.run_mode('enable')
        self.registry.user['inferenceGatewayBaseUrl'] = ('https://example.invalid', 1)
        before = self.registry.read()
        self.assertEqual(self.run_mode('restore')['changed_values'], 0)
        self.assertEqual(self.registry.read(), before)

    def test_modified_manifest_cannot_delete_arbitrary_policy(self):
        self.path.write_text('{"owner":"someone-else","values":{"unrelated":"x"}}')
        self.registry.user['unrelated'] = ('x', 1)
        with self.assertRaisesRegex(ConfigurationError, '^state_conflict$'):
            self.run_mode('restore')
        self.assertEqual(self.registry.user, {'unrelated': ('x', 1)})


@unittest.skipUnless(os.name == 'nt', 'Windows local configuration library')
class DesktopLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'integration' / 'state.json'
        self.library = self.root / 'Claude-3p' / 'configLibrary'
        self.entry = self.library / (LOCAL_ENTRY_ID + '.json')
        self.meta = self.library / '_meta.json'
        self.registry = MemoryRegistry()
        self.registry.writable = False

    def run_mode(self, mode, dry_run=False):
        return configure_desktop(mode, self.registry, self.state, self.root, dry_run)

    def test_denied_registry_uses_local_library_idempotently_and_restores(self):
        result = self.run_mode('enable')
        self.assertTrue(result['configured'])
        self.assertEqual(result['configuration_source'], 'local')
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(json.loads(self.entry.read_text()), LOCAL_VALUES)
        self.assertIs(json.loads(self.entry.read_text())['toolSearchEnabled'], True)
        self.assertEqual(json.loads(self.meta.read_text()), LOCAL_META)
        self.assertEqual(json.loads(self.state.read_text()), LOCAL_STATE)
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.entry, self.meta, self.state)]
        self.assertEqual(self.run_mode('enable')['changed_values'], 0)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.entry, self.meta, self.state)], before)
        self.assertTrue(self.run_mode('status')['configured'])
        # Existing user data elsewhere is not part of restore.
        personal = self.library.parent / 'personal.txt'
        personal.write_text('synthetic user data')
        self.run_mode('restore', True)
        self.assertTrue(self.meta.exists())
        result = self.run_mode('restore')
        self.assertEqual(result['changed_values'], len(LOCAL_VALUES))
        self.assertFalse(self.entry.exists())
        self.assertFalse(self.meta.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(personal.read_text(), 'synthetic user data')
        self.assertEqual(self.run_mode('restore')['changed_values'], 0)

    def test_preview_selects_local_without_writing(self):
        result = self.run_mode('enable', True)
        self.assertEqual(result['configuration_source'], 'local')
        self.assertEqual(result['changed_values'], len(LOCAL_VALUES))
        self.assertFalse(self.library.exists())
        self.assertFalse(self.state.exists())

    def test_saved_manifest_from_failed_registry_install_can_migrate(self):
        self.registry.fail_after = 0
        with self.assertRaises(OSError):
            apply('enable', self.registry, self.state)
        self.assertTrue(self.state.exists())
        self.assertFalse(self.registry.user)
        self.assertTrue(self.run_mode('enable')['configured'])
        self.assertEqual(json.loads(self.state.read_text()), LOCAL_STATE)

    def test_partial_registry_install_cannot_be_shadowed_by_local_fallback(self):
        self.registry.fail_after = 1
        with self.assertRaises(OSError):
            apply('enable', self.registry, self.state)
        before = self.state.read_bytes(), self.registry.read()
        with self.assertRaisesRegex(ConfigurationError, '^registry_write_permission_denied$'):
            self.run_mode('enable')
        self.assertEqual((self.state.read_bytes(), self.registry.read()), before)
        self.assertFalse(self.library.exists())

    def test_machine_and_user_policies_are_never_bypassed(self):
        self.registry.machine = {'': ('', 1)}
        with self.assertRaisesRegex(ConfigurationError, '^machine_policy_conflict$'):
            self.run_mode('enable')
        self.registry.machine = {}
        self.registry.user = {'toolSearchEnabled': ('false', 1)}
        with self.assertRaisesRegex(ConfigurationError, '^user_policy_conflict$'):
            self.run_mode('enable')
        self.assertFalse(self.state.exists())
        self.assertFalse(self.library.exists())

    def test_existing_local_deployment_is_not_read_or_overwritten(self):
        self.library.mkdir(parents=True)
        self.meta.write_text('not parsed')
        with patch('claude_desktop.read_local_file', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ConfigurationError, '^local_configuration_conflict$'):
                self.run_mode('enable')
        self.assertEqual(self.meta.read_text(), 'not parsed')
        self.assertFalse(self.state.exists())

    def test_existing_local_deployment_after_registry_failure_still_blocks(self):
        self.registry.fail_after = 0
        with self.assertRaises(OSError):
            apply('enable', self.registry, self.state)
        self.library.mkdir(parents=True)
        self.meta.write_text('not parsed')
        with self.assertRaisesRegex(ConfigurationError, '^local_configuration_conflict$'):
            self.run_mode('enable')
        self.assertEqual(self.meta.read_text(), 'not parsed')

    def test_interrupted_local_install_resumes(self):
        def create(path, values):
            if path == self.meta:
                raise PermissionError('synthetic failure')
            create_local_file(path, values)
        with patch('claude_desktop.create_local_file', side_effect=create):
            with self.assertRaises(PermissionError):
                self.run_mode('enable')
        self.assertTrue(self.state.exists())
        self.assertTrue(self.entry.exists())
        self.assertFalse(self.meta.exists())
        self.assertTrue(self.run_mode('enable')['configured'])

    def test_user_modified_entry_or_library_pointer_is_preserved(self):
        for target in ('entry', 'meta'):
            with self.subTest(target=target):
                self.run_mode('enable')
                path = self.entry if target == 'entry' else self.meta
                values = json.loads(path.read_text())
                values['inferenceGatewayBaseUrl' if target == 'entry' else 'appliedId'] = 'synthetic-user-change'
                path.write_text(json.dumps(values))
                before = self.entry.read_bytes(), self.meta.read_bytes()
                with self.assertRaisesRegex(ConfigurationError, '^local_configuration_conflict$'):
                    self.run_mode('enable')
                self.assertFalse(self.run_mode('status')['configured'])
                result = self.run_mode('restore')
                self.assertEqual(result['preserved_values'], len(LOCAL_VALUES))
                self.assertEqual((self.entry.read_bytes(), self.meta.read_bytes()), before)
                self.assertFalse(self.state.exists())
                # Only synthetic files in the temporary test directory.
                self.entry.unlink()
                self.meta.unlink()

    def test_concurrent_local_file_creation_is_never_overwritten(self):
        self.library.mkdir(parents=True)
        self.meta.write_text('synthetic concurrent writer')
        with self.assertRaisesRegex(ConfigurationError, '^local_configuration_changed_during_setup$'):
            create_local_file(self.meta, LOCAL_META)
        self.assertEqual(self.meta.read_text(), 'synthetic concurrent writer')
        self.assertFalse(list(self.library.glob('.copilot-config-*.tmp')))

    def test_io_failure_reason_identifies_stage_without_error_text(self):
        with patch('claude_desktop.write_json', side_effect=PermissionError('synthetic private text')):
            with self.assertRaisesRegex(ConfigurationError, '^state_write_permission_denied$'):
                save_state(self.state, LOCAL_STATE)
        with patch('claude_desktop.os.rename', side_effect=PermissionError('synthetic private text')):
            with self.assertRaisesRegex(ConfigurationError, '^local_configuration_write_permission_denied$'):
                create_local_file(self.meta, LOCAL_META)
        with patch('winreg.CreateKeyEx', side_effect=PermissionError('synthetic private text')):
            with self.assertRaisesRegex(ConfigurationError, '^registry_write_permission_denied$'):
                RegistryPolicy().set('toolSearchEnabled', 'true')
        with patch('winreg.OpenKey', side_effect=PermissionError('synthetic private text')):
            self.assertFalse(RegistryPolicy().can_write())


class DesktopCatalogTests(unittest.TestCase):
    def test_actual_models_and_tiers_with_live_default(self):
        original = copy.deepcopy(CATALOG)
        data = desktop_catalog(CATALOG)
        self.assertEqual(data['first_id'], 'claude-opus-5-5')
        self.assertFalse(data['has_more'])
        rows = {m['id']: m for m in data['data']}
        self.assertEqual(len(rows), 4)
        self.assertTrue(all('[1m]' not in k and k.startswith('claude-') for k in rows))
        self.assertTrue(all(m['supports_1m'] for m in rows.values()))
        self.assertFalse(rows['claude-opus-5']['is_family_default'])
        self.assertEqual(rows['claude-haiku-4-5']['anthropic_family_tier'], 'haiku')
        self.assertEqual(CATALOG, original)
        future = CATALOG + [{'model_id': 'claude-opus-6'}]
        self.assertEqual(desktop_catalog(future)['first_id'], 'claude-opus-6')
        self.assertEqual(desktop_catalog([])['data'], [])

    def test_denylist_and_duplicate_ids(self):
        with patch('model_selection.CONTEXT_1M_DENYLIST', frozenset({'claude-opus-5.5'})):
            rows = desktop_catalog([{'model_id': 'claude-opus-5-5'}] + CATALOG)['data']
        self.assertEqual(len(rows), 4)
        self.assertFalse(rows[0]['supports_1m'])
        self.assertTrue(rows[0]['is_family_default'])
        self.assertTrue(rows[1]['supports_1m'])

    def test_only_documented_paths_are_mapped(self):
        for tail in ('/v1/messages', '/v1/messages/count_tokens'):
            self.assertEqual(desktop_message_path('/desktop' + tail + '?beta=true&x=%2F'),
                             tail + '?beta=true&x=%2F')
        for path in ('/v1/messages', '/desktop/authorization', '/desktop/v1/chat/completions',
                     '/desktopx/v1/messages', '/desktop/../v1/messages'):
            self.assertEqual(desktop_message_path(path), path)


@unittest.skipUnless(os.name == 'nt', 'Windows registry and lock integration')
class WindowsPolicyTests(unittest.TestCase):
    def test_real_registry_roundtrip_in_disposable_non_policy_key(self):
        import winreg
        key = 'SOFTWARE\\CopilotDesktopProxy\\Tests\\' + uuid.uuid4().hex
        with TemporaryDirectory() as temp, patch('claude_desktop.POLICY_KEY', key):
            registry = RegistryPolicy()
            state = Path(temp) / 'state.json'
            try:
                self.assertEqual(registry.read(), {})
                self.assertTrue(apply('enable', registry, state)['configured'])
                self.assertEqual(registry.read(), {k: (v, winreg.REG_SZ) for k, v in POLICY.items()})
                self.assertTrue(apply('status', registry, state)['configured'])
                apply('restore', registry, state)
                self.assertEqual(registry.read(), {})
            finally:
                # Only the unique test key can be removed, never the real policy.
                for name in list(registry.read()):
                    registry.delete(name)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)

    def test_installers_serialize_and_release_lock(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            with state_lock(directory):
                with self.assertRaisesRegex(ConfigurationError, '^another_configuration_is_running$'):
                    with state_lock(directory):
                        self.fail('Second lock should fail')
            with state_lock(directory):
                pass


class DesktopWireTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.default_effort, self.server.upstream_timeout = 'xhigh', 5
        self.server.usage_store = UsageStore(Path(self.temp.name) / 'usage.sqlite3')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.config = patch('server.load_config', return_value=(
            'https://loki.delve.office.com/api/v1/llmapi', {}, CATALOG))
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        request = Request(self.url + path, data=body,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_separate_catalog_keeps_cli_unchanged(self):
        with patch('server.http.client.HTTPSConnection', side_effect=AssertionError('No upstream allowed')):
            code, body = self.request('/desktop/v1/models?limit=1000')
            _, cli_body = self.request('/v1/models')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['data'], desktop_catalog(CATALOG)['data'])
        self.assertTrue(any(m['id'] == 'copilot-auto[1m]' for m in json.loads(cli_body)['data']))

    def test_messages_and_token_count_use_shared_native_forwarder(self):
        for path, result in (('/v1/messages', b'{"type":"message","content":[],"usage":{"input_tokens":3,"output_tokens":1}}'),
                             ('/v1/messages/count_tokens', b'{"input_tokens":3}')):
            body = b'{ "model":"claude-opus-5-5[1m]", "messages":[{"role":"user","content":"test"}], "max_tokens":16, "metadata":{"nested":"unchanged"} }'
            upstream = FakeUpstream(FakeResponse(result))
            with patch('server.http.client.HTTPSConnection', return_value=upstream) as connection:
                status, raw = self.request('/desktop' + path + '?beta=true', body,
                    {'anthropic-beta': 'context-1m-2025-08-07', 'anthropic-version': '2023-06-01'})
            self.assertEqual((status, raw), (200, result))
            self.assertEqual(connection.call_count, 1)
            self.assertEqual(upstream.captured[1], '/api/v1/llmapi' + path + '?beta=true')
            self.assertEqual(upstream.captured[2], body.replace(b'claude-opus-5-5[1m]', b'claude-opus-5.5'))
            self.assertEqual(upstream.captured[3]['anthropic-beta'], 'context-1m-2025-08-07')
        # The HTTP body can reach the client before the handler's finally block
        # commits usage. Wait for that observable completion, with a hard bound.
        deadline = time.monotonic() + 3
        report = self.server.usage_store.report('day')
        while report['totals']['requests'] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
            report = self.server.usage_store.report('day')
        self.assertEqual(report['totals']['requests'], 1)

    def test_stream_and_status_are_not_hidden(self):
        raw = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
               b'event: ping\ndata: {"type":"ping"}\n\n'
               b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        body = b'{"model":"claude-opus-5-5","stream":true,"max_tokens":16,"messages":[{"role":"user","content":"test"}]}'
        upstream = FakeUpstream(FakeResponse(raw, 'text/event-stream'))
        with patch('server.http.client.HTTPSConnection', return_value=upstream):
            self.assertEqual(self.request('/desktop/v1/messages', body), (200, raw))
        upstream = FakeUpstream(FakeResponse(b'{"error":{"message":"Synthetic rejection"}}', status=400))
        with patch('server.http.client.HTTPSConnection', return_value=upstream) as connection:
            self.assertEqual(self.request('/desktop/v1/messages', body)[0], 400)
        self.assertEqual(connection.call_count, 1)

    def test_local_guards_and_unsupported_paths_remain_closed(self):
        with patch('server.http.client.HTTPSConnection', side_effect=AssertionError('No upstream allowed')):
            self.assertEqual(self.request('/desktop/v1/models', headers={'Origin': 'https://example.invalid'})[0], 403)
            self.assertEqual(self.request('/desktop/v1/messages', b'{}', {'Origin': 'https://example.invalid'})[0], 403)
            self.assertEqual(self.request('/desktop/v1/models', headers={'Host': 'example.invalid'})[0], 403)
            self.assertEqual(self.request('/desktop/authorization')[0], 404)
            self.assertEqual(self.request('/desktop/v1/chat/completions', b'{}')[0], 404)
            self.assertEqual(self.request('/desktop/v1/messages', b'{"model":"not-in-catalog","messages":[]}')[0], 400)


if __name__ == '__main__':
    unittest.main()
