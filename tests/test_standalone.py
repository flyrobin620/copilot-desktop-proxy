"""Standalone gateway mode (no Copilot desktop app) and layout migrations."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import types
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import app_config
import claude_desktop as desktop
from configure_claude import configure
from refresh_models import refresh
from server import Handler
from token_refresh import CLIENT_ID, RESOURCE, ModelCatalog, RefreshError, TokenRefresher
import token_helper
import test_claude_desktop as desktop_fixtures

from tests import CONFIG

UPSTREAM = 'https://loki.delve.office.com/api/v1/llmapi'
SETTINGS = {'enabled': True, 'source': 'standalone', 'python': 'python', 'username': 'one@example.invalid',
            'tenant_id': 'tenant-one', 'object_id': 'user-one', 'upstream': UPSTREAM,
            'headers': dict(app_config.DEFAULT_HEADERS)}
GATEWAY_CATALOG = {'data': [
    {'id': 'claude-opus-5.5', 'supported_endpoints': ['/v1/messages'], 'name': 'Claude Opus 5.5',
     'capabilities': {'supports': {'reasoning_effort': ['low', 'high', 'max']}}},
    {'id': 'gpt-6-sol', 'supported_endpoints': ['/responses'], 'name': 'GPT-6 Sol'},
    {'id': 'claude-haiku-5.5', 'supported_endpoints': ['/v1/messages'], 'capabilities': {}},
]}
MODELS = [{'model_id': 'claude-opus-5.5', 'supported_reasoning_efforts': '["low", "high", "max"]'},
          {'model_id': 'claude-haiku-5.5', 'supported_reasoning_efforts': '[]'}]


def token(expiry, label='one', oid='user-one'):
    fields = {'aud': RESOURCE, 'azp': CLIENT_ID, 'scp': 'LLM.Read', 'oid': oid, 'tid': 'tenant-one',
              'exp': expiry, 'preferred_username': 'one@example.invalid'}
    return 'fixture.' + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip('=') + '.' + label


class GatewayCatalogTests(unittest.TestCase):
    def test_only_messages_models_are_kept_in_gateway_order(self):
        self.assertEqual(app_config.parse_models(GATEWAY_CATALOG), MODELS)

    def test_empty_or_malformed_catalog_is_rejected(self):
        for body in ({}, {'data': 'x'}, {'data': [{'id': 'gpt', 'supported_endpoints': ['/responses']}]}, []):
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                app_config.parse_models(body)

    def test_fetch_uses_verified_host_and_given_headers(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps(GATEWAY_CATALOG).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('app_config.http.client.HTTPSConnection', return_value=connection) as factory:
            self.assertEqual(app_config.fetch_models(UPSTREAM, {'Authorization': 'Bearer x'}), MODELS)
        self.assertEqual(factory.call_args.args[0], 'loki.delve.office.com')
        connection.request.assert_called_once_with('GET', '/api/v1/llmapi/models',
                                                   headers={'Authorization': 'Bearer x'})
        with self.assertRaises(RuntimeError):
            app_config.fetch_models('https://attacker.example/api', {})

    def test_http_error_is_not_parsed(self):
        response = Mock(status=401)
        response.read.return_value = b'{"data": []}'
        with patch('app_config.http.client.HTTPSConnection') as factory:
            factory.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                app_config.fetch_models(UPSTREAM, {})

    def test_header_allowlist_drops_unknown_and_multiline_values(self):
        headers = {**app_config.DEFAULT_HEADERS, 'X-HostPlatform': 'Windows', 'X-ClientType': 'a\r\nInjected: 1'}
        result = app_config.approved_headers(headers)
        self.assertNotIn('X-HostPlatform', result)
        self.assertNotIn('X-ClientType', result)
        self.assertEqual(result['X-Llm-Api-Version'], app_config.DEFAULT_HEADERS['X-Llm-Api-Version'])


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'models.json'
        self.now = 100.0
        self.fetch = Mock(return_value=MODELS)

    def catalog(self):
        return ModelCatalog(self.path, fetch=self.fetch, clock=lambda: self.now, ttl=900, retry=60)

    def test_cold_start_fetches_even_for_read_only_and_persists(self):
        catalog = self.catalog()
        self.assertEqual(catalog.get(UPSTREAM, {'Authorization': 'Bearer t'}, refresh=False), MODELS)
        self.assertEqual(json.loads(self.path.read_text()), MODELS)
        self.assertEqual(catalog.source, 'gateway')
        catalog.get(UPSTREAM, {}, refresh=False)
        self.now += 10_000
        catalog.get(UPSTREAM, {}, refresh=False)
        self.fetch.assert_called_once()

    def test_ttl_refresh_and_failure_keeps_last_known_models(self):
        catalog = self.catalog()
        catalog.get(UPSTREAM, {})
        self.now += 901
        self.fetch.side_effect = RuntimeError('synthetic outage')
        self.assertEqual(catalog.get(UPSTREAM, {}), MODELS)
        self.now += 30
        catalog.get(UPSTREAM, {})
        self.assertEqual(self.fetch.call_count, 2)  # backoff, not a fetch per request

    def test_restart_serves_cached_catalog_while_gateway_is_down(self):
        self.catalog().get(UPSTREAM, {})
        self.fetch.side_effect = RuntimeError('synthetic outage')
        restarted = self.catalog()
        self.assertEqual(restarted.source, 'cache')
        self.assertEqual(restarted.get(UPSTREAM, {}), MODELS)

    def test_no_models_anywhere_is_a_clear_error(self):
        self.fetch.side_effect = RuntimeError('synthetic outage')
        with self.assertRaisesRegex(RefreshError, 'model_catalog_unavailable'):
            self.catalog().get(UPSTREAM, {})


class StandaloneRefresherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        for patcher in (patch('token_refresh.profile_dir', return_value=self.profile),
                        # Standalone mode must never touch the Copilot database.
                        patch('token_refresh.locate_db', side_effect=AssertionError('database used')),
                        patch('token_refresh.load_config', side_effect=AssertionError('database used'))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.time = 1000
        self.acquire = Mock(return_value=(token(4600), 4600))
        self.fetch = Mock(return_value=MODELS)

    def manager(self, settings=SETTINGS):
        catalog = ModelCatalog(self.profile / 'models.json', fetch=self.fetch)
        manager = TokenRefresher(settings, Mock(), acquire=self.acquire, clock=lambda: self.time, catalog=catalog)
        self.addCleanup(manager.close)
        return manager

    def test_profile_without_database_loads_and_reports_source(self):
        (self.profile / 'settings.json').write_text(json.dumps(SETTINGS))
        manager = TokenRefresher.from_profile(Mock())
        self.assertTrue(manager.standalone)
        self.assertEqual(manager.snapshot()['credential_source'], 'standalone')

    def test_profile_with_unverified_upstream_or_headers_is_rejected(self):
        for change in ({'upstream': 'https://attacker.example/api'}, {'headers': {}}, {'headers': 'x'},
                       {'object_id': ''}):
            with self.subTest(change=change):
                (self.profile / 'settings.json').write_text(json.dumps({**SETTINGS, **change}))
                with self.assertRaisesRegex(RefreshError, 'invalid_refresh_configuration'):
                    TokenRefresher.from_profile(Mock())

    def test_request_uses_microsoft_token_and_gateway_catalog(self):
        base, headers, models = self.manager().get_config()
        self.assertEqual(base, UPSTREAM)
        self.assertEqual(headers['Authorization'], 'Bearer ' + token(4600))
        self.assertEqual(headers['X-ClientScenario'], 'Code')
        self.assertEqual(models, MODELS)
        self.assertEqual(self.fetch.call_args.args[1]['Authorization'], 'Bearer ' + token(4600))

    def test_without_token_there_is_no_fallback_credential(self):
        self.acquire.side_effect = RefreshError('interaction_required')
        manager = self.manager()
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            manager.get_config()
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            manager.get_config(refresh=False)
        self.fetch.assert_not_called()

    def test_wrong_account_token_is_rejected(self):
        self.acquire.return_value = (token(4600, oid='user-two'), 4600)
        manager = self.manager()
        with self.assertRaisesRegex(RefreshError, 'token_identity_mismatch'):
            manager.get_config()
        self.fetch.assert_not_called()

    def test_proxy_serves_models_from_gateway_catalog(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.default_effort = 'xhigh'
        server.token_refresher = self.manager()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/v1/models', timeout=5) as response:
                ids = [m['id'] for m in json.load(response)['data']]
            with urlopen(f'http://127.0.0.1:{server.server_port}/authorization', timeout=5) as response:
                summary = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertIn('copilot-auto', ids)
        self.assertTrue(any(i.startswith('claude-opus-5') for i in ids))
        self.assertEqual(summary['database_selection'], 'not_used')
        self.assertEqual(summary['credential_source'], 'standalone')
        self.assertNotIn(token(4600), json.dumps(summary))


class HelperInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.app = Mock()
        self.app.get_accounts.return_value = [{'username': 'one@example.invalid',
                                               'local_account_id': 'user-one', 'realm': 'tenant-one'}]
        self.app.acquire_token_silent_with_error.return_value = {
            'access_token': token(5000), 'expires_in': 4000,
            'id_token_claims': {'oid': 'user-one', 'tid': 'tenant-one'}}
        self.app.acquire_token_interactive.return_value = {
            'access_token': token(5000), 'id_token_claims': {'oid': 'user-one', 'tid': 'tenant-one',
                                                             'preferred_username': 'one@example.invalid'}}
        sdk = types.SimpleNamespace(PublicClientApplication=Mock(return_value=self.app), __version__='1.39.0')
        extensions = types.SimpleNamespace(FilePersistenceWithDataProtection=Mock(), PersistedTokenCache=Mock())
        self.fetch = Mock(return_value=MODELS)
        for patcher in (patch.dict('sys.modules', {'msal': sdk, 'msal_extensions': extensions}),
                        patch('token_helper.profile_dir', return_value=self.profile),
                        patch('token_helper.time.time', return_value=1000),
                        patch('app_config.fetch_models', self.fetch),
                        patch('app_config.read_provider', side_effect=RuntimeError('no Copilot app')),
                        patch('app_config.load_config', side_effect=RuntimeError('no Copilot app'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self):
        return json.loads((self.profile / 'settings.json').read_text())

    def test_fresh_machine_without_copilot_signs_in_and_publishes_standalone_profile(self):
        result = token_helper.initialize(interactive=True)
        self.assertEqual((result['source'], result['models']), ('standalone', 2))
        settings = self.settings()
        self.assertEqual(settings['source'], 'standalone')
        self.assertNotIn('database', settings)
        self.assertEqual(settings['upstream'], app_config.DEFAULT_UPSTREAM)
        self.assertEqual(settings['headers'], app_config.DEFAULT_HEADERS)
        self.assertEqual(json.loads((self.profile / 'models.json').read_text()), MODELS)
        self.assertNotIn(token(5000), (self.profile / 'settings.json').read_text())
        self.app.acquire_token_interactive.assert_called_once()

    def test_non_interactive_setup_never_opens_sign_in(self):
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            token_helper.initialize(interactive=False)
        self.app.acquire_token_interactive.assert_not_called()
        self.assertFalse((self.profile / 'settings.json').exists())

    def test_catalog_failure_does_not_publish_a_profile(self):
        self.fetch.side_effect = RuntimeError('HTTP 403')
        with self.assertRaisesRegex(RefreshError, 'model_catalog_unavailable'):
            token_helper.initialize(interactive=True)
        self.assertFalse((self.profile / 'settings.json').exists())

    def test_database_profile_migrates_silently(self):
        legacy = {'enabled': True, 'python': 'old-python', 'database': 'C:/fixture/data.db',
                  'username': 'one@example.invalid', 'tenant_id': 'tenant-one', 'object_id': 'user-one'}
        (self.profile / 'settings.json').write_text(json.dumps(legacy))
        self.assertEqual(token_helper.status()['source'], 'copilot_db')
        result = token_helper.migrate()
        self.assertTrue(result['migrated'])
        self.app.acquire_token_interactive.assert_not_called()
        settings = self.settings()
        self.assertEqual((settings['source'], settings['object_id']), ('standalone', 'user-one'))
        self.assertNotIn('database', settings)
        self.assertEqual(token_helper.status()['source'], 'standalone')
        self.assertFalse(token_helper.migrate()['migrated'])

    def test_failed_migration_keeps_the_working_database_profile(self):
        legacy = {'enabled': True, 'python': 'old-python', 'database': 'C:/fixture/data.db',
                  'username': 'one@example.invalid', 'tenant_id': 'tenant-one', 'object_id': 'user-one'}
        (self.profile / 'settings.json').write_text(json.dumps(legacy))
        self.app.get_accounts.return_value = []
        with self.assertRaisesRegex(RefreshError, 'interaction_required'):
            token_helper.migrate()
        self.assertEqual(self.settings(), legacy)


class LayoutMigrationTests(unittest.TestCase):
    def test_legacy_root_helper_is_repointed_to_scripts(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state.json'
            registry = desktop_fixtures.MemoryRegistry()
            registry.writable = False
            desktop.configure_desktop('enable', registry, state, root)
            entry = root / 'Claude-3p' / 'configLibrary' / (desktop.LOCAL_ENTRY_ID + '.json')
            entry.write_text(json.dumps(desktop.LEGACY_HELPER_VALUES))
            state.write_text(json.dumps(desktop.LEGACY_HELPER_STATE))
            self.assertTrue(desktop.configure_desktop('status', registry, state, root)['configured'])
            preview = desktop.configure_desktop('migrate', registry, state, root, dry_run=True)
            self.assertFalse(preview['migrated'])
            self.assertEqual(json.loads(entry.read_text()), desktop.LEGACY_HELPER_VALUES)
            self.assertTrue(desktop.configure_desktop('migrate', registry, state, root)['migrated'])
            self.assertEqual(json.loads(entry.read_text()), desktop.HELPER_VALUES)
            self.assertTrue(desktop.HELPER_VALUES['inferenceCredentialHelper'].endswith(
                str(Path('scripts') / 'claude-gateway-auth.ps1')))
            self.assertFalse(desktop.configure_desktop('migrate', registry, state, root)['migrated'])
            desktop.configure_desktop('restore', registry, state, root)
            self.assertFalse(entry.exists())

    def test_other_deployments_are_not_migrated(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = desktop_fixtures.MemoryRegistry()
            result = desktop.configure_desktop('migrate', registry, root / 'state.json', root)
            self.assertFalse(result['migrated'])
            self.assertFalse((root / 'Claude-3p').exists())
            # A helper owned by another installation folder is left untouched.
            state = root / 'state.json'
            other = {**desktop.LEGACY_HELPER_STATE,
                     'values': desktop.helper_values(root / 'other folder' / 'claude-gateway-auth.ps1')}
            state.write_text(json.dumps(other))
            result = desktop.configure_desktop('migrate', registry, state, root)
            self.assertEqual((result['migrated'], result['managed_by_this_proxy']), (False, False))
            self.assertEqual(json.loads(state.read_text()), json.loads(json.dumps(other)))

    def test_model_refresh_repoints_a_root_layout_session_hook(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'config').mkdir()
            (root / 'config' / 'claude-settings.json').write_text((CONFIG / 'claude-settings.json').read_text())
            template = json.loads((CONFIG / 'claude-settings.json').read_text())
            target = root / 'global.json'
            with patch('configure_claude.ROOT', root):
                configure(target, template, quiet=True)
            data = json.loads(target.read_text())
            legacy = 'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + str(root / 'agency-start.ps1') + '"'
            data['hooks']['SessionStart'][-1]['hooks'][0]['command'] = legacy
            target.write_text(json.dumps(data))
            with patch('refresh_models.build_opener') as opener, patch('configure_claude.ROOT', root), \
                 redirect_stdout(io.StringIO()):
                opener.return_value.open.return_value = io.BytesIO(b'{"data":[{"id":"claude-opus-5-5"}]}')
                refresh(root=root, global_path=target)
            commands = [h['command'] for g in json.loads(target.read_text())['hooks']['SessionStart'] for h in g['hooks']]
            self.assertEqual(commands, [legacy.replace(str(root / 'agency-start.ps1'),
                                                       str(root / 'scripts' / 'agency-start.ps1'))])


if __name__ == '__main__':
    unittest.main()
