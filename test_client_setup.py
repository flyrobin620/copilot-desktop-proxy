"""Offline Windows launcher tests: temporary profiles, fake App policy, local metadata only."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from configure_claude import configure
from refresh_models import build_settings

ROOT = Path(__file__).resolve().parent


class SettingsPreviewTests(unittest.TestCase):
    def test_preview_does_not_create_or_modify_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'new' / 'settings.json'
            with redirect_stdout(io.StringIO()) as output:
                configure(path, dry_run=True)
            self.assertTrue(json.loads(output.getvalue())['dry_run'])
            self.assertFalse(path.parent.exists())
            path.parent.mkdir()
            path.write_text('{"permissions":{"defaultMode":"dontAsk"},"env":{"ANTHROPIC_AUTH_TOKEN":""}}')
            before = path.read_bytes()
            configure(path, quiet=True, migrate_legacy_beta=True, dry_run=True)
            self.assertEqual(path.read_bytes(), before)

    def test_cli_validation_failure_has_no_configuration_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            # Deliberately non-secret sentinel; no user data is used.
            path.write_text('{"env": "synthetic-content-not-for-output"}')
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'configure_claude.py'),
                                     '--settings', str(path), '--dry-run'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)['reason'], 'settings_validation_or_write_failed')
            self.assertNotIn('synthetic-content-not-for-output', result.stdout + result.stderr)


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows PowerShell launcher integration')
class ClientLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude clients ' ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / 'user settings.json'
        self.documents = self.root / 'user documents'
        self.events_path = self.root / 'events.jsonl'
        self.catalog_status = 200
        self.catalog_body = {'data': [{'id': 'claude-opus-5-5'}]}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                body = json.dumps(owner.catalog_body).encode()
                self.send_response(owner.catalog_status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02})
        self.thread.start()
        self.addCleanup(self.close_server)
        for name in ('configure-clients.ps1', 'install-agency.ps1', 'claude-desktop.ps1',
                     'configure_claude.py', 'restore_claude.py', 'claude-settings.json'):
            shutil.copyfile(ROOT / name, self.root / name)
        script = self.root / 'configure-clients.ps1'
        script.write_text(script.read_text().replace('http://127.0.0.1:8787/desktop/v1/models',
                          f'http://127.0.0.1:{self.server.server_port}/desktop/v1/models'), encoding='utf-8')
        python = sys.executable.replace("'", "''")
        (self.root / 'launcher-common.ps1').write_text(
            f"function Resolve-ProxyPython {{ return '{python}' }}\n", encoding='utf-8')
        (self.root / 'one-click.ps1').write_text('''param([string]$Mode)
Add-Content -LiteralPath (Join-Path $PSScriptRoot 'events.jsonl') -Value '{"event":"proxy"}'
if ($env:CLIENT_SETUP_TEST_PROXY_FAIL -eq '1') { exit 3 }
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot '.models') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'generated.json') -Destination (Join-Path $PSScriptRoot '.models\\claude-settings.json')
exit 0
''', encoding='utf-8')
        (self.root / 'claude_desktop.py').write_text('''import json, os, sys
from pathlib import Path
dry = '--dry-run' in sys.argv
mode = sys.argv[1]
with (Path(__file__).parent / 'events.jsonl').open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'event':mode, 'dry':dry}) + '\\n')
key = 'CLIENT_SETUP_TEST_PREFLIGHT_FAIL' if dry else 'CLIENT_SETUP_TEST_APP_FAIL'
if os.environ.get(key) == '1':
    print(json.dumps({'ok':False, 'reason':'synthetic_conflict'}))
    raise SystemExit(2)
print(json.dumps({'ok':True}))
''', encoding='utf-8')
        template = json.loads((ROOT / 'claude-settings.json').read_text())
        generated, _ = build_settings(template, [{'id': 'claude-opus-5-5'}, {'id': 'claude-sonnet-5-5'},
                                                 {'id': 'claude-haiku-4-5'}])
        (self.root / 'generated.json').write_text(json.dumps(generated))
        self.env = os.environ.copy()
        for key in ('CLIENT_SETUP_TEST_PROXY_FAIL', 'CLIENT_SETUP_TEST_PREFLIGHT_FAIL', 'CLIENT_SETUP_TEST_APP_FAIL'):
            self.env.pop(key, None)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def events(self):
        return [json.loads(line) for line in self.events_path.read_text().splitlines()] if self.events_path.exists() else []

    def run_setup(self, *args, script='configure-clients.ps1', expected=0):
        command = ['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-File', str(self.root / script)]
        if script != 'claude-desktop.ps1':
            command += ['-SettingsPath', str(self.settings), '-DocumentsPath', str(self.documents)]
        result = subprocess.run(command + list(args), cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def add_profile(self, folder, text, encoding='utf-8'):
        path = self.documents / folder / 'profile.ps1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_both_configures_generated_models_and_profiles_idempotently(self):
        original = {'env': {'UNRELATED': 'keep', 'ANTHROPIC_AUTH_TOKEN': ''},
                    'permissions': {'defaultMode': 'dontAsk'},
                    'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo personal'}]}]}}
        self.settings.write_text(json.dumps(original))
        personal = '# personal profile\nfunction personal { Get-Date }\n'
        profiles = [self.add_profile('WindowsPowerShell', personal),
                    self.add_profile('PowerShell', personal, 'utf-16')]
        self.run_setup()
        data = json.loads(self.settings.read_text())
        self.assertEqual(data['env']['UNRELATED'], 'keep')
        self.assertEqual(data['env']['ANTHROPIC_AUTH_TOKEN'], '')
        self.assertEqual(data['permissions'], original['permissions'])
        self.assertEqual(data['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'claude-opus-5-5[1m]')
        self.assertEqual(data['env']['ENABLE_TOOL_SEARCH'], 'true')
        self.assertEqual(data['hooks']['SessionStart'][0], original['hooks']['SessionStart'][0])
        for path in profiles:
            text = path.read_text(encoding='utf-8-sig')
            self.assertIn(personal.strip(), text)
            self.assertEqual(text.count('# BEGIN COPILOT DESKTOP PROXY'), 1)
            self.assertIn(str(self.root).replace("'", "''"), text)
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}, {'event': 'proxy'},
                                         {'event': 'enable', 'dry': False}])
        self.assertEqual(self.requests, ['/desktop/v1/models'])
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in [self.settings, *profiles]]
        self.run_setup()
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in [self.settings, *profiles]], before)
        self.assertEqual(sum(e['event'] == 'proxy' for e in self.events()), 2)

    def test_preview_does_not_start_proxy_or_write_any_client(self):
        self.run_setup('-WhatIf')
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}])
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.documents.exists())
        self.assertEqual(self.requests, [])

    def test_app_conflict_stops_before_agency_or_proxy(self):
        self.env['CLIENT_SETUP_TEST_PREFLIGHT_FAIL'] = '1'
        self.run_setup(expected=1)
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}])
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.documents.exists())

    def test_invalid_agency_settings_stop_before_proxy_or_app_write(self):
        self.settings.write_text('{"env":')
        before = self.settings.read_bytes()
        self.run_setup(expected=1)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(self.documents.exists())
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}])

    def test_broken_profile_stops_before_any_settings_write(self):
        profile = self.add_profile('PowerShell', '# BEGIN COPILOT DESKTOP PROXY\n# incomplete')
        before = profile.read_bytes()
        self.run_setup(expected=1)
        self.assertFalse(self.settings.exists())
        self.assertEqual(profile.read_bytes(), before)
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}])

    def test_proxy_failure_propagates_and_leaves_clients_unconfigured(self):
        self.env['CLIENT_SETUP_TEST_PROXY_FAIL'] = '1'
        self.run_setup(expected=1)
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.documents.exists())
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}, {'event': 'proxy'}])
        self.assertEqual(self.requests, [])

    def test_old_worker_or_empty_catalog_blocks_client_writes(self):
        for status, body in ((404, {}), (200, {}), (200, {'data': []})):
            with self.subTest(status=status, body=body):
                self.catalog_status, self.catalog_body = status, body
                self.run_setup(expected=1)
                self.assertFalse(self.settings.exists())
                self.assertFalse(self.documents.exists())
                self.assertNotIn({'event': 'enable', 'dry': False}, self.events())

    def test_app_apply_failure_reports_partial_success_and_can_retry(self):
        self.env['CLIENT_SETUP_TEST_APP_FAIL'] = '1'
        output = self.run_setup(expected=1)
        self.assertIn('Agency=True; Claude App=False', output)
        self.assertIn('Setup may be partial', output)
        self.assertTrue(self.settings.exists())
        del self.env['CLIENT_SETUP_TEST_APP_FAIL']
        self.run_setup()

    def test_existing_agency_entry_uses_common_flow_without_app_policy(self):
        self.run_setup(script='install-agency.ps1')
        self.assertTrue(self.settings.exists())
        self.assertEqual(self.events(), [{'event': 'proxy'}])
        self.assertEqual(self.requests, [])

    def test_existing_agency_entry_preserves_failure_exit_and_preview(self):
        self.run_setup('-WhatIf', script='install-agency.ps1')
        self.assertEqual(self.events(), [])
        self.assertFalse(self.settings.exists())
        self.env['CLIENT_SETUP_TEST_PROXY_FAIL'] = '1'
        self.run_setup(script='install-agency.ps1', expected=1)
        self.assertFalse(self.settings.exists())

    def test_existing_app_configure_uses_common_flow_without_agency_writes(self):
        self.run_setup('-Mode', 'Configure', script='claude-desktop.ps1')
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.documents.exists())
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}, {'event': 'proxy'},
                                         {'event': 'enable', 'dry': False}])

    def test_app_preview_and_policy_operations_do_not_start_proxy(self):
        self.run_setup('-Mode', 'Start', '-WhatIf', script='claude-desktop.ps1')
        self.run_setup('-Mode', 'Check', script='claude-desktop.ps1')
        self.run_setup('-Mode', 'Restore', '-WhatIf', script='claude-desktop.ps1')
        self.run_setup('-Mode', 'Restore', script='claude-desktop.ps1')
        self.assertEqual(self.events(), [{'event': 'enable', 'dry': True}, {'event': 'status', 'dry': False},
                                         {'event': 'restore', 'dry': True}, {'event': 'restore', 'dry': False}])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.documents.exists())


if __name__ == '__main__':
    unittest.main()
