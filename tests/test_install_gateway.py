"""Installer gateway setup with real merges, isolated profiles and no services/network."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from tests import ROOT, SRC
import claude_desktop as desktop
from tests.test_claude_desktop import MemoryRegistry


class DesktopInstallTests(unittest.TestCase):
    def test_fresh_install_creates_helper_without_registry_even_if_writable(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state.json'
            registry = MemoryRegistry()
            preview = desktop.configure_desktop('install', registry, state, root, dry_run=True)
            self.assertFalse(preview['configured'])
            self.assertFalse(state.exists())
            result = desktop.configure_desktop('install', registry, state, root, require_helper=True)
            self.assertTrue(result['configured'])
            self.assertTrue(result['restart_required'])
            self.assertEqual(registry.writes, 0)
            entry = root / 'Claude-3p/configLibrary' / (desktop.LOCAL_ENTRY_ID + '.json')
            self.assertEqual(json.loads(entry.read_text()), desktop.HELPER_VALUES)
            before = (entry.read_bytes(), entry.stat().st_mtime_ns)
            again = desktop.configure_desktop('install', registry, state, root)
            self.assertFalse(again['restart_required'])
            self.assertEqual(before, (entry.read_bytes(), entry.stat().st_mtime_ns))
            desktop.configure_desktop('restore', registry, state, root)
            self.assertFalse(entry.exists())

    def test_existing_owned_registry_is_kept_but_on_demand_is_not_falsely_enabled(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = MemoryRegistry()
            state = root / 'state.json'
            desktop.configure_desktop('enable', registry, state, root)
            self.assertTrue(desktop.configure_desktop('install', registry, state, root)['configured'])
            before = dict(registry.user)
            with self.assertRaisesRegex(desktop.ConfigurationError, 'on_demand_requires_local_configuration'):
                desktop.configure_desktop('install', registry, state, root, require_helper=True)
            self.assertEqual(registry.user, before)

    def test_foreign_policy_and_local_profile_are_preserved(self):
        for policy in (True, False):
            with self.subTest(policy=policy), TemporaryDirectory() as temp:
                root = Path(temp)
                registry = MemoryRegistry()
                state = root / 'state.json'
                if policy:
                    registry.machine['inferenceProvider'] = ('foreign', 1)
                else:
                    path = root / 'Claude-3p/configLibrary/foreign.json'
                    path.parent.mkdir(parents=True)
                    path.write_text('foreign settings')
                with self.assertRaises(desktop.ConfigurationError):
                    desktop.configure_desktop('install', registry, state, root)
                self.assertFalse(state.exists())
                self.assertFalse((root / 'Claude-3p/configLibrary/_meta.json').exists())

    def test_redirected_files_are_detected_without_package_identity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            real_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                if path.name == '_meta.json':
                    return root / 'Packages/Test.App/LocalCache/Local/Claude-3p/configLibrary/_meta.json'
                return real_resolve(path, *args, **kwargs)
            with patch.object(Path, 'resolve', resolve):
                self.assertTrue(desktop.redirected_appdata(root))
            with patch.dict(os.environ, {'LOCALAPPDATA': temp, 'APPDATA': temp}), \
                 patch('claude_desktop.packaged_process', return_value=False), \
                 patch('claude_desktop.redirected_appdata', return_value=True), \
                 patch('sys.argv', ['claude_desktop.py', 'install']), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(desktop.main(), 1)
            self.assertEqual(json.loads(out.getvalue())['reason'], 'packaged_app_redirects_appdata')
            self.assertEqual(list(root.iterdir()), [])


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows installer')
class InstallerGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="gateway install ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ('src', 'scripts', 'config', 'profile', 'local', 'roaming'):
            (self.root / folder).mkdir()
        script = (ROOT / 'scripts/install.ps1').read_text(encoding='utf-8-sig')
        # Disable only service discovery and redirect the unrelated profile scan.
        script = script.replace("'http://127.0.0.1:8787' + $Path", "'http://127.0.0.1:1' + $Path")
        script = script.replace("[Environment]::GetFolderPath('MyDocuments')", "(Join-Path $ProxyRoot 'documents')")
        (self.root / 'scripts/install.ps1').write_text(script, encoding='utf-8')
        for name in ('paths.py', 'configure_claude.py', 'restore_claude.py'):
            shutil.copyfile(SRC / name, self.root / 'src' / name)
        shutil.copyfile(SRC / 'claude_desktop.py', self.root / 'src/desktop_impl.py')
        # All configuration code is real; only the Windows policy store is fake.
        (self.root / 'src/claude_desktop.py').write_text('''import os
import desktop_impl as module
class Registry:
    def read(self, machine=False):
        return {'policy': ('foreign', 1)} if machine and os.environ.get('FIXTURE_POLICY') else {}
    def can_write(self): return True
module.RegistryPolicy = Registry
module.packaged_process = lambda: os.environ.get('FIXTURE_PACKAGED') == '1'
raise SystemExit(module.main())
''', encoding='utf-8')
        (self.root / 'src/token_helper.py').write_text('print(\'{"ok":true,"configured":false}\')')
        executable = sys.executable.replace("'", "''")
        (self.root / 'scripts/launcher-common.ps1').write_text(
            f"function Resolve-ProxyPython {{ return '{executable}' }}\n"
            "function Resolve-ProxyClaude { return 'existing-claude.exe' }\n", encoding='utf-8')
        for name in ('start.ps1', 'stop.ps1', 'set-startup-mode.ps1'):
            (self.root / 'scripts' / name).write_text("throw 'Unexpected lifecycle mutation in NoStart test'")
        for name in ('package-lock.json', 'claude-settings.json'):
            shutil.copyfile(ROOT / 'config' / name, self.root / 'config' / name)
        self.env = {**os.environ, 'USERPROFILE': str(self.root / 'profile'),
                    'LOCALAPPDATA': str(self.root / 'local'), 'APPDATA': str(self.root / 'roaming')}
        for name in ('CLAUDE_CONFIG_DIR', 'CLAUDE_USER_DATA_DIR', 'FIXTURE_POLICY', 'FIXTURE_PACKAGED'):
            self.env.pop(name, None)
        self.settings = self.root / 'profile/.claude/settings.json'
        self.entry = self.root / 'local/Claude-3p/configLibrary' / (desktop.LOCAL_ENTRY_ID + '.json')

    def run_install(self, *args, expected=0):
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'Bypass', '-File', str(self.root / 'scripts/install.ps1'),
            '-NoPull', '-NoStart', '-SkipClaude', *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def test_default_install_configures_both_and_rerun_is_idempotent(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({'permissions': {'defaultMode': 'default'},
            'env': {'PERSONAL': 'keep', 'ENABLE_TOOL_SEARCH': 'false', 'ANTHROPIC_AUTH_TOKEN': 'fixture-existing'},
            'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo personal'}]}]}}))
        output = self.run_install()
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:8787')
        self.assertEqual(data['env']['ANTHROPIC_AUTH_TOKEN'], 'fixture-existing')
        self.assertEqual(data['env']['PERSONAL'], 'keep')
        self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'], 'false')
        self.assertEqual(data['permissions'], {'defaultMode': 'default'})
        self.assertEqual(len(data['hooks']['SessionStart']), 2)
        app = json.loads(self.entry.read_text())
        self.assertEqual(app['inferenceProvider'], 'gateway')
        self.assertEqual(app['inferenceCredentialKind'], 'helper-script')
        self.assertEqual(app['inferenceCredentialHelper'], str(self.root / 'scripts/claude-gateway-auth.ps1'))
        self.assertIn('Claude App', output)
        self.assertFalse((self.root / '.run').exists())
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.entry, self.settings)]
        self.run_install()
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.entry, self.settings)])

    def test_code_only_creates_missing_settings_and_honors_custom_directory(self):
        self.env['CLAUDE_CONFIG_DIR'] = str(self.root / 'custom-code')
        self.run_install('-Clients', 'Code')
        self.assertTrue((self.root / 'custom-code/settings.json').exists())
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.entry.exists())

    def test_desktop_only_does_not_create_cli_settings(self):
        self.run_install('-Clients', 'Desktop')
        self.assertFalse(self.settings.exists())
        self.assertTrue(self.entry.exists())

    def test_code_only_does_not_migrate_existing_desktop_configuration(self):
        self.entry.parent.mkdir(parents=True)
        original = json.dumps(desktop.helper_values(self.root / 'claude-gateway-auth.ps1'))
        self.entry.write_text(original)
        self.run_install('-Clients', 'Code')
        self.assertEqual(self.entry.read_text(), original)
        self.assertTrue(self.settings.exists())

    def test_keep_does_not_opt_in_new_clients(self):
        self.run_install('-Clients', 'Keep')
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.entry.exists())

    def test_foreign_cli_gateway_blocks_before_app_writes(self):
        self.settings.parent.mkdir()
        original = '{"env":{"ANTHROPIC_BASE_URL":"https://other.invalid","ANTHROPIC_AUTH_TOKEN":"PRIVATE"}}'
        self.settings.write_text(original)
        output = self.run_install(expected=1)
        self.assertIn('existing_gateway_conflict', output)
        self.assertNotIn('PRIVATE', output)
        self.assertEqual(self.settings.read_text(), original)
        self.assertFalse(self.entry.exists())

    def test_cloud_provider_is_not_silently_overridden(self):
        self.settings.parent.mkdir()
        original = '{"env":{"CLAUDE_CODE_USE_BEDROCK":"1"}}'
        self.settings.write_text(original)
        self.run_install(expected=1)
        self.assertEqual(self.settings.read_text(), original)

    def test_packaged_context_and_enterprise_policy_fail_without_cli_write(self):
        for flag in ('FIXTURE_PACKAGED', 'FIXTURE_POLICY'):
            with self.subTest(flag=flag):
                self.env[flag] = '1'
                self.run_install(expected=1)
                self.env.pop(flag)
                self.assertFalse(self.entry.exists())
                self.assertFalse(self.settings.exists())


if __name__ == '__main__':
    unittest.main()
